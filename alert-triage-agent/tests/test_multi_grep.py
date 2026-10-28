"""Unit tests for the multi-grep duplicate pipeline and 30m stale lock detection."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
import yaml

from agent.app.duplicate_checker import (
    build_multi_grep_pipeline,
    check_for_duplicate,
    DuplicateCheckResult,
)


@pytest.fixture
def temp_incident_dir(tmp_path):
    store = tmp_path / "knowledge" / "incident-manager"
    store.mkdir(parents=True, exist_ok=True)
    return store


def test_multi_grep_pipeline_escaping():
    """Verify that quotes and metacharacters are escaped safely in multi-grep command."""
    pol = 'ata-5xx-"critical"-alert'
    res = "ata-inventory-'db'\\special"
    cmd = build_multi_grep_pipeline(pol, res, store_path="/knowledge/incident-manager")

    assert "grep -FlZ" in cmd
    assert "xargs -r0" in cmd
    assert "status: (mitigation-in-progress|ticketed|investigating)" in cmd
    # Verify no unescaped single quotes that would break the shell pipeline
    assert "\\'" in cmd or "'\\''" in cmd


def test_active_duplicate_within_30_minutes(temp_incident_dir):
    """Verify that an incident record created 10 minutes ago is flagged as an active duplicate."""
    now = datetime(2026, 10, 22, 22, 0, 0, tzinfo=timezone.utc)
    started_at = (now - timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%SZ")

    inc_file = temp_incident_dir / "incident-backend-5xx.md"
    frontmatter = {
        "type": "incident",
        "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        "resource_name": "ata-inventory-api",
        "status": "mitigation-in-progress",
        "timestamp": started_at,
        "origin": "ata-inventory-api",
    }
    content = f"---\n{yaml.dump(frontmatter)}---\n# Incident\n"
    inc_file.write_text(content, encoding="utf-8")

    res = check_for_duplicate(
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        resource_name="ata-inventory-api",
        store_path=str(temp_incident_dir),
        now=now,
    )

    assert res.is_duplicate is True
    assert res.status == "ACTIVE_DUPLICATE_PROCESSING"
    assert len(res.matching_files) == 1
    assert str(inc_file) in res.matching_files[0]


def test_stale_lock_older_than_30_minutes_bypassed(temp_incident_dir, caplog):
    """Verify that an incident record created 45 minutes ago is treated as a stale lock and ignored."""
    now = datetime(2026, 10, 22, 22, 0, 0, tzinfo=timezone.utc)
    started_at = (now - timedelta(minutes=45)).strftime("%Y-%m-%dT%H:%M:%SZ")

    inc_file = temp_incident_dir / "incident-crashed-session.md"
    frontmatter = {
        "type": "incident",
        "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        "resource_name": "ata-inventory-api",
        "status": "mitigation-in-progress",
        "timestamp": started_at,
        "origin": "ata-inventory-api",
    }
    content = f"---\n{yaml.dump(frontmatter)}---\n# Incident\n"
    inc_file.write_text(content, encoding="utf-8")

    res = check_for_duplicate(
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        resource_name="ata-inventory-api",
        store_path=str(temp_incident_dir),
        now=now,
    )

    assert res.is_duplicate is False
    assert res.status == "STALE_LOCK_BYPASSED"
    assert len(res.stale_files) == 1
    assert str(inc_file) in res.stale_files[0]
    assert "stale incident lock" in caplog.text.lower()


def test_no_match_returns_false(temp_incident_dir):
    """Verify that non-matching policy or resource returns is_duplicate: False."""
    res = check_for_duplicate(
        policy_name="ata-nonexistent-policy",
        resource_name="ata-nonexistent-resource",
        store_path=str(temp_incident_dir),
    )
    assert res.is_duplicate is False
    assert res.status == "NO_DUPLICATE"


def test_active_duplicate_with_ticketed_status(temp_incident_dir):
    """Verify that an incident with status: ticketed within 30m is detected as an active duplicate."""
    now = datetime(2026, 10, 23, 0, 19, 0, tzinfo=timezone.utc)
    started_at = (now - timedelta(minutes=6)).strftime("%Y-%m-%dT%H:%M:%SZ")

    inc_file = temp_incident_dir / "incident-backend-5xx.md"
    frontmatter = {
        "type": "incident",
        "policy_name": "projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        "resource_name": "ata-inventory-api",
        "status": "ticketed",
        "servicenow_number": "INC0010148",
        "servicenow_sys_id": "sn-sys-123",
        "timestamp": started_at,
        "origin": "ata-inventory-api",
    }
    content = f"---\n{yaml.dump(frontmatter)}---\n# Incident\n"
    inc_file.write_text(content, encoding="utf-8")

    res = check_for_duplicate(
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-backend-5xx",
        resource_name="ata-inventory-api",
        store_path=str(temp_incident_dir),
        now=now,
    )

    assert res.is_duplicate is True
    assert res.status == "ACTIVE_DUPLICATE_PROCESSING"
    assert len(res.matching_files) == 1
    assert str(inc_file) in res.matching_files[0]


def test_active_duplicate_with_investigating_status(temp_incident_dir):
    """Verify that an incident with status: investigating within 30m is detected as an active duplicate."""
    now = datetime(2026, 10, 23, 0, 15, 0, tzinfo=timezone.utc)
    started_at = (now - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%SZ")

    inc_file = temp_incident_dir / "incident-investigating.md"
    frontmatter = {
        "type": "incident",
        "policy_name": "ata-inventory-5xx",
        "resource_name": "ata-inventory-api",
        "status": "investigating",
        "timestamp": started_at,
        "origin": "ata-inventory-api",
    }
    content = f"---\n{yaml.dump(frontmatter)}---\n# Incident\n"
    inc_file.write_text(content, encoding="utf-8")

    res = check_for_duplicate(
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        store_path=str(temp_incident_dir),
        now=now,
    )

    assert res.is_duplicate is True
    assert res.status == "ACTIVE_DUPLICATE_PROCESSING"
    assert len(res.matching_files) == 1


def test_append_duplicate_alert_preserves_ticket_and_refreshes_timestamp(temp_incident_dir):
    """Verify append_duplicate_alert_to_incident refreshes timestamp and preserves existing ServiceNow ticket."""
    from agent.app.contracts.alert import AlertEvent
    from agent.app.duplicate_checker import append_duplicate_alert_to_incident

    t0 = "2026-10-23T00:13:00Z"
    t1 = "2026-10-23T00:19:00Z"

    inc_file = temp_incident_dir / "incident-ata-orders-5xx.md"
    initial_frontmatter = {
        "type": "incident",
        "group_id": "grp_orders_1",
        "policy_name": "ata-orders-5xx",
        "resource_name": "ata-orders-api",
        "status": "mitigation-in-progress",
        "timestamp": t0,
        "origin": "ata-orders-api",
        "servicenow_number": "INC0010148",
        "servicenow_sys_id": "sys_abc123",
        "alerts": [
            {
                "incident_id": "alert-1",
                "policy": "ata-orders-5xx",
                "resource": "ata-orders-api",
                "state": "open",
                "started_at": t0,
            }
        ],
    }
    inc_file.write_text(f"---\n{yaml.dump(initial_frontmatter, sort_keys=False)}---\n# Initial Report\n", encoding="utf-8")

    alert2 = AlertEvent(
        incident_id="alert-2",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        state="open",
        started_at=t1,
    )

    updated_meta = append_duplicate_alert_to_incident(
        file_path=inc_file,
        alert=alert2,
    )

    assert updated_meta["timestamp"] == t1
    assert updated_meta["servicenow_number"] == "INC0010148"
    assert updated_meta["servicenow_sys_id"] == "sys_abc123"
    assert len(updated_meta["alerts"]) == 2
    assert updated_meta["alerts"][1]["incident_id"] == "alert-2"
    assert updated_meta["alerts"][1]["started_at"] == t1

    # Check file content on disk
    file_content = inc_file.read_text(encoding="utf-8")
    assert "Duplicate Alert Appended" in file_content
    assert "alert-2" in file_content

