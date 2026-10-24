"""Unit tests for D4 Runtime Quality Fixes.

Covers:
1. Origin parity: root cause text always names the selected origin.
2. S2 correlation: ata-apigee-orders-5xx correlates to ata-inventory-api latency group (score >= 65.0).
3. Telemetry N/A logs reason at WARNING.
4. ServiceNow update_incident patches short_description and structured description.
"""

from datetime import datetime, timezone
import logging
from unittest.mock import MagicMock, patch
import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import ActionItem, EvidenceItem, InvestigationReport, ProbableRootCause
from agent.app.correlation.scoring import classify_anomaly_category, compute_multi_factor_score
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent
from bridge.app.adapters.servicenow import ServiceNowAdapter, ServiceNowIncidentRef
from bridge.app.config import BridgeConfig


def test_root_cause_names_selected_origin():
    """Assert that the probable root cause statement explicitly names the selected origin."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.oe5st3c3z3pt",
        policy_name="ata-apigee-orders-5xx",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T13:30:23Z",
    )

    dossiers = [
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            findings=["Latency spike observed on ata-inventory-api"],
            metrics={"error_rate_5xx": "12.5%", "metric_onset": "2026-10-23T13:24:57Z"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.85,
        ),
        EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-orders-api",
            findings=["Caller orders-api experiencing upstream timeouts"],
            metrics={"error_rate_5xx": "8.0%", "metric_onset": "2026-10-23T13:25:10Z"},
            evidence_items=[],
            queries_executed={},
            anomaly_detected=True,
            confidence=0.80,
        ),
    ]

    # Test when origin is upstream dependency
    selected_origin = "ata-inventory-api"
    text, conf = synth._determine_root_cause(
        dossiers=dossiers,
        origin=selected_origin,
        alert=alert,
        blast_radius=["ata-inventory-api", "ata-orders-api", "ata-orders-v1"],
    )

    assert selected_origin in text, f"Root cause text must name selected origin {selected_origin}: {text}"
    assert "ata-orders-api" not in text.split("on ")[1].split(" ")[0], "Root cause must not attribute origin to intermediate caller"


def test_s2_latency_to_gateway_5xx_correlation():
    """Assert that Apigee orders 5xx/504 alert correlates to inventory latency group with score >= 65.0."""
    anchor_time = datetime.fromisoformat("2026-10-23T13:13:08+00:00")
    alert_time = datetime.fromisoformat("2026-10-23T13:15:34+00:00")

    alert_cat = classify_anomaly_category("ata-apigee-orders-5xx")
    assert alert_cat == "gateway_5xx"

    breakdown = compute_multi_factor_score(
        alert_resource="ata-orders-v1",
        alert_category=alert_cat,
        alert_time=alert_time,
        group_resources=["ata-inventory-api"],
        group_category="latency_504",
        group_anchor_time=anchor_time,
        deployment_time=None,
    )

    # 2 hops with direct gateway timeout coupling -> 28.0 pts
    assert breakdown.topo_score == 28.0
    assert breakdown.causal_score == 30.0
    assert breakdown.time_score > 14.0
    # Total score must exceed auto-attach threshold (65.0)
    assert breakdown.total_score >= 65.0, f"Expected total score >= 65.0, got {breakdown.total_score}"


def test_absent_telemetry_logs_warning_in_cloud_run(caplog):
    """Assert that genuinely absent Cloud Run telemetry logs the reason at WARNING."""
    from agent.app.specialists.cloud_run import CloudRunSpecialist

    spec = CloudRunSpecialist()
    alert = AlertEvent(
        incident_id="test-no-data",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T13:00:00Z",
    )
    cfg = AgentConfig(project_id="test-proj")

    with caplog.at_level(logging.WARNING):
        # In test environment without GCP credentials, query calls will trigger WARNING logs for absent data
        import asyncio
        dossier = asyncio.run(spec.investigate(alert, cfg))

    assert dossier.metrics["error_rate_5xx"] == "N/A (not captured)"
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("HTTP 5xx error rate on ata-inventory-api is N/A" in r.message for r in warning_records)


def test_update_incident_patches_incident_fields_and_work_notes():
    """Assert that ServiceNow update_incident PATCHes short_description/description and appends executive summary."""
    cfg = BridgeConfig(
        project_id="test-proj",
        servicenow_instance_url="https://instance.service-now.com",
        servicenow_username="admin",
        servicenow_password="pwd",
    )
    adapter = ServiceNowAdapter(config=cfg)

    report = InvestigationReport(
        group_id="grp_test_s5",
        filename="incident-grp_test_s5.md",
        summary="Recent revision deployment ata-inventory-api-00049-7dh",
        observations="Observed deployment correlation",
        recommended_actions=["Rollback revision"],
        probable_root_cause=ProbableRootCause(
            text="Recent revision deployment ata-inventory-api-00049-7dh triggered failure.",
            confidence=0.80,
        ),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
    )

    mock_client = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_client.patch.return_value = mock_resp
    mock_client.post.return_value = mock_resp

    with patch.object(adapter, "_get_client", return_value=mock_client), \
         patch.object(adapter, "_resolve_caller_id", return_value="caller_sys_id"):
        ref = adapter.update_incident("sys_12345", report, number="INC0010220")

    assert ref.sys_id == "sys_12345"
    assert ref.number == "INC0010220"

    # Verify PATCH call was made to the incident table endpoint
    patch_calls = mock_client.patch.call_args_list
    assert len(patch_calls) >= 1
    endpoint, kwargs = patch_calls[0]
    assert "api/now/table/incident/sys_12345" in endpoint[0]
    assert "short_description" in kwargs["json"]
    assert "description" in kwargs["json"]

    # Verify work notes were appended via PATCH
    assert any("work_notes" in call[1].get("json", {}) for call in patch_calls)
