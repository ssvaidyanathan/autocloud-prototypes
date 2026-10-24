"""Unit tests for Apigee real telemetry and Synthesizer Apigee analytics.

Verifies:
1. ApigeeSpecialist extracts real telemetry from Cloud Monitoring & Cloud Logging without static values.
2. When telemetry is absent or below threshold, 'N/A (not captured)' is recorded.
3. Synthesizer _build_apigee_analytics builds dynamic response codes and latencies.
4. Remediation action items extract real client IP or use <CLIENT_IP>/32 placeholder (N1).
5. Zero fabricated numbers or hardcoded IP subnets exist in Apigee specialist paths.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch
import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent


@pytest.fixture
def test_config():
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        is_local=True,
    )


@pytest.mark.asyncio
async def test_apigee_specialist_without_live_data_records_na(test_config):
    """When no live telemetry is returned, ApigeeSpecialist records 'N/A (not captured)'."""
    specialist = ApigeeSpecialist()
    alert = AlertEvent(
        incident_id="0.test_429",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_mon_cls, \
         patch("google.cloud.logging.Client") as mock_log_cls:
        mock_mon = mock_mon_cls.return_value
        mock_mon.list_time_series.return_value = []
        mock_log = mock_log_cls.return_value
        mock_log.list_entries.return_value = []

        dossier = await specialist.investigate(alert, test_config)

        assert dossier.specialist_name == "ApigeeSpecialist"
        assert dossier.target_resource == "ata-orders-v1"
        assert dossier.anomaly_detected is True
        assert dossier.metrics["proxy_response_code_429"] == "N/A (not captured)"
        assert dossier.metrics["target_latency_p95_ms"] == "N/A (not captured)"
        assert dossier.metrics["target_latency_p50"] == "N/A (not captured)"
        assert dossier.metrics["top_client_ip"] == "<CLIENT_IP>/32"
        assert "N/A (not captured)" in dossier.evidence_items[0].content


@pytest.mark.asyncio
async def test_apigee_specialist_with_real_telemetry(test_config):
    """When live telemetry is captured, ApigeeSpecialist populates real counts, latencies, and client IP."""
    specialist = ApigeeSpecialist()
    alert = AlertEvent(
        incident_id="0.test_real_429",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        state="open",
        started_at="2026-10-23T04:00:00Z",
    )

    # Mock time series for 429
    mock_ts = MagicMock()
    mock_ts.metric.labels = {"response_code": "429", "fault_code": "policies.ratelimit.SpikeArrestViolation"}
    mock_pt = MagicMock()
    mock_pt.value.int64_value = 73
    mock_ts.points = [mock_pt]

    # Mock latency time series
    mock_lat_ts = MagicMock()
    mock_dist = MagicMock()
    mock_dist.count = 5
    mock_dist.mean = 28.4
    mock_lat_pt = MagicMock()
    mock_lat_pt.value.distribution_value = mock_dist
    mock_lat_ts.points = [mock_lat_pt]

    # Mock log entry with real client IP
    mock_entry = MagicMock()
    mock_entry.payload = {
        "status_code": "429",
        "client_ip": "203.0.113.42",
        "target_latency_ms": "28",
    }
    mock_entry.timestamp = datetime(2026, 10, 23, 4, 0, 10, tzinfo=timezone.utc)

    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mock_mon_cls, \
         patch("google.cloud.logging.Client") as mock_log_cls:
        mock_mon = mock_mon_cls.return_value

        def list_ts_side_effect(request):
            if "response_count" in request.filter:
                return [mock_ts]
            elif "latencies" in request.filter:
                return [mock_lat_ts]
            return []

        mock_mon.list_time_series.side_effect = list_ts_side_effect
        mock_log = mock_log_cls.return_value
        mock_log.list_entries.return_value = [mock_entry]

        dossier = await specialist.investigate(alert, test_config)

        assert dossier.metrics["proxy_response_code_429"] >= 73
        assert dossier.metrics["top_client_ip"] == "203.0.113.42"
        assert dossier.metrics["target_latency_p95_ms"] == 28
        assert "203.0.113.42" in dossier.evidence_items[0].content


def test_synthesizer_n1_dynamic_client_ip_extraction():
    """Verify remediation commands dynamically extract real client IP or use <CLIENT_IP>/32 placeholder."""
    synth = SynthesizerAgent()
    alert = AlertEvent(
        incident_id="0.test_n1",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        state="open",
    )

    # 1. With real extracted client IP
    dossier_with_ip = EvidenceDossier(
        specialist_name="ApigeeSpecialist",
        target_resource="ata-orders-v1",
        findings=[],
        metrics={"top_client_ip": "203.0.113.88"},
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
    )
    actions = synth._synthesize_recommendations([dossier_with_ip], "ata-orders-v1", alert)
    armor_action = next(a for a in actions if "security-policies" in a.command)
    assert "--src-ip-ranges=203.0.113.88/32" in armor_action.command
    assert "198.51.100.0" not in armor_action.command

    # 2. Without captured client IP (placeholder fallback)
    dossier_without_ip = EvidenceDossier(
        specialist_name="ApigeeSpecialist",
        target_resource="ata-orders-v1",
        findings=[],
        metrics={"top_client_ip": "<CLIENT_IP>/32"},
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
    )
    actions_fallback = synth._synthesize_recommendations([dossier_without_ip], "ata-orders-v1", alert)
    armor_action_fallback = next(a for a in actions_fallback if "security-policies" in a.command)
    assert "--src-ip-ranges=<CLIENT_IP>/32" in armor_action_fallback.command
    assert "198.51.100.0" not in armor_action_fallback.command


def test_synthesizer_build_apigee_analytics_dynamic_and_na():
    """Verify _build_apigee_analytics dynamically binds real metrics and falls back to N/A."""
    synth = SynthesizerAgent()

    # Case 1: Real captured analytics
    dossier_real = EvidenceDossier(
        specialist_name="ApigeeSpecialist",
        target_resource="ata-orders-v1",
        findings=[],
        metrics={
            "response_codes": {"200": 85, "429": 110},
            "target_latency_p50": "12ms",
            "target_latency_p95": "45ms",
            "target_latency_p99": "88ms",
        },
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
    )
    analytics = synth._build_apigee_analytics([dossier_real], ["ata-orders-v1"])
    assert analytics["proxy_name"] == "ata-orders-v1"
    assert analytics["response_codes"] == {"200": 85, "429": 110}
    assert analytics["target_latency_p50"] == "12ms"
    assert analytics["target_latency_p95"] == "45ms"
    assert analytics["target_latency_p99"] == "88ms"

    # Case 2: Telemetry absent / uncaptured
    dossier_empty = EvidenceDossier(
        specialist_name="ApigeeSpecialist",
        target_resource="ata-orders-v1",
        findings=[],
        metrics={},
        evidence_items=[],
        queries_executed={},
        anomaly_detected=True,
    )
    analytics_empty = synth._build_apigee_analytics([dossier_empty], ["ata-orders-v1"])
    assert analytics_empty["proxy_name"] == "ata-orders-v1"
    assert analytics_empty["target_latency_p50"] == "N/A (not captured)"
    assert analytics_empty["target_latency_p95"] == "N/A (not captured)"
    assert analytics_empty["target_latency_p99"] == "N/A (not captured)"
    # Zero static 1420, 284, 5400, 5800
    assert "1420" not in str(analytics_empty)
    assert "284" not in str(analytics_empty)
    assert "5400" not in str(analytics_empty)
    assert "5800" not in str(analytics_empty)


def test_no_fabricated_numbers_in_apigee_code():
    """Verify source code for apigee.py and synthesizer.py Apigee methods has zero fabricated numbers."""
    with open("agent/app/specialists/apigee.py", "r") as f:
        apigee_src = f.read()

    # None of the fabricated numbers should appear in apigee.py
    for forbidden in ["284", "145", "5400", "5800", "1420", "99.8%", "198.51.100"]:
        assert forbidden not in apigee_src, f"Found forbidden fabricated literal {forbidden} in apigee.py"

    with open("agent/app/specialists/synthesizer.py", "r") as f:
        synth_src = f.read()

    assert "198.51.100" not in synth_src, "Found hardcoded IP 198.51.100 in synthesizer.py"
