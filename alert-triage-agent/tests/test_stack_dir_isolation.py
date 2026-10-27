"""The suite never reads or writes the stack-local files of a stack deployed from this checkout.

A checkout that deployed stack <prefix> has .<prefix>-api-key, .<prefix>-apigee.env and
.deploy-state/ in alert-triage-agent/ (the default ATA_STACK_DIR). tests/conftest.py points
ATA_STACK_DIR at an empty per-session directory, so scripts started by the tests do not see them
(D8: 21 tests failed in a checkout that had deployed stack atat). These tests plant such files in
the checkout under an unused prefix and check that the scripts read the test directory instead.
"""

import os
import secrets
import shutil
import string
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS_DIR = ROOT / "scripts" / "scenarios"
TEST_BASH = shutil.which(os.environ.get("ATA_TEST_BASH", "bash"))


def _unused_prefix():
    """A valid NAME_PREFIX ([a-z][a-z0-9]{1,9}) with no stack-local file in this checkout."""
    alphabet = string.ascii_lowercase + string.digits
    for _ in range(100):
        prefix = "zt" + "".join(secrets.choice(alphabet) for _ in range(6))
        if not any((ROOT / name).exists() for name in (f".{prefix}-api-key", f".{prefix}-apigee.env")):
            return prefix
    pytest.fail("no unused test prefix found")


@pytest.fixture
def planted(tmp_path):
    """Stack-local files of `prefix` in the checkout (removed afterwards) and in ATA_STACK_DIR."""
    prefix = _unused_prefix()
    stack_dir = Path(os.environ["ATA_STACK_DIR"])
    in_checkout = [ROOT / f".{prefix}-api-key", ROOT / f".{prefix}-apigee.env"]
    in_stack_dir = [stack_dir / p.name for p in in_checkout]
    try:
        in_checkout[0].write_text("checkout-key\n")
        in_checkout[1].write_text("APIGEE_HOST=checkout.invalid\nAPIGEE_ORG=checkout-org\nAPIGEE_ENV=checkout-env\n")
        in_stack_dir[0].write_text("test-dir-key\n")
        in_stack_dir[1].write_text("APIGEE_HOST=test-dir.invalid\nAPIGEE_ORG=test-org\nAPIGEE_ENV=test-env\n")
        yield prefix
    finally:
        for p in in_checkout + in_stack_dir:
            p.unlink(missing_ok=True)


def _env(prefix, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIGEE_", "PINNED_INVENTORY_")) and k not in ("ATA_API_KEY", "MOCK_MODE", "DRY_RUN")}
    env.update(NAME_PREFIX=prefix, MOCK_MODE="false", DRY_RUN="false", **extra)
    return env


def test_conftest_stack_dir_is_a_dir_outside_the_checkout():
    stack_dir = Path(os.environ["ATA_STACK_DIR"]).resolve()
    assert stack_dir.is_dir()
    assert ROOT not in stack_dir.parents and stack_dir != ROOT


def test_common_sh_reads_the_stack_dir_not_the_checkout(planted, tmp_path):
    script = tmp_path / "probe.sh"
    script.write_text(f'INVENTORY_PIN_OPTIONAL=true\nsource "{SCENARIOS_DIR}/common.sh"\n'
                      'echo "FILES=${API_KEY_FILE} ${APIGEE_ENV_FILE}"\n'
                      'echo "APIGEE=${APIGEE_HOST} ${APIGEE_ORG} ${APIGEE_ENV}"\n'
                      'echo "KEY=$(resolve_api_key)"\n')
    # A pin path that does not exist: the pin file is not a stack-local file (ATA_INVENTORY_PIN_FILE).
    res = subprocess.run([TEST_BASH, str(script)], cwd=str(tmp_path), capture_output=True, text=True, timeout=30,
                         env=_env(planted, ATA_INVENTORY_PIN_FILE=str(tmp_path / "no-pin.env")), check=False)
    assert res.returncode == 0, res.stdout + res.stderr
    stack_dir = os.environ["ATA_STACK_DIR"]
    assert f"FILES={stack_dir}/.{planted}-api-key {stack_dir}/.{planted}-apigee.env\n" in res.stdout
    assert "APIGEE=test-dir.invalid test-org test-env\n" in res.stdout
    assert "KEY=test-dir-key\n" in res.stdout
    assert "checkout" not in res.stdout + res.stderr


def test_verify_demo_infra_reads_the_stack_dir_not_the_checkout(planted, tmp_path):
    # verify-demo-infra.sh reads the files before it sources common.sh; stop it at the first gcloud.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for tool in ("gcloud", "curl"):
        (bin_dir / tool).write_text("#!/bin/sh\necho stub-stop >&2\nexit 7\n")
        (bin_dir / tool).chmod(0o755)
    env = _env(planted, PATH=f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
               ATA_INVENTORY_PIN_FILE=str(tmp_path / "no-pin.env"), INVENTORY_PIN_OPTIONAL="true")
    res = subprocess.run([TEST_BASH, str(ROOT / "scripts" / "verify-demo-infra.sh")], cwd=str(tmp_path),
                         capture_output=True, text=True, timeout=60, env=env, check=False)
    out = res.stdout + res.stderr
    stack_dir = os.environ["ATA_STACK_DIR"]
    assert f"using test-dir.invalid from {stack_dir}/.{planted}-apigee.env" in out
    assert f"Using the Apigee API key from {stack_dir}/.{planted}-api-key" in out
    assert "checkout" not in out
