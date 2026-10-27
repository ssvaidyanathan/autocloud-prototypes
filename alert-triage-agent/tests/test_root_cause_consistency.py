"""Unit tests for Root Cause and Origin Consistency Rule (D10).

Rule:
1. The component named as the cause in the root-cause sentence MUST equal the origin field.
2. 'upstream dependency X' is used ONLY when X is the origin AND a dependency of the alerting resource.
"""

import pytest
from datetime import datetime, timezone
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier, EvidenceItem
from agent.app.specialists.synthesizer import SynthesizerAgent
from topology.topology_graph import get_upstream_dependencies, _canonicalize_node


@pytest.fixture
def synthesizer():
    return SynthesizerAgent()


def _extract_cause_component(root_cause_text: str) -> str:
    """Extract the primary resource component named as the cause in the root cause sentence."""
    import re
    # Check "at Apigee ingress proxy `{component}`"
    m = re.search(r"at Apigee ingress proxy [`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    # Check "Recent revision deployment ... on [upstream dependency ]`{component}`"
    m = re.search(r"Recent (?:revision )?deployment [`']?[^`'\s]+[`']? on (?:upstream dependency )?[`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    # Check "PostgreSQL ... on [upstream dependency ]`{component}`"
    m = re.search(r"PostgreSQL [^`]+ on (?:upstream dependency )?[`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    # Check "Cloud SQL ... on [upstream dependency ]`{component}`"
    m = re.search(r"Cloud SQL [^`]+ on (?:upstream dependency )?[`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    # Check "Upstream dependency degradation on `{component}`"
    m = re.search(r"Upstream dependency degradation on [`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    # Check "Service degradation on `{component}`"
    m = re.search(r"Service degradation on [`']?([^`'\s]+)[`']?", root_cause_text)
    if m:
        return m.group(1)

    return ""


def test_s1_backend_5xx_consistency(synthesizer):
    """S1: Alert on ata-inventory-api, origin ata-inventory-api.
    Component named MUST equal origin; 'upstream dependency' MUST NOT be used.
    """
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T07:52:06Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Elevated HTTP 5xx error rate detected: 15.2%"],
            metrics={"error_rate_5xx": "15.2%"},
            anomaly_detected=True,
        )
    ]
    origin = "ata-inventory-api"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "upstream dependency" not in text, f"'upstream dependency' must not be used when origin == alert resource: {text}"
    assert "HTTP 5xx error rate: 15.2%" in text


def test_s2_backend_latency_consistency(synthesizer):
    """S2: Alert on ata-orders-api, origin ata-inventory-api.
    Component named MUST equal origin; 'upstream dependency' MUST be used because ata-inventory-api is a dependency of ata-orders-api.
    Dominant signal must be latency (no 5xx citation).
    """
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-orders-latency",
        resource_name="ata-orders-api",
        started_at="2026-10-23T19:52:57Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Elevated latency detected: 6500ms"],
            metrics={"latency_p95_ms": 6500.0, "error_rate_5xx": "61.8%"},
            anomaly_detected=True,
        )
    ]
    origin = "ata-inventory-api"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "upstream dependency" in text.lower() and "ata-inventory-api" in text
    # Dominant signal must match latency policy, not 5xx
    assert "P95 latency: 6500ms" in text
    assert "5xx error rate" not in text


def test_s3_group2_apigee_429_consistency(synthesizer):
    """S3 Group 2: Alert on ata-orders-v1 (SpikeArrest 429), origin ata-orders-v1.
    Origin stays at gateway proxy; SpikeArrest rate limit identified; no 'upstream dependency'.
    """
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:27:14Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="ApigeeSpecialist",
            target_resource="ata-orders-v1",
            findings=["HTTP 429 rate limit enforced by SpikeArrest policy"],
            metrics={"status_429_rate": "100%"},
            anomaly_detected=True,
        )
    ]
    origin = "ata-orders-v1"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "upstream dependency" not in text
    assert "SpikeArrest rate-limit violations at Apigee ingress proxy" in text
    # No unevidenced traffic-volume claim.
    assert "traffic burst" not in text and "surge" not in text


def test_s4_cloudsql_consistency(synthesizer):
    """S4: Alert on ata-inventory-api, origin ata-inventory-db.
    Component named MUST equal origin; 'upstream dependency' used because ata-inventory-db is a dependency of ata-inventory-api.
    """
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:22:13Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["PostgreSQL connection pool saturation"],
            metrics={"active_connections": 25, "max_connections": 25, "active_lock_waits": 0},
            anomaly_detected=True,
        )
    ]
    origin = "ata-inventory-db"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "on upstream dependency `ata-inventory-db`" in text


def test_s5_bad_deploy_consistency(synthesizer):
    """S5: Alert on ata-inventory-api, origin ata-inventory-api with revision ata-inventory-api-00101-vw6.
    Origin = ata-inventory-api (NOT ata-inventory-db).
    Component named MUST equal origin; names revision, deploy time, and DB auth errors from app logs.
    'upstream dependency' MUST NOT be used.
    """
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:42:56Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-inventory-api",
            findings=["Recent revision deployment detected on `ata-inventory-api`: `ata-inventory-api-00101-vw6`."],
            metrics={
                "recent_revision": "ata-inventory-api-00101-vw6",
                "change_service": "ata-inventory-api",
                "previous_serving_revision": "ata-inventory-api-00070-6nq",
                "previous_serving_evidence": (
                    "Cloud Monitoring run.googleapis.com/request_count: ata-inventory-api-00070-6nq was the only "
                    "revision of ata-inventory-api that received requests before the deploy (120 requests)."
                ),
                "deploy_timestamp": "2026-10-23T13:40:00Z",
                "minutes_since_deploy": 2,
                "all_changes": [{
                    "service": "ata-inventory-api",
                    "revision": "ata-inventory-api-00101-vw6",
                    "timestamp": "2026-10-23T13:40:00Z",
                    "deployed_minutes_ago": 2,
                }],
            },
            anomaly_detected=True,
        ),
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Application error logs indicate database authentication failure."],
            metrics={
                "has_db_auth_error": True,
                "observed_symptom": "DB auth errors from the app logs",
                "error_rate_5xx": "45.0%",
            },
            anomaly_detected=True,
        ),
    ]
    origin = "ata-inventory-api"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "ata-inventory-api-00101-vw6" in text
    assert "database authentication failures in application logs" in text
    assert "upstream dependency" not in text, f"'upstream dependency' must not be used: {text}"

    # Verify observations cite DB auth failure symptom from application logs
    obs = synthesizer._synthesize_observations(dossiers, alert)
    assert "database authentication failure observed in application logs" in obs.lower()

    # Verify recommendations for S5
    actions = synthesizer._synthesize_recommendations(dossiers, origin, alert)
    # Rollback action naming previous revision
    rollback_action = actions[0]
    assert "ata-inventory-api-00070-6nq" in rollback_action.command
    assert "update-traffic" in rollback_action.command
    # Revision DB config check
    config_action = actions[1]
    assert "ata-inventory-api-00101-vw6" in config_action.command
    assert "revisions describe" in config_action.command
    # NO SQL connection-pool steps
    for a in actions:
        assert "pg_terminate_backend" not in a.command
        assert "max_connections=" not in a.command


def test_upstream_dependency_negative_rule(synthesizer):
    """Negative test: 'upstream dependency X' must NEVER be used when X is not an upstream dependency of alerting resource."""
    alert = AlertEvent(
        incident_id="0.test-inc-1",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T07:52:06Z",
    )
    # Even if origin is set to an unrelated service or self
    for candidate_origin in ["ata-inventory-api", "ata-orders-v1"]:
        dossiers = [
            EvidenceDossier(
                specialist_name="CloudRunSpecialist",
                target_resource=candidate_origin,
                findings=["Operational anomaly"],
                metrics={"error_rate_5xx": "10.0%"},
                anomaly_detected=True,
            )
        ]
        text, _ = synthesizer._determine_root_cause(dossiers, candidate_origin, alert)
        is_dep = _canonicalize_node(candidate_origin) in get_upstream_dependencies(_canonicalize_node(alert.resource_name))
        if not is_dep:
            assert "upstream dependency" not in text, f"Should not cite 'upstream dependency' for non-dependency {candidate_origin}: {text}"


def test_s4_cloudsql_na_locks_omits_lock_wait(synthesizer):
    """S4: When active_lock_waits is 'N/A (not captured)' and findings mention 'N/A: no lock wait log entries',
    has_locks MUST be False and root cause MUST assert connection pool saturation only,
    strictly omitting any lock wait contention clause.
    """
    alert = AlertEvent(
        incident_id="0.oe768id1cj1b",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-24T13:04:51Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=[
                "PostgreSQL connection pool saturation on ata-inventory-db: 24 of 25 connections in use.",
                "Active lock waits on ata-inventory-db is N/A: no lock wait log entries found in window [2026-10-24T12:04:51Z, 2026-10-24T13:04:51Z]",
            ],
            metrics={
                "active_connections": 24,
                "max_connections": 25,
                "active_lock_waits": "N/A (not captured)",
            },
            evidence_items=[
                EvidenceItem(
                    headline="Cloud SQL Connection Metrics: ata-inventory-db",
                    content="Connections: 24/25\nLock waits: N/A (not captured)",
                )
            ],
            anomaly_detected=True,
        )
    ]
    origin = "ata-inventory-db"
    text, conf = synthesizer._determine_root_cause(dossiers, origin, alert)

    cause_comp = _extract_cause_component(text)
    assert cause_comp == origin, f"Named cause component '{cause_comp}' must equal origin '{origin}'"
    assert "on upstream dependency `ata-inventory-db`" in text
    assert "connection pool saturation" in text.lower()
    assert "24/25 active connections" in text
    assert "lock" not in text.lower()
    assert "contention" not in text.lower()
    assert "n/a" not in text.lower()
    assert "not captured" not in text.lower()


@pytest.mark.asyncio
async def test_cloud_run_db_auth_error_detection_from_logs():
    """Verify CloudRunSpecialist detects DB auth/connection failure keywords in application logs."""
    from unittest.mock import MagicMock, patch
    from agent.app.specialists.cloud_run import CloudRunSpecialist
    from agent.app.config import AgentConfig

    spec = CloudRunSpecialist()
    alert = AlertEvent(
        incident_id="0.test-inc-s5",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-24T13:21:01Z",
    )
    config = AgentConfig(project_id="test-proj", region="us-west1", is_local=True)

    # Mock log entry containing "Failed to connect to Cloud SQL"
    mock_entry = MagicMock()
    mock_entry.log_name = "projects/test-proj/logs/run.googleapis.com%2Fstderr"
    mock_entry.payload = "Failed to connect to Cloud SQL: 'NoneType' object has no attribute 'decode'"
    mock_entry.text_payload = None
    mock_entry.http_request = None
    mock_entry.severity = "ERROR"
    mock_entry.timestamp = datetime.now(timezone.utc)

    with patch("google.cloud.logging.Client") as mock_log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_metric_cls:
        mock_log_client = MagicMock()
        mock_log_client.list_entries.return_value = [mock_entry]
        mock_log_cls.return_value = mock_log_client

        mock_metric_client = MagicMock()
        mock_metric_client.list_time_series.return_value = []
        mock_metric_cls.return_value = mock_metric_client

        dossier = await spec.investigate(alert, config)
        assert dossier.metrics.get("has_db_auth_error") is True
        assert any("database authentication" in f.lower() for f in dossier.findings)
        assert dossier.anomaly_detected is True

