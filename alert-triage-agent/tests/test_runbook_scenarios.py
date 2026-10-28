"""Tests for user runbook scenario scripts, --hold duration, and check-result.sh."""

import os
import subprocess
from pathlib import Path
import pytest

SCENARIOS_DIR = Path(__file__).parent.parent / "scripts" / "scenarios"
CHECK_RESULT_SCRIPT = SCENARIOS_DIR / "check-result.sh"


def test_check_result_help():
    """Verify check-result.sh --help displays usage and options."""
    res = subprocess.run(
        [str(CHECK_RESULT_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "--since" in res.stdout
    assert "--scenario" in res.stdout
    assert "--dry-run" in res.stdout


def test_check_result_mock_scenario_s1():
    """Verify check-result.sh --mock --scenario s1 outputs expected values and comparison."""
    res = subprocess.run(
        [str(CHECK_RESULT_SCRIPT), "--mock", "--scenario", "s1"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "Alert Triage Agent: Scenario Result Checker" in res.stdout
    assert "Target Scenario: S1" in res.stdout
    assert "Incident Group 1" in res.stdout
    assert "ata-inventory-api" in res.stdout
    assert "INC0010482" in res.stdout
    assert "Expected Values Comparison (from DEMO.md)" in res.stdout
    assert "[S1: Backend 5xx Error Spike]" in res.stdout
    assert "[MATCH]" in res.stdout


def test_check_result_mock_scenario_s3():
    """Verify check-result.sh --mock --scenario s3 checks 2 distinct groups."""
    res = subprocess.run(
        [str(CHECK_RESULT_SCRIPT), "--mock", "--scenario", "s3"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "[S3: Concurrent Disjoint Incidents]" in res.stdout
    assert "Expected 2 distinct groups/tickets" in res.stdout


@pytest.mark.parametrize("sc", ["s1_backend_5xx.sh", "s2_backend_latency.sh", "s3_concurrent_disjoint.sh", "s4_db_connection_pool.sh", "s5_bad_deploy.sh"])
def test_scenario_scripts_mock_hold_zero(sc):
    """Verify all scenario scripts support --hold, --mock, --dry-run, and complete cleanly."""
    script_path = SCENARIOS_DIR / sc
    res = subprocess.run(
        [str(script_path), "--mock", "--hold", "0s"],
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )
    assert res.returncode == 0
    assert "NEXT STEPS" in res.stdout
    assert "check-result.sh" in res.stdout
    assert "reset-scenarios.sh" in res.stdout


def test_common_api_key_error_when_missing(tmp_path):
    """Verify resolve_api_key prints clear error and instructions if key is missing."""
    isolated_dir = tmp_path / "pkg" / "scripts" / "scenarios"
    isolated_dir.mkdir(parents=True, exist_ok=True)

    test_sh = tmp_path / "test_key.sh"
    test_sh.write_text(f"""#!/usr/bin/env bash
set -e
SCRIPT_DIR="{isolated_dir}"
source "{SCENARIOS_DIR}/common.sh"
resolve_api_key
""")
    test_sh.chmod(0o755)

    env = os.environ.copy()
    env.pop("ATA_API_KEY", None)
    env["MOCK_MODE"] = "false"
    env["DRY_RUN"] = "false"

    res = subprocess.run(
        [str(test_sh)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode != 0
    assert "Apigee consumer API key not found" in res.stderr
    assert "developers/ata-dev@example.com/apps/ata-demo-app" in res.stderr
    assert ".ata-api-key" in res.stderr


def test_common_api_key_success_when_present(tmp_path):
    """Verify resolve_api_key returns the key from <alert-triage-agent>/.<prefix>-api-key."""
    # A copy of scripts/, so the key file is not written into the checkout.
    import shutil
    pkg = tmp_path / "pkg" / "alert-triage-agent"
    shutil.copytree(SCENARIOS_DIR.parent, pkg / "scripts", ignore=shutil.ignore_patterns("inventory-pin.*.env"))
    key_file = pkg / ".ata-api-key"
    key_file.write_text("test-consumer-key-12345\n")

    test_sh = tmp_path / "test_key.sh"
    test_sh.write_text(f"""#!/usr/bin/env bash
set -e
source "{pkg}/scripts/scenarios/common.sh"
resolve_api_key
""")
    test_sh.chmod(0o755)

    env = os.environ.copy()
    env.pop("ATA_STACK_DIR", None)  # the default location, in the copy
    env.pop("ATA_API_KEY", None)
    env["MOCK_MODE"] = "false"
    env["DRY_RUN"] = "false"

    res = subprocess.run(
        [str(test_sh)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert res.returncode == 0
    assert "test-consumer-key-12345" in res.stdout


def test_verify_demo_infra_help():
    """Verify verify-demo-infra.sh --help displays options."""
    verify_script = Path(__file__).parent.parent / "scripts" / "verify-demo-infra.sh"
    res = subprocess.run(
        [str(verify_script), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "--mock" in res.stdout
    assert "--dry-run" in res.stdout


def test_verify_demo_infra_mock():
    """Verify verify-demo-infra.sh --mock passes all checks."""
    verify_script = Path(__file__).parent.parent / "scripts" / "verify-demo-infra.sh"
    res = subprocess.run(
        [str(verify_script), "--mock"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "Verifying ATA Demo Infrastructure & Endpoints (MOCK/DRY-RUN)" in res.stdout
    assert "Verification Complete: All Checks Passed" in res.stdout

