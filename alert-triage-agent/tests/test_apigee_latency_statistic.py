"""Apigee target latency: percentiles are taken over one well-defined population and labelled.

Before: each per-minute target/latencies mean was repeated min(count, 10) times and mixed with
per-request access-log latencies, and the result was reported as "P95". That is neither a
request percentile nor a percentile of means. Now:
- access-log latencies present -> percentiles over those logged requests only;
- otherwise -> percentiles over the per-minute means (each minute once);
- the basis is recorded (`target_latency_basis`) and shown wherever the value is labelled.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.apigee import ApigeeSpecialist, apply_gateway_timeout_gap, target_latency_summary
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent


@pytest.fixture
def test_config():
    return AgentConfig(project_id="cloud-assist-fde-2", name_prefix="ata", region="us-west1", is_local=True)


def _dist_ts(points: list[tuple[int, float]]) -> MagicMock:
    ts = MagicMock()
    ts.metric.labels = {}
    pts = []
    for count, mean in points:
        dist = MagicMock()
        dist.count = count
        dist.mean = mean
        pt = MagicMock()
        pt.value.distribution_value = dist
        pts.append(pt)
    ts.points = pts
    return ts


def _log_entry(latency_ms: str, status: str = "504") -> MagicMock:
    e = MagicMock()
    e.payload = {"status_code": status, "client_ip": "203.0.113.42", "target_latency_ms": latency_ms}
    e.timestamp = datetime(2026, 10, 27, 12, 30, 0, tzinfo=timezone.utc)
    return e


async def _investigate(config, lat_points, log_entries, policy="ata-apigee-inventory-5xx"):
    alert = AlertEvent(
        incident_id="0.lat", policy_name=policy, resource_name="ata-inventory-v1",
        state="open", started_at="2026-10-27T12:29:23Z",
    )
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         patch("google.cloud.logging.Client") as log_cls:
        def side_effect(request):
            if "latencies" in request.filter:
                return [_dist_ts(lat_points)] if lat_points else []
            return []
        mon_cls.return_value.list_time_series.side_effect = side_effect
        log_cls.return_value.list_entries.return_value = log_entries
        return await ApigeeSpecialist().investigate(alert, config)


# --- target_latency_summary (pure) ---------------------------------------------------------

def test_summary_uses_logged_requests_only_when_present():
    # 19 fast requests + 1 slow one; a slow per-minute mean must not leak into the percentiles.
    logged = [10.0] * 19 + [6000.0]
    s = target_latency_summary(logged, [5000.0, 5000.0])
    assert s["p50_ms"] == 10
    assert s["p95_ms"] == 6000
    assert s["basis"] == "percentiles of 20 logged requests (ata-apigee-access)"


def test_summary_falls_back_to_minute_means_each_counted_once():
    # Old code repeated the 10-request minute 10x, so P50 was 6500. Each minute once: P50 of 3 means.
    s = target_latency_summary([], [20.0, 30.0, 6500.0])
    assert s["p50_ms"] == 30
    assert s["p95_ms"] == 6500
    assert s["basis"] == "percentiles of 3 per-minute means (apigee.googleapis.com/target/latencies)"


def test_summary_none_without_data():
    assert target_latency_summary([], []) is None


def test_summary_singular_wording():
    assert target_latency_summary([42.0], [])["basis"] == "percentiles of 1 logged request (ata-apigee-access)"
    assert target_latency_summary([], [42.0])["basis"] == (
        "percentiles of 1 per-minute mean (apigee.googleapis.com/target/latencies)"
    )


# --- ApigeeSpecialist ----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_specialist_does_not_mix_means_with_logged_requests(test_config):
    # Old code: means 5000 x10 + 2 logged 20ms -> "P50" 5000. Logged requests alone give 20.
    dossier = await _investigate(test_config, [(50, 5000.0)], [_log_entry("20"), _log_entry("20")])
    assert dossier.metrics["target_latency_p50"] == "20ms"
    assert dossier.metrics["target_latency_p95_ms"] == 20
    assert dossier.metrics["target_latency_basis"] == "percentiles of 2 logged requests (ata-apigee-access)"
    content = dossier.evidence_items[0].content
    assert "Target Latency (P95 of 2 logged requests): 20ms" in content
    assert "(P95)" not in content


@pytest.mark.asyncio
async def test_specialist_metric_only_labels_minute_means(test_config):
    dossier = await _investigate(test_config, [(10, 6500.0), (3, 20.0)], [])
    assert dossier.metrics["target_latency_basis"] == (
        "percentiles of 2 per-minute means (apigee.googleapis.com/target/latencies)"
    )
    assert "Target Latency (P95 of 2 per-minute means): 6500ms" in dossier.evidence_items[0].content


@pytest.mark.asyncio
async def test_specialist_no_data_has_no_basis(test_config):
    dossier = await _investigate(test_config, [], [])
    assert "target_latency_basis" not in dossier.metrics
    assert dossier.metrics["target_latency_p95"] == "N/A (not captured)"
    assert "Target Latency: N/A (not captured)" in dossier.evidence_items[0].content


# --- Synthesizer ---------------------------------------------------------------------------

def _apigee_dossier(metrics: dict) -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="ApigeeSpecialist", target_resource="ata-inventory-v1", findings=[],
        metrics=metrics, evidence_items=[], queries_executed={}, anomaly_detected=True,
    )


def _apigee_latency_row(metrics: dict) -> dict:
    rows = SynthesizerAgent()._build_metrics_comparison([_apigee_dossier(metrics)], "ata-inventory-api")
    return next(r for r in rows if r["layer"] == "Apigee")


def test_metrics_row_names_the_basis_and_never_claims_plain_p95():
    row = _apigee_latency_row({
        "target_latency_p95_ms": 6547,
        "target_latency_basis": "percentiles of 357 logged requests (ata-apigee-access)",
    })
    assert row["metric_name"] == "Apigee target latency, P95 (percentiles of 357 logged requests (ata-apigee-access))"
    assert row["during_value"] == "6547ms"
    # No baseline was measured, so no variance is claimed.
    assert row["variance"] == "N/A (no baseline captured)"


def test_metrics_row_timeout_only_with_observed_gateway_timeout():
    row = _apigee_latency_row({
        "target_latency_p95_ms": 6547,
        "target_latency_basis": "percentiles of 357 logged requests (ata-apigee-access)",
        "gateway_timeout": {"count": 357, "fault_code": "messaging.adaptors.http.flow.GatewayTimeout"},
    })
    assert row["during_value"] == "6547ms (GatewayTimeout observed)"


def test_metrics_row_without_basis_does_not_say_p95():
    row = _apigee_latency_row({"target_latency_p95_ms": "N/A (not captured)"})
    assert "P95" not in row["metric_name"]
    assert row["during_value"] == "N/A (not captured)"


def test_analytics_carries_basis():
    analytics = SynthesizerAgent()._build_apigee_analytics([_apigee_dossier({
        "response_codes": {"504": 357},
        "target_latency_p50": "6500ms", "target_latency_p95": "6547ms", "target_latency_p99": "6600ms",
        "target_latency_basis": "percentiles of 357 logged requests (ata-apigee-access)",
    })], ["ata-inventory-v1"])
    assert analytics["target_latency_basis"] == "percentiles of 357 logged requests (ata-apigee-access)"


def _render(analytics: dict) -> str:
    from bridge.app.contracts.report import InvestigationReport
    from bridge.app.templates.renderer import render_standalone_html

    report = InvestigationReport(
        group_id="grp-apigee-lat", summary="Elevated latency", observations="obs",
        origin="ata-inventory-api", blast_radius=["ata-inventory-v1"], priority="P2",
        project_id="cloud-assist-fde-2", region="us-west1", apigee_analytics=analytics,
    )
    return render_standalone_html(report)


def test_html_shows_basis_next_to_percentiles():
    html = _render({
        "proxy_name": "ata-inventory-v1", "response_codes": {"504": 357},
        "target_latency_p50": "6500ms", "target_latency_p95": "6547ms", "target_latency_p99": "6600ms",
        "target_latency_basis": "percentiles of 357 logged requests (ata-apigee-access)",
    })
    assert "Target latency (percentiles of 357 logged requests (ata-apigee-access)):" in html
    assert "P95 6547ms" in html


def test_html_without_basis_keeps_plain_labels():
    html = _render({
        "proxy_name": "ata-inventory-v1", "response_codes": {"200": 5},
        "target_latency_p50": "N/A (not captured)", "target_latency_p95": "N/A (not captured)",
        "target_latency_p99": "N/A (not captured)",
    })
    assert "Target latency:" in html
    assert "P95 N/A (not captured)" in html


# --- GatewayTimeout faults record no target latency (final-qa2 S2, INC0010351) -----------
# S2 data shape: proxy/response_count 504 x394 (fault GatewayTimeout) + 200 x2; target/latencies
# one per-minute point, count 2, mean 16ms. The 16ms is the 2 answered requests only; it was
# rendered as "P95 16ms (GatewayTimeout observed)" while the backend measured ~6.5 s.

def test_gap_marks_percentiles_the_timeouts_hide():
    s = apply_gateway_timeout_gap(target_latency_summary([], [16.0]), 394, 2)
    reason = "not available: 394 of 396 requests were GatewayTimeout faults, which record no target latency"
    assert s["p50_ms"] == reason
    assert s["p95_ms"] == reason
    assert s["p99_ms"] == reason
    assert s["basis"].endswith("; excludes 394 GatewayTimeout faults, which record no target latency")


def test_gap_keeps_percentiles_the_timeouts_do_not_reach():
    # 1 timeout in 1000: 0.1% < 1%, so P50/P95/P99 of the answered requests stand.
    s = apply_gateway_timeout_gap(target_latency_summary([20.0] * 999, []), 1, 999)
    assert (s["p50_ms"], s["p95_ms"], s["p99_ms"]) == (20, 20, 20)
    assert "excludes 1 GatewayTimeout fault" in s["basis"]


def test_gap_boundary_is_strictly_above_one_minus_q():
    # 5 of 100 is exactly 5%: P95 still within the answered requests; 6 of 100 is not.
    at = apply_gateway_timeout_gap(target_latency_summary([20.0] * 95, []), 5, 95)
    assert at["p95_ms"] == 20 and isinstance(at["p99_ms"], str)
    over = apply_gateway_timeout_gap(target_latency_summary([20.0] * 94, []), 6, 94)
    assert isinstance(over["p95_ms"], str) and over["p50_ms"] == 20


def test_gap_without_timeouts_is_identity():
    s = target_latency_summary([], [16.0])
    assert apply_gateway_timeout_gap(s, 0, 2) is s


def _rc_ts(code: str, count: int, fault: str | None = None) -> MagicMock:
    ts = MagicMock()
    ts.metric.labels = {"response_code": code, **({"fault_code": fault, "fault_source": "target"} if fault else {})}
    ts.resource.labels = {}
    pt = MagicMock()
    pt.value.int64_value = count
    ts.points = [pt]
    return ts


@pytest.mark.asyncio
async def test_s2_shape_p95_is_not_available_not_16ms(test_config):
    alert = AlertEvent(
        incident_id="0.oecelps8a57g", policy_name="ata-inventory-latency", resource_name="ata-inventory-v1",
        state="open", started_at="2026-10-28T04:55:11Z",
    )
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         patch("google.cloud.logging.Client") as log_cls:
        def side_effect(request):
            if "latencies" in request.filter:
                return [_dist_ts([(2, 16.0)])]
            return [
                _rc_ts("504", 394, "messaging.adaptors.http.flow.GatewayTimeout"),
                _rc_ts("200", 2),
            ]
        mon_cls.return_value.list_time_series.side_effect = side_effect
        log_cls.return_value.list_entries.return_value = []
        dossier = await ApigeeSpecialist().investigate(alert, test_config)
    reason = "not available: 394 of 396 requests were GatewayTimeout faults, which record no target latency"
    assert dossier.metrics["target_latency_p95_ms"] == reason
    assert dossier.metrics["target_latency_p95"] == reason
    assert dossier.metrics["target_latency_p50"] == reason
    assert dossier.metrics["target_latency_basis"] == (
        "percentiles of 1 per-minute mean (apigee.googleapis.com/target/latencies); "
        "excludes 394 GatewayTimeout faults, which record no target latency"
    )
    row = SynthesizerAgent()._build_metrics_comparison([dossier], "ata-inventory-api")
    lat_row = next(r for r in row if r["layer"] == "Apigee" and "latency" in r["metric_name"])
    assert lat_row["during_value"] == reason
    assert "16ms" not in lat_row["during_value"]


@pytest.mark.asyncio
async def test_logged_timeouts_with_latency_are_in_the_population(test_config):
    # A GatewayTimeout entry that does carry target_latency_ms is measured, not excluded.
    e = _log_entry("5001")
    e.payload["fault_source"] = "GatewayTimeout"
    dossier = await _investigate(test_config, [], [e, _log_entry("20", status="200")])
    assert "excludes" not in dossier.metrics["target_latency_basis"]
    assert dossier.metrics["target_latency_p95_ms"] == 5001


@pytest.mark.asyncio
async def test_logged_timeouts_without_latency_are_excluded(test_config):
    blank = _log_entry("")
    blank.payload["fault_source"] = "GatewayTimeout"
    dossier = await _investigate(test_config, [], [blank, blank, _log_entry("20", status="200")])
    assert dossier.metrics["target_latency_p95_ms"] == (
        "not available: 2 of 3 requests were GatewayTimeout faults, which record no target latency"
    )
