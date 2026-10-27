"""Tests for the inventory baseline pin guards in scripts/scenarios/common.sh.

The pinned revision must run the pinned digest: older inventory code performs the
fault action on GET /admin/*, so a stale pin turns preflight/reset probes into leaks.
"""

import os
import subprocess
from pathlib import Path

import pytest

SCENARIOS_DIR = Path(__file__).parent.parent / "scripts" / "scenarios"
PINNED_DIGEST = "sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce"
PINNED_REVISION = "ata-inventory-api-00073-9vb"


def _run_guard(tmp_path, guard, gcloud_stdout, mock_mode="false"):
    """Source common.sh with a fake gcloud that prints gcloud_stdout, then run guard."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_gcloud = bin_dir / "gcloud"
    fake_gcloud.write_text(f"#!/usr/bin/env bash\necho {gcloud_stdout!r}\n")
    fake_gcloud.chmod(0o755)

    test_sh = tmp_path / "guard.sh"
    test_sh.write_text(f"""#!/usr/bin/env bash
source "{SCENARIOS_DIR}/common.sh"
{guard}
""")
    test_sh.chmod(0o755)

    env = os.environ.copy()
    for var in ("PINNED_INVENTORY_DIGEST", "PINNED_INVENTORY_REVISION", "PINNED_INVENTORY_IMAGE"):
        env.pop(var, None)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["MOCK_MODE"] = mock_mode
    env["DRY_RUN"] = "false"
    return subprocess.run(
        [str(test_sh)], cwd=str(tmp_path), capture_output=True, text=True, env=env, check=False
    )


def test_default_pin_is_revision_running_pinned_digest(tmp_path):
    res = _run_guard(tmp_path, 'echo "${PINNED_INVENTORY_REVISION} ${PINNED_INVENTORY_DIGEST}"', "")
    assert res.returncode == 0
    assert res.stdout.strip().splitlines()[-1] == f"{PINNED_REVISION} {PINNED_DIGEST}"


def test_pinned_revision_guard_passes_on_matching_digest(tmp_path):
    image = f"us-west1-docker.pkg.dev/p/ata-docker/ata-inventory-api@{PINNED_DIGEST}"
    res = _run_guard(tmp_path, "verify_pinned_inventory_revision", image)
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize(
    "gcloud_stdout",
    [
        "us-west1-docker.pkg.dev/p/ata-docker/ata-inventory-api@sha256:eae481b5e7787d8dc7631cb1a3738c039edb071391defda1c2c870edee3dcad8",
        "",  # revision not found / describe failed
    ],
)
def test_pinned_revision_guard_fails_loudly_on_mismatch(tmp_path, gcloud_stdout):
    res = _run_guard(tmp_path, "verify_pinned_inventory_revision", gcloud_stdout)
    assert res.returncode != 0
    assert f"expected {PINNED_DIGEST}" in res.stderr


def test_serving_guard_passes_when_pinned_revision_has_all_traffic(tmp_path):
    res = _run_guard(tmp_path, "verify_inventory_serving_pinned", f"{PINNED_REVISION},100")
    assert res.returncode == 0, res.stdout + res.stderr


@pytest.mark.parametrize(
    "gcloud_stdout",
    [
        "ata-inventory-api-00070-6nq,100",
        f"{PINNED_REVISION};ata-inventory-api-00070-6nq,50;50",
        "",
    ],
)
def test_serving_guard_fails_loudly_when_traffic_elsewhere(tmp_path, gcloud_stdout):
    res = _run_guard(tmp_path, "verify_inventory_serving_pinned", gcloud_stdout)
    assert res.returncode != 0
    assert f"expected {PINNED_REVISION}=100" in res.stderr


@pytest.mark.parametrize("guard", ["verify_pinned_inventory_revision", "verify_inventory_serving_pinned"])
def test_guards_skip_gcloud_in_mock_mode(tmp_path, guard):
    res = _run_guard(tmp_path, guard, "garbage", mock_mode="true")
    assert res.returncode == 0, res.stdout + res.stderr


# --- The pin lives in ONE file: scripts/scenarios/inventory-pin.env --------------------

PIN_FILE = SCENARIOS_DIR / "inventory-pin.env"
REPO = SCENARIOS_DIR.parent.parent


def _source_with_pin(tmp_path, pin_text, guard='echo "PIN=${PINNED_INVENTORY_REVISION} ${PINNED_INVENTORY_DIGEST} PREV=${PREVIOUS_PINNED_INVENTORY_REVISION}"', **env_extra):
    pin = tmp_path / "pin.env"
    pin.write_text(pin_text)
    script = tmp_path / "src.sh"
    script.write_text(f'#!/usr/bin/env bash\nsource "{SCENARIOS_DIR}/common.sh"\n{guard}\n')
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PINNED_INVENTORY_", "ATA_"))}
    env.update(ATA_INVENTORY_PIN_FILE=str(pin), **env_extra)
    return subprocess.run([os.environ.get("ATA_TEST_BASH", "bash"), str(script)], capture_output=True,
                          text=True, env=env, check=False, cwd=str(tmp_path))


GOOD_PIN = f"PIN_INVENTORY_REVISION={PINNED_REVISION}\nPIN_INVENTORY_DIGEST={PINNED_DIGEST}\n"


def test_committed_pin_file_is_the_default_pin():
    text = PIN_FILE.read_text()
    assert f"PIN_INVENTORY_REVISION={PINNED_REVISION}\n" in text
    assert f"PIN_INVENTORY_DIGEST={PINNED_DIGEST}\n" in text


def test_pin_is_read_from_the_pin_file(tmp_path):
    other = "sha256:" + "ab" * 32
    res = _source_with_pin(tmp_path, f"# c\n\nPIN_INVENTORY_REVISION=ata-inventory-api-pin-1\nPIN_INVENTORY_DIGEST={other}\n"
                           f"PIN_INVENTORY_PREVIOUS_REVISION={PINNED_REVISION}\nPIN_INVENTORY_PREVIOUS_DIGEST={PINNED_DIGEST}\n")
    assert res.returncode == 0, res.stderr
    assert res.stdout.strip() == f"PIN=ata-inventory-api-pin-1 {other} PREV={PINNED_REVISION}"


def test_env_override_still_wins_for_one_run(tmp_path):
    res = _source_with_pin(tmp_path, GOOD_PIN, PINNED_INVENTORY_REVISION="ata-inventory-api-00001-abc")
    assert res.returncode == 0, res.stderr
    assert res.stdout.startswith("PIN=ata-inventory-api-00001-abc ")


@pytest.mark.parametrize(
    "pin_text, message",
    [
        (GOOD_PIN + "rm -rf /\n", "unexpected line 'rm -rf /'"),
        (GOOD_PIN + "PINNED_INVENTORY_REVISION=x\n", "unexpected line"),
        (f"PIN_INVENTORY_DIGEST={PINNED_DIGEST}\n", "PIN_INVENTORY_REVISION '' is not"),
        (f"PIN_INVENTORY_REVISION=other-service-00001\nPIN_INVENTORY_DIGEST={PINNED_DIGEST}\n", "is not a ata-inventory-api revision name"),
        (f"PIN_INVENTORY_REVISION={PINNED_REVISION}\nPIN_INVENTORY_DIGEST=sha256:XYZ\n", "is not sha256:<64 hex>"),
        (f"PIN_INVENTORY_REVISION={PINNED_REVISION}\nPIN_INVENTORY_DIGEST={PINNED_DIGEST.upper()}\n", "is not sha256:<64 hex>"),
        (GOOD_PIN + f"PIN_INVENTORY_PREVIOUS_REVISION={PINNED_REVISION}\n", "must both be empty or both valid"),
        (f"PIN_INVENTORY_REVISION={PINNED_REVISION}\nPIN_INVENTORY_DIGEST=$(touch /tmp/pwned)\n", "is not sha256:<64 hex>"),
    ],
)
def test_invalid_pin_file_fails_loudly_before_anything_runs(tmp_path, pin_text, message):
    res = _source_with_pin(tmp_path, pin_text, guard="echo REACHED")
    assert res.returncode != 0
    assert message in res.stderr
    assert "cannot load the ata-inventory-api pin" in res.stderr
    assert "REACHED" not in res.stdout


def test_missing_pin_file_fails_loudly(tmp_path):
    script = tmp_path / "src.sh"
    script.write_text(f'#!/usr/bin/env bash\nsource "{SCENARIOS_DIR}/common.sh"\necho REACHED\n')
    env = dict(os.environ, ATA_INVENTORY_PIN_FILE=str(tmp_path / "nope.env"))
    res = subprocess.run([os.environ.get("ATA_TEST_BASH", "bash"), str(script)], capture_output=True, text=True, env=env)
    assert res.returncode != 0 and "missing or unreadable" in res.stderr and "REACHED" not in res.stdout


@pytest.mark.parametrize("value", ["0", "", "x", "-1", "01", "1.5"])
def test_invalid_instance_cap_fails_loudly(tmp_path, value):
    res = _source_with_pin(tmp_path, GOOD_PIN, guard="echo REACHED", ATA_DEMO_MAX_INSTANCES=value)
    if value == "":
        # Empty means unset: the default 1 applies.
        assert res.returncode == 0
        return
    assert res.returncode != 0
    assert "ATA_DEMO_MAX_INSTANCES" in res.stderr


def test_no_other_file_hardcodes_the_pinned_digest_or_revision():
    """Re-pinning is one step: only the pin file (and tests/docs) name the pinned values."""
    for path in [*(REPO / "scripts").rglob("*.sh"), REPO / "demo/ata-inventory-api/scripts/deploy.sh"]:
        text = path.read_text()
        assert PINNED_DIGEST not in text, path
        assert PINNED_REVISION not in text, path
