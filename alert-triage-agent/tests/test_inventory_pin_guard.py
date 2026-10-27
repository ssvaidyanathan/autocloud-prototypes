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
