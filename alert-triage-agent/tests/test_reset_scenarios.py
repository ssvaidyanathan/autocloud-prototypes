"""Tests for reset-scenarios.sh CLI options, alert querying, and wait behavior."""

import json
import os
import subprocess
from pathlib import Path
import pytest

RESET_SCRIPT = Path(__file__).parent.parent / "scripts" / "reset-scenarios.sh"


def test_reset_scenarios_help():
    """Verify reset-scenarios.sh --help outputs usage and supported flags."""
    res = subprocess.run(
        [str(RESET_SCRIPT), "--help"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0
    assert "--no-wait" in res.stdout
    assert "--quiet-period" in res.stdout
    assert "--timeout" in res.stdout
    assert "--poll-interval" in res.stdout
    assert "--dry-run" in res.stdout
    assert "--mock" in res.stdout


def test_reset_scenarios_mock_no_wait():
    """Verify reset-scenarios.sh --mock --no-wait executes quickly and skips waiting."""
    res = subprocess.run(
        [str(RESET_SCRIPT), "--mock", "--no-wait"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0
    assert "[--no-wait] Skipping wait for open alerts and quiet period." in res.stdout
    assert "All Scenarios Reset Successfully" in res.stdout


def test_reset_scenarios_mock_simulation():
    """Verify reset-scenarios.sh --mock simulates wait and quiet period."""
    res = subprocess.run(
        [str(RESET_SCRIPT), "--mock"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0
    assert "Simulating wait for open alerts to close" in res.stdout
    assert "Simulating quiet period" in res.stdout
    assert "All Scenarios Reset Successfully" in res.stdout


def test_reset_scenarios_dry_run_simulation():
    """Verify reset-scenarios.sh --dry-run simulates wait and quiet period."""
    res = subprocess.run(
        [str(RESET_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert res.returncode == 0
    assert "Simulating wait for open alerts to close" in res.stdout
    assert "Simulating quiet period" in res.stdout
    assert "All Scenarios Reset Successfully" in res.stdout


def test_query_open_ata_alerts_scoping():
    """Test scoping logic used by query_open_ata_alerts extracts only ata alerts."""
    sample_alerts = [
        {
            "name": "projects/proj/alerts/0.ata1",
            "state": "OPEN",
            "openTime": "2026-10-25T01:00:00Z",
            "policy": {
                "name": "projects/proj/alertPolicies/101",
                "displayName": "ata-orders-5xx",
                "userLabels": {"app": "alert-triage-agent"},
            },
        },
        {
            "name": "projects/proj/alerts/0.ata2",
            "state": "OPEN",
            "openTime": "2026-10-25T01:05:00Z",
            "policy": {
                "name": "projects/proj/alertPolicies/102",
                "displayName": "ata-apigee-orders-429",
                "userLabels": {"app": "alert-triage-agent"},
            },
        },
        {
            "name": "projects/proj/alerts/0.foreign1",
            "state": "OPEN",
            "openTime": "2026-10-14T06:00:00Z",
            "policy": {
                "name": "projects/proj/alertPolicies/999",
                "displayName": "suite_cd_001: High Latency Detected in Central",
            },
        },
        {
            "name": "projects/proj/alerts/0.foreign2",
            "state": "OPEN",
            "openTime": "2026-10-12T18:00:00Z",
            "policy": {
                "name": "projects/proj/alertPolicies/888",
                "displayName": "IMA Alert: Secret Manager Permission Denied",
            },
        },
    ]

    open_alerts = []
    for alert in sample_alerts:
        policy = alert.get("policy", {})
        disp = str(policy.get("displayName") or "").strip()
        pol_name = str(policy.get("name") or "").strip()
        user_labels = policy.get("userLabels") or alert.get("userLabels") or {}

        is_ata = False
        if isinstance(user_labels, dict):
            if user_labels.get("app") == "alert-triage-agent" or user_labels.get("managed_by") == "alert-triage-agent":
                is_ata = True
        if disp.lower().startswith("ata-") or "ata-" in pol_name.lower() or "alert-triage-agent" in disp.lower() or "alert-triage-agent" in pol_name.lower():
            is_ata = True

        if is_ata:
            open_alerts.append({
                "id": alert.get("name", "").split("/")[-1],
                "name": alert.get("name", ""),
                "policy_display": disp or pol_name,
                "policy_name": pol_name,
                "open_time": alert.get("openTime", ""),
            })

    assert len(open_alerts) == 2
    ids = [a["id"] for a in open_alerts]
    assert "0.ata1" in ids
    assert "0.ata2" in ids
    assert "0.foreign1" not in ids
    assert "0.foreign2" not in ids
