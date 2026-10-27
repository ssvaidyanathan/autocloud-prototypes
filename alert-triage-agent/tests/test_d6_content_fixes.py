"""Unit tests for Task D6 Report Content Defects Fixes.

Covers:
1. Log excerpts exclude audit logs, principal emails, and caller IPs.
2. Log excerpts come strictly from application/request logs and format as one line <= 200 chars.
3. ChangeSpecialist and get_change_timeline detect service-level updates (Services.ReplaceService).
4. Normal metric values (e.g. database active connections 2 of 25) are described as normal, not threshold reached.
5. Problem bullet wording matches dominant evidence (5xx-dominant vs latency-dominant).
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import EvidenceItem
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.change import ChangeSpecialist
from agent.app.specialists.logs import (
    is_audit_log,
    is_application_or_request_log,
    sanitize_log_text,
    format_single_line_log_excerpt,
)
from agent.app.specialists.synthesizer import SynthesizerAgent
from tools.app.server import get_change_timeline


class DummyLogEntry:
    def __init__(
        self,
        log_name="",
        severity="ERROR",
        timestamp=None,
        http_request=None,
        payload=None,
    ):
        self.log_name = log_name
        self.severity = severity
        self.timestamp = timestamp or datetime(2026, 10, 23, 17, 16, 30, tzinfo=timezone.utc)
        self.http_request = http_request
        self.payload = payload


def test_audit_logs_and_emails_excluded_from_log_excerpts():
    """Assert that Cloud Audit Log entries and operator emails/IPs are strictly excluded/redacted."""
    audit_entry = DummyLogEntry(
        log_name="projects/cloud-assist-fde-2/logs/cloudaudit.googleapis.com%2Factivity",
        severity="NOTICE",
        payload={
            "@type": "type.googleapis.com/google.cloud.audit.AuditLog",
            "serviceName": "run.googleapis.com",
            "methodName": "google.cloud.run.v1.Services.ReplaceService",
            "authenticationInfo": {
                "principalEmail": "scion-fde-ops@deploy-demo-test.iam.gserviceaccount.com"
            },
            "requestMetadata": {
                "callerIp": "34.61.176.32",
                "callerSuppliedUserAgent": "gcloud/588.0.0",
            },
        },
    )

    assert is_audit_log(audit_entry) is True
    assert is_application_or_request_log(audit_entry) is False
    assert format_single_line_log_excerpt(audit_entry) is None


def test_clean_application_log_excerpt_formatting():
    """Assert that application/request logs format as a single clean line <= 200 characters."""
    valid_request_entry = DummyLogEntry(
        log_name="projects/cloud-assist-fde-2/logs/run.googleapis.com%2Frequests",
        severity="ERROR",
        http_request={
            "requestUrl": "https://ata-inventory-api.run.app/items/reserve",
            "status": 500,
            "latency": "250ms",
        },
        payload="Internal error reserving inventory item",
    )

    assert is_audit_log(valid_request_entry) is False
    assert is_application_or_request_log(valid_request_entry) is True

    excerpt = format_single_line_log_excerpt(valid_request_entry)
    assert excerpt is not None
    assert "\n" not in excerpt
    assert len(excerpt) <= 200
    assert "ERROR" in excerpt
    assert "HTTP 500" in excerpt
    assert "/items/reserve" in excerpt
    assert "Internal error reserving inventory item" in excerpt


def test_sanitization_removes_emails_and_ips_from_log_text():
    """Assert sanitize_log_text redacts email addresses and IPs."""
    dirty_text = "Failed call from scion-ops@internal.net host 10.240.0.42 to backend"
    sanitized = sanitize_log_text(dirty_text)
    assert "scion-ops@internal.net" not in sanitized
    assert "10.240.0.42" not in sanitized
    assert "[REDACTED_EMAIL]" in sanitized
    assert "[REDACTED_IP]" in sanitized


@pytest.mark.asyncio
async def test_change_specialist_detects_service_update():
    """Assert ChangeSpecialist identifies Services.ReplaceService configuration updates."""
    specialist = ChangeSpecialist()
    alert = AlertEvent(
        incident_id="test-change-001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:16:30Z",
    )
    config = AgentConfig(project_id="cloud-assist-fde-2", region="us-west1", is_local=True)

    fake_timeline = {
        "changes": [
            {
                "source": "cloud_run_service",
                "service_name": "ata-inventory-api",
                "revision_name": "ata-inventory-api-00051-cfg",
                "timestamp": "2026-10-23T17:04:04Z",
                "action": "Services.ReplaceService",
                "details": {"action": "Services.ReplaceService"},
            }
        ]
    }

    with patch("agent.app.specialists.change.fetch_change_timeline", return_value=fake_timeline):
        dossier = await specialist.investigate(alert, config)

    assert dossier.anomaly_detected is True
    assert any("Recent" in f and "detected on `ata-inventory-api`" in f for f in dossier.findings)
    assert "No configuration or revision deployments detected" not in " ".join(dossier.findings)


def test_normal_database_connections_described_as_normal():
    """Assert that baseline connection count (2 of 25) is described as normal, not a breach."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="test-sql-normal",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:16:30Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["Baseline healthy database"],
            metrics={"active_connections": 2, "max_connections": 25, "cpu_utilization_pct": 8.5},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=False,
            confidence=0.70,
        )
    ]

    obs = synth._synthesize_observations(dossiers, alert)
    assert "Database active connections reached 2 threshold" not in obs
    assert "Database active connections normal (2 of 25) on ata-inventory-db" in obs


def test_high_database_connections_described_as_threshold_reached():
    """Assert that connection count >= 80% of the measured limit is described as reaching it."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="test-sql-breach",
        policy_name="ata-cloudsql-connections-high",
        resource_name="ata-inventory-db",
        started_at="2026-10-23T13:20:00Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["Connection saturation"],
            metrics={"active_connections": 24, "max_connections": 25},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.90,
        )
    ]

    obs = synth._synthesize_observations(dossiers, alert)
    assert "Database active connections reached 24 of 25 on ata-inventory-db" in obs


def test_problem_bullet_wording_matches_5xx_dominant_evidence():
    """Assert that when 5xx rate is elevated, Problem statement specifies 5xx failure rate."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="test-5xx-dominant",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T17:16:30Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["5xx spike"],
            metrics={"error_rate_5xx": "82.8%", "latency_p95_ms": 2500.0},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.91,
        )
    ]

    problem = synth._synthesize_problem(
        dossiers=dossiers,
        origin="ata-inventory-api",
        alert=alert,
        blast_radius=["ata-inventory-api", "ata-inventory-v1"],
    )

    assert "Elevated HTTP 5xx error rate (82.8%)" in problem
    assert "Elevated service latency and request timeouts" not in problem


def test_problem_bullet_wording_matches_latency_dominant_evidence():
    """Assert that when latency is elevated without 5xx errors, Problem statement specifies latency."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="test-latency-dominant",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:13:00Z",
    )
    dossiers = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Latency spike"],
            metrics={"error_rate_5xx": "0.0%", "latency_p95_ms": 6500.0},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.91,
        )
    ]

    problem = synth._synthesize_problem(
        dossiers=dossiers,
        origin="ata-inventory-api",
        alert=alert,
        blast_radius=["ata-inventory-api", "ata-inventory-v1"],
    )

    assert "Elevated service latency and request timeouts (6500ms)" in problem
    assert "5xx failure rate" not in problem
