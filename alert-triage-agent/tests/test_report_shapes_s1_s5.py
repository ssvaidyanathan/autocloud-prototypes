"""Report rendering for the five demo scenario shapes (final-qa2 live retest, 2026-10-28).

Each scenario is rebuilt from the data the live run read (INC0010350..55): Apigee
proxy/response_count and target/latencies through ApigeeSpecialist, Cloud Run / Cloud SQL
dossier values, and the change timeline's Apigee history. The report pieces the N3 fixes touch
are then built by the synthesizer and rendered: the HTML metrics table and Apigee analytics line
(bridge renderer) and the ticket description (agent and bridge contracts, which must agree).

What N3 changes, per scenario (asserted below):
- S1: nothing (no GatewayTimeout, Cloud Run origin, no proxy deploy in the window).
- S2: HTML Apigee target latency P95 "16ms (GatewayTimeout observed)" -> "not available: ...".
- S3 inventory: HTML unchanged; ticket names no other group's proxy, counts it.
- S3 orders 429: HTML Cloud Run N/A rows -> one "not applicable (Apigee proxy)" row; ticket unchanged.
- S4: HTML Cloud Run N/A rows -> one "not applicable (Cloud SQL)" row; ticket counts orders-v1 r187.
- S5: ticket counts orders-v1 r187 (restored after S3) instead of naming it.
- All: the HTML "Operational Impact" column (N1) shows what was measured on each node, or
  "not measured in this investigation"; before, it was fixed template text per node type.
"""

from unittest.mock import MagicMock, patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.report import InvestigationReport as AgentReport
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import not_applicable_dossier
from agent.app.specialists.synthesizer import SynthesizerAgent
from bridge.app.contracts.report import EvidenceItem, InvestigationReport as BridgeReport
from bridge.app.templates.renderer import render_standalone_html
from topology.topology_graph import get_blast_radius

GT = "messaging.adaptors.http.flow.GatewayTimeout"
SCOPE = "org cloud-assist-fde-2, environment prod, proxies ata-*"
HIST = "Apigee deployment history (" + SCOPE + "): "
R186 = {"proxy": "ata-orders-v1", "revision": "186", "environment": "prod", "timestamp": "2026-10-28T05:09:31Z"}
R187 = {"proxy": "ata-orders-v1", "revision": "187", "environment": "prod", "timestamp": "2026-10-28T05:27:07Z"}
NO_DEPLOYS = (
    HIST + "no proxy deployments in the 2-hour window prior to alert onset; deployed: ata-inventory-v1 "
    "revision 152 since 2026-10-27T23:01:59Z, ata-orders-v1 revision 185 since 2026-10-28T01:57:49Z."
)


def _deployed(d: dict, mins: int) -> tuple[str, list[dict]]:
    line = HIST + f"proxy {d['proxy']} revision {d['revision']} deployed {d['timestamp']} ({mins}m before alert onset)."
    return line, [dict(d, deployed_minutes_ago=mins)]


def _cr(service: str, err: str, slowest_ms: float | None = None) -> EvidenceDossier:
    lat = {"latency_p95_ms": slowest_ms, "latency_statistic": "slowest logged request latency"} if slowest_ms else {}
    return EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource=service, findings=[],
        metrics={"error_rate_5xx": err, "container_restarts": "N/A (not captured)", **lat},
        evidence_items=[], queries_executed={}, anomaly_detected=True,
    )


def _sql(conns: int, cpu: float) -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="CloudSQLSpecialist", target_resource="ata-inventory-db", findings=[],
        metrics={"active_connections": conns, "cpu_utilization_pct": cpu, "max_connections": 25},
        evidence_items=[], queries_executed={}, anomaly_detected=False,
    )


# name: alert (policy, resource, started_at), origin, Apigee codes and latency points,
# other dossiers, change history (full line, in-window deployments).
SCENARIOS = {
    "S1": dict(
        policy="ata-inventory-5xx", resource="ata-inventory-api", started="2026-10-28T04:32:27Z",
        origin="ata-inventory-api", codes=[("500", 629, None), ("200", 116, None)],
        lat=[(10, 10.0)] * 5 + [(10, 11.0)] * 5 + [(10, 21.0)],
        others=lambda: [_cr("ata-inventory-api", "83.4%"), _sql(3, 10.2), _cr("ata-orders-api", "82.7%")],
        history=(NO_DEPLOYS, []),
    ),
    "S2": dict(
        policy="ata-inventory-latency", resource="ata-inventory-api", started="2026-10-28T04:55:11Z",
        origin="ata-inventory-api", codes=[("504", 394, GT), ("200", 2, None)], lat=[(2, 16.0)],
        others=lambda: [
            _cr("ata-inventory-api", "0.0%", 6518.03), _sql(4, 10.5), _cr("ata-orders-api", "0.0%", 6603.14),
        ],
        history=(NO_DEPLOYS, []),
    ),
    "S3-inventory": dict(
        policy="ata-inventory-5xx", resource="ata-inventory-api", started="2026-10-28T05:16:16Z",
        origin="ata-inventory-api", codes=[("500", 360, None)], lat=[],
        others=lambda: [_cr("ata-inventory-api", "82.7%"), _sql(3, 10.2)],
        history=_deployed(R186, 6),
    ),
    "S3-orders-429": dict(
        policy="ata-apigee-orders-429", resource="ata-orders-v1", started="2026-10-28T05:18:50Z",
        origin="ata-orders-v1", codes=[("429", 512, "policies.ratelimit.SpikeArrestViolation")], lat=[],
        others=lambda: [not_applicable_dossier("CloudRunSpecialist", "ata-orders-v1", "Apigee proxy"), _sql(3, 10.2)],
        history=_deployed(R186, 9),
    ),
    "S4": dict(
        policy="ata-cloudsql-connections-critical", resource="ata-inventory-db", started="2026-10-28T05:40:22Z",
        origin="ata-inventory-db", codes=[("200", 632, None)],
        lat=[(60, 16.0)] * 5 + [(60, 17.0)] * 4 + [(60, 19.0)],
        others=lambda: [
            not_applicable_dossier("CloudRunSpecialist", "ata-inventory-db", "Cloud SQL"), _sql(24, 13.7),
            _cr("ata-inventory-api", "0.0%"), _cr("ata-orders-api", "0.0%"),
        ],
        history=_deployed(R187, 13),
    ),
    "S5": dict(
        policy="ata-inventory-5xx", resource="ata-inventory-api", started="2026-10-28T06:09:19Z",
        origin="ata-inventory-api", codes=[("500", 553, None), ("200", 2, None)],
        lat=[(50, 70.0)] * 5 + [(50, 74.0)] * 4 + [(50, 79.0)],
        others=lambda: [_cr("ata-inventory-api", "98.8%"), _sql(2, 18.9), _cr("ata-orders-api", "99.0%")],
        history=_deployed(R187, 42),
    ),
}


def _code_ts(code: str, count: int, fault) -> MagicMock:
    ts = MagicMock()
    ts.metric.labels = {"response_code": code, **({"fault_code": fault, "fault_source": "target"} if fault else {})}
    ts.resource.labels = {}
    pt = MagicMock()
    pt.value.int64_value = count
    ts.points = [pt]
    return ts


def _lat_ts(points) -> MagicMock:
    ts = MagicMock()
    pts = []
    for count, mean in points:
        pt = MagicMock()
        pt.value.distribution_value.count = count
        pt.value.distribution_value.mean = mean
        pts.append(pt)
    ts.points = pts
    return ts


async def _apigee_dossier(s: dict, alert: AlertEvent) -> EvidenceDossier:
    cfg = AgentConfig(project_id="cloud-assist-fde-2", name_prefix="ata", region="us-west1", is_local=True)
    with patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         patch("google.cloud.logging.Client") as log_cls:
        mon_cls.return_value.list_time_series.side_effect = lambda request: (
            ([_lat_ts(s["lat"])] if s["lat"] else []) if "latencies" in request.filter
            else [_code_ts(*c) for c in s["codes"]]
        )
        log_cls.return_value.list_entries.return_value = []
        return await ApigeeSpecialist().investigate(alert, cfg)


def ticket_history_line(dossiers, origin: str, alert: AlertEvent):
    """The ticket's Apigee history line as the synthesizer builds it for this group."""
    return SynthesizerAgent._apigee_history_line(
        dossiers, origin=origin, components=SynthesizerAgent._history_scope(origin, alert, None),
    )


async def build_outputs(name: str, history_line=ticket_history_line) -> dict:
    s = SCENARIOS[name]
    alert = AlertEvent(
        incident_id=f"0.{name}", policy_name=s["policy"], resource_name=s["resource"],
        state="open", started_at=s["started"],
    )
    full, in_window = s["history"]
    change = EvidenceDossier(
        specialist_name="ChangeSpecialist", target_resource=s["origin"], findings=[full],
        metrics={"apigee_deployments_in_window": in_window, "apigee_deployment_scope": SCOPE,
                 "apigee_deployment_window_minutes": 120},
    )
    apigee = await _apigee_dossier(s, alert)
    dossiers = s["others"]() + [apigee, change]
    synth = SynthesizerAgent()
    rows = synth._build_metrics_comparison(dossiers, s["origin"])
    blast = [s["origin"]] + [b for b in get_blast_radius(s["origin"]) if b != s["origin"]]
    nodes = synth._build_topology_nodes(s["origin"], blast, dossiers)
    analytics = synth._build_apigee_analytics(dossiers, [s["origin"]])
    line = history_line(dossiers, s["origin"], alert)
    common = dict(
        group_id=f"grp-{name}", summary=f"{name} summary", observations="obs", origin=s["origin"],
        blast_radius=blast, priority="P1", project_id="cloud-assist-fde-2", region="us-west1",
        change_induced="No (checked: ata-tools change timeline (Apigee deployment history))",
        apigee_history=line,
    )
    bridge = BridgeReport(
        **common, metrics_comparison=rows, apigee_analytics=analytics, topology_nodes=nodes,
        evidence_by_area={"change": [EvidenceItem(headline="Apigee Deployment History", content=full)]},
    )
    desc = bridge.build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="b")
    agent_desc = AgentReport(**common).build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="b")
    return {
        "impact": {n["resource"]: n["impact"] for n in nodes},
        "rows": [(r["layer"], r["metric_name"], r["resource"], r["during_value"]) for r in rows],
        "analytics": {k: analytics.get(k) for k in ("target_latency_p50", "target_latency_p95", "target_latency_p99",
                                                    "target_latency_basis")},
        "ticket_history": next((ln for ln in desc.splitlines() if ln.startswith("Apigee deployment history")), None),
        "agent_desc": agent_desc,
        "desc": desc,
        "html": render_standalone_html(bridge),
    }


def _out(name: str) -> dict:
    import asyncio

    return asyncio.run(build_outputs(name))


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_ticket_description_contract_parity(name):
    out = _out(name)
    assert out["desc"] == out["agent_desc"]


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_html_keeps_the_full_org_wide_history(name):
    full = SCENARIOS[name]["history"][0]
    assert full in _out(name)["html"]


# --- S1: unchanged -------------------------------------------------------------------------

def test_s1_outputs():
    out = _out("S1")
    assert out["analytics"]["target_latency_p95"] == "21ms"
    assert out["analytics"]["target_latency_basis"] == (
        "percentiles of 11 per-minute means (apigee.googleapis.com/target/latencies)"
    )
    assert ("Cloud Run", "run.googleapis.com/request_count (5xx)", "ata-inventory-api", "83.4%") in out["rows"]
    assert ("Apigee", "apigee.googleapis.com/proxy/response_count (5xx)", "ata-inventory-v1", "629 req") in out["rows"]
    assert out["ticket_history"] == NO_DEPLOYS.replace("proxies ata-*", "proxies with prefix ata-")


# --- S2: P95 not available ------------------------------------------------------------------

S2_NA = "not available: 394 of 396 requests were GatewayTimeout faults, which record no target latency"


def test_s2_outputs():
    out = _out("S2")
    lat = next(r for r in out["rows"] if r[0] == "Apigee")
    assert lat[1] == (
        "Apigee target latency, P95 (percentiles of 1 per-minute mean (apigee.googleapis.com/target/latencies); "
        "excludes 394 GatewayTimeout faults, which record no target latency)"
    )
    assert lat[3] == S2_NA
    assert out["analytics"]["target_latency_p95"] == S2_NA
    assert "16ms" not in out["html"]
    assert f"P95 {S2_NA}" in out["html"]
    assert out["ticket_history"] == NO_DEPLOYS.replace("proxies ata-*", "proxies with prefix ata-")


# --- S3 -------------------------------------------------------------------------------------

def test_s3_inventory_outputs():
    out = _out("S3-inventory")
    assert out["ticket_history"] == (
        "Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies with prefix ata-; "
        "group components and proxies routing directly to ata-inventory-api: ata-inventory-api, ata-inventory-v1): "
        "no deployments of these proxies in the 2-hour window prior to alert onset "
        "(1 other proxy changed in window, see report)."
    )
    assert ("Cloud Run", "run.googleapis.com/request_count (5xx)", "ata-inventory-api", "82.7%") in out["rows"]
    assert ("Apigee", "apigee.googleapis.com/proxy/response_count (5xx)", "ata-inventory-v1", "360 req") in out["rows"]


def test_s3_orders_429_outputs():
    out = _out("S3-orders-429")
    cloud_run = [r for r in out["rows"] if r[0] == "Cloud Run"]
    assert cloud_run == [("Cloud Run", "Cloud Run service metrics", "ata-orders-v1", "not applicable (Apigee proxy)")]
    assert "N/A (no telemetry captured)" not in out["html"]
    assert ("Apigee", "apigee.googleapis.com/proxy/response_count (429)", "ata-orders-v1", "512 req") in out["rows"]
    # Its own proxy change stays named.
    assert out["ticket_history"] == SCENARIOS["S3-orders-429"]["history"][0].replace(
        "proxies ata-*", "proxies with prefix ata-")


# --- S4 / S5 --------------------------------------------------------------------------------

def test_s4_outputs():
    out = _out("S4")
    cloud_run = [r for r in out["rows"] if r[0] == "Cloud Run"]
    assert cloud_run[0] == ("Cloud Run", "Cloud Run service metrics", "ata-inventory-db", "not applicable (Cloud SQL)")
    assert [r[2] for r in cloud_run[1:]] == ["ata-inventory-api", "ata-inventory-api", "ata-orders-api", "ata-orders-api"]
    assert out["analytics"]["target_latency_p95"] == "19ms"
    assert out["ticket_history"] == (
        "Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies with prefix ata-; "
        "group components and proxies routing directly to ata-inventory-db: ata-inventory-db): "
        "no deployments of these proxies in the 2-hour window prior to alert onset "
        "(1 other proxy changed in window, see report)."
    )


def test_s5_outputs():
    out = _out("S5")
    assert out["analytics"]["target_latency_p95"] == "79ms"
    assert ("Cloud Run", "run.googleapis.com/request_count (5xx)", "ata-inventory-api", "98.8%") in out["rows"]
    assert out["ticket_history"].endswith(
        "ata-inventory-api, ata-inventory-v1): no deployments of these proxies in the 2-hour window prior "
        "to alert onset (1 other proxy changed in window, see report)."
    )
    assert "revision 187" not in out["ticket_history"]


# --- N1: Operational Impact is measured values, never template text ---------------------------

NM = "not measured in this investigation"
TEMPLATE = ("Internal application 5xx exceptions", "Cascading 5xx failure propagation",
            "Cascading HTTP 504 gateway timeout", "SpikeArrest 429 throttling",
            "Connection pool saturation & transaction lock wait contention", "Upstream dependent")

IMPACT = {
    "S1": {"ata-inventory-api": "HTTP 5xx error rate 83.4%", "ata-inventory-v1": "HTTP 500: 629",
           "ata-orders-api": "HTTP 5xx error rate 82.7%", "ata-orders-v1": NM},
    "S2": {"ata-inventory-api": "HTTP 5xx error rate 0.0%; slowest logged request latency 6518.03ms",
           "ata-inventory-v1": "HTTP 504: 394 (GatewayTimeout)",
           "ata-orders-api": "HTTP 5xx error rate 0.0%; slowest logged request latency 6603.14ms",
           "ata-orders-v1": NM},
    "S3-inventory": {"ata-inventory-api": "HTTP 5xx error rate 82.7%", "ata-inventory-v1": "HTTP 500: 360",
                     "ata-orders-api": NM, "ata-orders-v1": NM},
    "S3-orders-429": {"ata-orders-v1": "HTTP 429: 512 (SpikeArrestViolation)"},
    "S4": {"ata-inventory-db": "24 of 25 connections active", "ata-inventory-api": "HTTP 5xx error rate 0.0%",
           "ata-inventory-v1": "no HTTP 4xx/5xx responses recorded", "ata-orders-api": "HTTP 5xx error rate 0.0%",
           "ata-orders-v1": NM},
    "S5": {"ata-inventory-api": "HTTP 5xx error rate 98.8%", "ata-inventory-v1": "HTTP 500: 553",
           "ata-orders-api": "HTTP 5xx error rate 99.0%", "ata-orders-v1": NM},
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_operational_impact_is_measured_or_not_measured(name):
    out = _out(name)
    assert out["impact"] == IMPACT[name]
    for phrase in TEMPLATE:
        assert phrase not in out["html"]
    for value in IMPACT[name].values():
        assert value in out["html"]


def test_degraded_report_claims_no_impact(tmp_path):
    from agent.app.config import reset_agent_settings
    from agent.app.triage import generate_investigation_report, write_preanalysis_lock

    cfg = AgentConfig(
        project_id="cloud-assist-fde-2", name_prefix="ata", region="us-west1", is_local=True,
        incident_store_path=str(tmp_path / "knowledge/incident-manager"), fallback_store_path=str(tmp_path / "fb"),
    )
    reset_agent_settings(cfg)
    try:
        alert = AlertEvent(incident_id="0.deg", policy_name="ata-inventory-5xx", resource_name="ata-inventory-api",
                           resource_type="cloud_run_revision", state="open", started_at="2026-10-28T04:32:27Z")
        _, group_id, fname = write_preanalysis_lock(alert, cfg)
        report = generate_investigation_report(alert, group_id, fname, cfg, degraded_reason="test")
    finally:
        reset_agent_settings(None)
    assert report.topology_nodes and {n["impact"] for n in report.topology_nodes} == {NM}


def test_bridge_fallback_rows_claim_no_impact():
    report = BridgeReport(group_id="g", summary="s", observations="o", origin="ata-inventory-api",
                          blast_radius=["ata-inventory-api", "ata-inventory-v1"], priority="P1",
                          project_id="cloud-assist-fde-2", region="us-west1")
    html = render_standalone_html(report)
    assert html.count(NM) == 2
    assert "Root defect" not in html and "Cascading error propagation" not in html
    with_node = render_standalone_html(BridgeReport(
        group_id="g", summary="s", observations="o", origin="ata-inventory-api", blast_radius=["ata-inventory-api"],
        priority="P1", project_id="cloud-assist-fde-2", region="us-west1",
        topology_nodes=[{"resource": "ata-inventory-api", "type": "Cloud Run Service", "role": "Origin"}],
    ))
    assert NM in with_node and "Impacted downstream consumer" not in with_node
