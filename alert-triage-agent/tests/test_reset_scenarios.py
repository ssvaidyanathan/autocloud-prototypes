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
    assert "--max-alert-age" in res.stdout
    assert "--serving-revisions" in res.stdout
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


def test_query_open_ata_alerts_filters_non_serving_and_stale():
    """Verify that alerts on non-serving revisions and alerts older than max age are ignored."""
    from datetime import datetime, timezone

    reset_start_time = 1793050000.0  # reference epoch
    max_alert_age_secs = 3600.0      # 60m
    serving_revs_set = {"ata-inventory-api-00070-6nq", "ata-orders-api-00010-abc"}

    alerts = [
        # 1. Active alert: on serving revision, opened 5m ago
        {
            "name": "projects/proj/alerts/0.active1",
            "state": "OPEN",
            "openTime": datetime.fromtimestamp(reset_start_time - 300, tz=timezone.utc).isoformat(),
            "policy": {"displayName": "ata-inventory-latency", "userLabels": {"app": "alert-triage-agent"}},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"revision_name": "ata-inventory-api-00070-6nq"},
            },
        },
        # 2. Stale alert on retired revision (e.g. 00033-8w6 from 2 hours ago)
        {
            "name": "projects/proj/alerts/0.retired1",
            "state": "OPEN",
            "openTime": datetime.fromtimestamp(reset_start_time - 7200, tz=timezone.utc).isoformat(),
            "policy": {"displayName": "ata-inventory-latency", "userLabels": {"app": "alert-triage-agent"}},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"revision_name": "ata-inventory-api-00033-8w6"},
            },
        },
        # 3. Recent alert but on non-serving revision (e.g. 00034 opened 2m ago)
        {
            "name": "projects/proj/alerts/0.retired2",
            "state": "OPEN",
            "openTime": datetime.fromtimestamp(reset_start_time - 120, tz=timezone.utc).isoformat(),
            "policy": {"displayName": "ata-inventory-5xx", "userLabels": {"app": "alert-triage-agent"}},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"revision_name": "ata-inventory-api-00034-old"},
            },
        },
        # 4. Stale alert on serving revision (opened 90m ago)
        {
            "name": "projects/proj/alerts/0.stale1",
            "state": "OPEN",
            "openTime": datetime.fromtimestamp(reset_start_time - 5400, tz=timezone.utc).isoformat(),
            "policy": {"displayName": "ata-orders-latency", "userLabels": {"app": "alert-triage-agent"}},
            "resource": {
                "type": "cloud_run_revision",
                "labels": {"revision_name": "ata-orders-api-00010-abc"},
            },
        },
    ]

    active = []
    ignored = []

    for alert in alerts:
        policy = alert.get("policy", {})
        disp = str(policy.get("displayName") or "").strip()
        alert_id = alert.get("name", "").split("/")[-1]
        rev = alert.get("resource", {}).get("labels", {}).get("revision_name")
        open_time = alert.get("openTime")
        open_epoch = datetime.fromisoformat(open_time).timestamp()

        ignore_reasons = []
        if rev and serving_revs_set and rev not in serving_revs_set:
            ignore_reasons.append(f"Non-serving revision ({rev})")

        cutoff = reset_start_time - max_alert_age_secs
        if open_epoch < cutoff:
            ignore_reasons.append(f"Opened > 60m before reset")

        entry = {"id": alert_id, "policy_display": disp, "revision": rev}
        if ignore_reasons:
            entry["ignore_reasons"] = ignore_reasons
            ignored.append(entry)
        else:
            active.append(entry)

    assert len(active) == 1
    assert active[0]["id"] == "0.active1"
    assert len(ignored) == 3
    ignored_ids = [x["id"] for x in ignored]
    assert "0.retired1" in ignored_ids
    assert "0.retired2" in ignored_ids
    assert "0.stale1" in ignored_ids

