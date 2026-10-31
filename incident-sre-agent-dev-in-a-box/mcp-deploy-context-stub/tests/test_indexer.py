"""Unit tests for DeploymentContextIndexer."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.indexer import DeploymentContextIndexer
from app.models import DeploymentEvent


def test_indexer_lookup_by_check_id(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify lookup by monitoring check ID."""
    res = populated_indexer.lookup_alert_context(check_id="sample-check-1")
    assert res.found is True
    assert res.context is not None
    assert res.context.service_name == "sample-workload"
    assert res.context.cluster_name == "customer-cluster-1"


def test_indexer_lookup_by_target_ip(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify lookup by target IP address."""
    res = populated_indexer.lookup_alert_context(target_ip="10.10.0.1")
    assert res.found is True
    assert res.context is not None
    assert res.context.workload_name == "sample-workload"


def test_indexer_lookup_by_service_hint(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify lookup by service hint and case-insensitivity."""
    res = populated_indexer.lookup_alert_context(service_hint="SAMPLE-WORKLOAD")
    assert res.found is True
    assert res.context is not None
    assert res.context.service_name == "sample-workload"


def test_indexer_lookup_unmatched_returns_empty(
    populated_indexer: DeploymentContextIndexer,
) -> None:
    """Verify unmatched lookup returns found=False with context=None."""
    res = populated_indexer.lookup_alert_context(
        target_ip="192.168.1.99",
        check_id="non-existent-check",
        service_hint="unknown-service",
    )
    assert res.found is False
    assert res.context is None


def test_indexer_get_recent_deployments(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify deployment query filtering by workload and cluster."""
    # Match workload
    res = populated_indexer.get_recent_deployments(workload="sample-workload")
    assert res.total_count == 1
    assert len(res.deployments) == 1
    assert res.deployments[0].event_id == "evt-helm-deploy-089"
    assert res.deployments[0].revision == "89"

    # Match cluster
    res2 = populated_indexer.get_recent_deployments(cluster="customer-cluster-1")
    assert res2.total_count == 1

    # Unmatched query returns empty
    res_empty = populated_indexer.get_recent_deployments(workload="unknown-workload")
    assert res_empty.total_count == 0
    assert res_empty.deployments == []


def test_indexer_sorting_and_pagination() -> None:
    """Verify deployments are sorted newest first and obey pagination limits."""
    idx = DeploymentContextIndexer()
    for i in range(10):
        minute = f"{i:02d}"
        idx.add_deployment_event(
            DeploymentEvent(
                event_id=f"evt-{i}",
                timestamp=f"2026-10-27T01:{minute}:00Z",
                workload_name="multi-rollout",
                cluster_name="customer-cluster-1",
                revision=str(i),
            )
        )

    # Query with limit 3
    res = idx.get_recent_deployments(workload="multi-rollout", limit=3)
    assert res.total_count == 10
    assert len(res.deployments) == 3
    # Newest timestamp should be first
    assert res.deployments[0].event_id == "evt-9"
    assert res.deployments[1].event_id == "evt-8"
    assert res.deployments[2].event_id == "evt-7"


def test_load_bundled_fixtures(bundled_fixtures_path: Path) -> None:
    """Verify loading from the bundled fixtures directory."""
    idx = DeploymentContextIndexer()
    idx.load_directory(bundled_fixtures_path)

    # Verify scenario-helm-rollout is indexed
    res = idx.lookup_alert_context(check_id="sample-check-1")
    assert res.found is True
    assert res.context is not None
    assert res.context.service_name == "sample-workload"

    deps = idx.get_recent_deployments(workload="sample-workload")
    assert deps.total_count >= 2
    assert deps.deployments[0].revision == "89"


def test_load_gcs_uri_mocked() -> None:
    """Verify GCS synchronization using mocked Google Cloud Storage client."""
    idx = DeploymentContextIndexer()

    mock_blob = MagicMock()
    mock_blob.name = "scenario-mock.json"
    mock_blob.download_as_text.return_value = json.dumps(
        {
            "alert_contexts": [
                {
                    "service_name": "gcs-workload",
                    "cluster_name": "gcs-cluster",
                    "namespace": "gcs-ns",
                    "workload_name": "gcs-workload",
                    "owner_team": "gcs-team",
                    "check_id": "gcs-check-123",
                }
            ],
            "deployments": [
                {
                    "event_id": "evt-gcs-1",
                    "timestamp": "2026-10-27T03:00:00Z",
                    "workload_name": "gcs-workload",
                    "cluster_name": "gcs-cluster",
                    "revision": "gcs-rev-1",
                }
            ],
        }
    )

    mock_bucket = MagicMock()
    mock_bucket.list_blobs.return_value = [mock_blob]

    mock_client = MagicMock()
    mock_client.bucket.return_value = mock_bucket

    with patch("google.cloud.storage.Client", return_value=mock_client):
        idx.load_gcs_uri("gs://test-bucket/eval-fixtures/")

    res = idx.lookup_alert_context(check_id="gcs-check-123")
    assert res.found is True
    assert res.context is not None
    assert res.context.service_name == "gcs-workload"

    deps = idx.get_recent_deployments(workload="gcs-workload")
    assert deps.total_count == 1
    assert deps.deployments[0].event_id == "evt-gcs-1"


def test_indexer_chronological_timestamp_comparison() -> None:
    """Verify start_time and end_time filter correctly across timezone formats and offsets."""
    idx = DeploymentContextIndexer()
    idx.add_deployment_event(
        DeploymentEvent(
            event_id="evt-early",
            timestamp="2026-10-27T01:00:00Z",
            workload_name="time-test",
            revision="1",
        )
    )
    idx.add_deployment_event(
        DeploymentEvent(
            event_id="evt-mid",
            timestamp="2026-10-27T01:30:00+00:00",
            workload_name="time-test",
            revision="2",
        )
    )
    idx.add_deployment_event(
        DeploymentEvent(
            event_id="evt-late",
            timestamp="2026-10-27T02:00:00Z",
            workload_name="time-test",
            revision="3",
        )
    )

    res = idx.get_recent_deployments(
        workload="time-test",
        start_time="2026-10-27T01:15:00+00:00",
        end_time="2026-10-27T01:45:00Z",
    )
    assert res.total_count == 1
    assert res.deployments[0].event_id == "evt-mid"
