"""Unit tests validating single OKF storage path under knowledge/incident-manager/.

Asserts:
1. write_preanalysis_lock uploads strictly to knowledge/incident-manager/<file> in GCS with zero root bucket writes.
2. append_duplicate_alert_to_incident uploads strictly to knowledge/incident-manager/<file> in GCS with zero root bucket writes.
3. duplicate_checker pipeline and search confinement target knowledge/incident-manager/*.md.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest
import yaml

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.duplicate_checker import (
    append_duplicate_alert_to_incident,
    build_multi_grep_pipeline,
    check_for_duplicate,
)
from agent.app.triage import write_preanalysis_lock


@pytest.fixture
def sample_alert() -> AlertEvent:
    return AlertEvent(
        incident_id="incident-test-single-path-123",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-23T03:00:00Z",
    )


def test_write_preanalysis_lock_single_gcs_path(tmp_path: Path, sample_alert: AlertEvent):
    """Verify write_preanalysis_lock only writes to knowledge/incident-manager/<file> in GCS."""
    cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        incident_store_dir=str(tmp_path),
        is_local=False,
    )

    uploaded_blobs: list[str] = []

    mock_blob = MagicMock()
    mock_bucket = MagicMock()

    def mock_blob_factory(blob_name: str):
        uploaded_blobs.append(blob_name)
        blob = MagicMock()
        blob.name = blob_name
        return blob

    mock_bucket.blob.side_effect = mock_blob_factory

    mock_storage_client = MagicMock()
    mock_storage_client.bucket.return_value = mock_bucket

    with patch("google.cloud.storage.Client", return_value=mock_storage_client):
        file_path, group_id, filename = write_preanalysis_lock(sample_alert, cfg)

    assert file_path.exists()
    assert len(uploaded_blobs) == 1
    assert uploaded_blobs[0] == f"knowledge/incident-manager/{filename}"
    # Assert zero writes to root bucket path
    assert filename not in uploaded_blobs


def test_append_duplicate_alert_single_gcs_path(tmp_path: Path, sample_alert: AlertEvent):
    """Verify append_duplicate_alert_to_incident only writes to knowledge/incident-manager/<file> in GCS."""
    inc_file = tmp_path / "incident-test-single-path.md"
    frontmatter = {
        "type": "incident",
        "group_id": "grp_test_single_1",
        "policy_name": "ata-inventory-5xx",
        "resource_name": "ata-inventory-api",
        "status": "mitigation-in-progress",
        "timestamp": "2026-10-23T03:00:00Z",
        "origin": "ata-inventory-api",
        "servicenow_number": "INC0010999",
        "servicenow_sys_id": "sys_test999",
        "alerts": [
            {
                "incident_id": "incident-test-single-path-123",
                "policy": "ata-inventory-5xx",
                "resource": "ata-inventory-api",
                "state": "open",
                "started_at": "2026-10-23T03:00:00Z",
            }
        ],
    }
    inc_file.write_text(f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n# Test Report\n", encoding="utf-8")

    cfg = AgentConfig(
        project_id="cloud-assist-fde-2",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        incident_store_dir=str(tmp_path),
        is_local=False,
    )

    alert2 = AlertEvent(
        incident_id="incident-test-single-path-456",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        state="open",
        started_at="2026-10-23T03:05:00Z",
    )

    uploaded_blobs: list[str] = []

    mock_bucket = MagicMock()

    def mock_blob_factory(blob_name: str):
        uploaded_blobs.append(blob_name)
        blob = MagicMock()
        blob.name = blob_name
        return blob

    mock_bucket.blob.side_effect = mock_blob_factory

    mock_storage_client = MagicMock()
    mock_storage_client.bucket.return_value = mock_bucket

    with patch("google.cloud.storage.Client", return_value=mock_storage_client):
        updated_meta = append_duplicate_alert_to_incident(
            file_path=inc_file,
            alert=alert2,
            config=cfg,
        )

    assert updated_meta["timestamp"] == "2026-10-23T03:05:00Z"
    assert len(uploaded_blobs) == 1
    assert uploaded_blobs[0] == f"knowledge/incident-manager/{inc_file.name}"
    # Assert zero writes to root bucket path
    assert inc_file.name not in uploaded_blobs


def test_duplicate_checker_reads_single_knowledge_path(tmp_path: Path, sample_alert: AlertEvent):
    """Verify duplicate checker pipeline targets knowledge/incident-manager/*.md and reads file correctly."""
    cmd = build_multi_grep_pipeline(
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        store_path="/knowledge/incident-manager",
    )
    assert "/knowledge/incident-manager/*.md" in cmd

    # Test reading active file from directory
    inc_file = tmp_path / "incident-active-test.md"
    now_ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    frontmatter = {
        "type": "incident",
        "group_id": "grp_active_1",
        "policy_name": "ata-inventory-5xx",
        "resource_name": "ata-inventory-api",
        "status": "mitigation-in-progress",
        "timestamp": now_ts,
        "origin": "ata-inventory-api",
        "servicenow_number": "INC0010999",
        "servicenow_sys_id": "sys_test999",
        "alerts": [
            {
                "incident_id": sample_alert.incident_id,
                "policy": sample_alert.policy_name,
                "resource": sample_alert.resource_name,
                "state": "open",
                "started_at": now_ts,
            }
        ],
    }
    inc_file.write_text(f"---\n{yaml.dump(frontmatter, sort_keys=False)}---\n# Active Incident\n", encoding="utf-8")

    result = check_for_duplicate(
        policy_name=sample_alert.policy_name,
        resource_name=sample_alert.resource_name,
        store_path=str(tmp_path),
    )
    assert result.is_duplicate is True
    assert result.active_file == str(inc_file)
    assert result.existing_frontmatter["servicenow_number"] == "INC0010999"
