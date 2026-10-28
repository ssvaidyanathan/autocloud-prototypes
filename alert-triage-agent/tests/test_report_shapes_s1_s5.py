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


def _sql(conns: int, cpu: float, anomalous: bool = False) -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="CloudSQLSpecialist", target_resource="ata-inventory-db", findings=[],
        metrics={"active_connections": conns, "cpu_utilization_pct": cpu, "max_connections": 25},
        evidence_items=[], queries_executed={}, anomaly_detected=anomalous,
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
            not_applicable_dossier("CloudRunSpecialist", "ata-inventory-db", "Cloud SQL"), _sql(24, 13.7, anomalous=True),
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
        def series(request):
            if "latencies" in request.filter:
                return [_lat_ts(s["lat"])] if s["lat"] else []
            if s.get("codes_error"):
                raise RuntimeError("response_count read failed")
            return [_code_ts(*c) for c in s["codes"]]

        mon_cls.return_value.list_time_series.side_effect = series
        log_cls.return_value.list_entries.return_value = list(s.get("log_entries", []))
        return await ApigeeSpecialist().investigate(alert, cfg)


def ticket_history_line(dossiers, origin: str, alert: AlertEvent):
    """The ticket's Apigee history line as the synthesizer builds it for this group."""
    return SynthesizerAgent._apigee_history_line(
        dossiers, origin=origin, components=SynthesizerAgent._history_scope(origin, alert, None),
    )


async def build_outputs(name: str, history_line=ticket_history_line, scenario: dict | None = None) -> dict:
    s = scenario or SCENARIOS[name]
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
    # The ticket problem statement as the synthesizer builds it (it names per-component 5xx state).
    problem = synth._synthesize_problem(alert, dossiers, s["origin"], blast)
    rca = synth._build_root_cause_analysis("rc", 0.7, dossiers, s["origin"], alert)
    # The observations the synthesizer writes (they reach the ticket body and the HTML).
    observations = synth._synthesize_observations(dossiers, alert)
    common = dict(
        group_id=f"grp-{name}", summary=problem, observations=observations, origin=s["origin"],
        blast_radius=blast, priority="P1", project_id="cloud-assist-fde-2", region="us-west1",
        change_induced="No (checked: ata-tools change timeline (Apigee deployment history))",
        apigee_history=line,
    )
    bridge = BridgeReport(
        **common, metrics_comparison=rows, apigee_analytics=analytics, topology_nodes=nodes,
        root_cause_analysis=rca,
        evidence_by_area={"change": [EvidenceItem(headline="Apigee Deployment History", content=full)]},
    )
    desc = bridge.build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="b")
    agent_desc = AgentReport(**common).build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="b")
    return {
        "problem": problem,
        "observations": observations,
        "alternatives": {a["alternative"]: a["reason"] for a in rca.get("alternatives_ruled_out", [])},
        "dossiers": dossiers,
        "blast": blast,
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
# Every proxy count names its source (fresh-rev-n6 O2).
IN_METRIC = " in the proxy/response_count metric"
TEMPLATE = ("Internal application 5xx exceptions", "Cascading 5xx failure propagation",
            "Cascading HTTP 504 gateway timeout", "SpikeArrest 429 throttling",
            "Connection pool saturation & transaction lock wait contention", "Upstream dependent")

IMPACT = {
    "S1": {"ata-inventory-api": "HTTP 5xx error rate 83.4%", "ata-inventory-v1": "HTTP 500: 629" + IN_METRIC,
           "ata-orders-api": "HTTP 5xx error rate 82.7%", "ata-orders-v1": NM},
    "S2": {"ata-inventory-api": "HTTP 5xx error rate 0.0%; slowest logged request latency 6518.03ms",
           "ata-inventory-v1": "HTTP 504: 394 (GatewayTimeout)" + IN_METRIC,
           "ata-orders-api": "HTTP 5xx error rate 0.0%; slowest logged request latency 6603.14ms",
           "ata-orders-v1": NM},
    "S3-inventory": {"ata-inventory-api": "HTTP 5xx error rate 82.7%", "ata-inventory-v1": "HTTP 500: 360" + IN_METRIC,
                     "ata-orders-api": NM, "ata-orders-v1": NM},
    "S3-orders-429": {"ata-orders-v1": "HTTP 429: 512 (SpikeArrestViolation)" + IN_METRIC},
    "S4": {"ata-inventory-db": "24 of 25 connections active", "ata-inventory-api": "HTTP 5xx error rate 0.0%",
           "ata-inventory-v1": "0 HTTP 4xx/5xx responses in the proxy/response_count metric (of 632 requests)", "ata-orders-api": "HTTP 5xx error rate 0.0%",
           "ata-orders-v1": NM},
    "S5": {"ata-inventory-api": "HTTP 5xx error rate 98.8%", "ata-inventory-v1": "HTTP 500: 553" + IN_METRIC,
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


def test_execute_investigation_puts_the_scoped_line_on_the_ticket(tmp_path):
    """The synthesizer's own call site scopes the history (S3 inventory), not only the helper."""
    import asyncio

    from agent.app.config import reset_agent_settings
    from agent.app.triage import write_preanalysis_lock

    cfg = AgentConfig(
        project_id="cloud-assist-fde-2", name_prefix="ata", region="us-west1", is_local=True,
        incident_store_path=str(tmp_path / "knowledge/incident-manager"), fallback_store_path=str(tmp_path / "fb"),
    )
    full, in_window = SCENARIOS["S3-inventory"]["history"]
    change = EvidenceDossier(
        specialist_name="ChangeSpecialist", target_resource="ata-inventory-api", findings=[full],
        metrics={"apigee_deployments_in_window": in_window, "apigee_deployment_scope": SCOPE,
                 "apigee_deployment_window_minutes": 120},
    )

    async def cloud_run(alert, config, target_service=None):
        return _cr(target_service or "ata-inventory-api", "82.7%")

    reset_agent_settings(cfg)
    try:
        alert = AlertEvent(incident_id="0.oecf31rr5hv4", policy_name="ata-inventory-5xx",
                           resource_name="ata-inventory-api", resource_type="cloud_run_revision",
                           state="open", started_at="2026-10-28T05:16:16Z")
        _, group_id, fname = write_preanalysis_lock(alert, cfg)
        synth = SynthesizerAgent()
        apigee = EvidenceDossier(specialist_name="ApigeeSpecialist", target_resource="ata-inventory-v1",
                                 metrics={"response_codes": {"500": 360}})
        with patch.object(synth.cloud_run_specialist, "investigate", side_effect=cloud_run), \
             patch.object(synth.apigee_specialist, "investigate", return_value=apigee), \
             patch.object(synth.cloud_sql_specialist, "investigate", return_value=_sql(3, 10.2)), \
             patch.object(synth.change_specialist, "investigate", return_value=change):
            report = asyncio.run(synth.execute_investigation(alert, group_id, fname, cfg))
    finally:
        reset_agent_settings(None)
    assert report.apigee_history == (
        "Apigee deployment history (org cloud-assist-fde-2, environment prod, proxies with prefix ata-; "
        "group components and proxies routing directly to ata-inventory-api: ata-inventory-api, ata-inventory-v1): "
        "no deployments of these proxies in the 2-hour window prior to alert onset "
        "(1 other proxy changed in window, see report)."
    )


# --- N4: the ticket and the HTML state the same measurement for every component ---------------
#
# S4 live (INC0010362): the ticket said "HTTP 5xx rate not measured on ata-inventory-v1" while the
# HTML Operational Impact row said "no HTTP 4xx/5xx responses recorded" for the same proxy, and
# the HTML 429 alternative said "Apigee 429 count ... not available". proxy/response_count was
# queried and returned only HTTP 200, so all three now state "0 ... of N requests".
# Output change per scenario (fresh-n4-report.md): S1, S2, S3-inventory, S5: HTML 429 alternative
# row only. S4: ticket problem statement, HTML impact row, HTML 429 alternative. S3-orders-429: none.

S4_PROBLEM = (
    "Database connection pool saturation (connections at 24/25) on ata-inventory-db since 2026-10-28T05:40:22Z "
    "(no HTTP 5xx errors measured on ata-inventory-api, ata-orders-api; "
    "0 HTTP 4xx/5xx responses in the proxy/response_count metric (of 632 requests) on ata-inventory-v1; "
    "HTTP 5xx rate not measured on ata-orders-v1)"
)


def test_s4_ticket_and_html_agree_on_the_proxy_measurement():
    out = _out("S4")
    assert out["problem"] == S4_PROBLEM
    assert S4_PROBLEM in out["desc"] and S4_PROBLEM in out["agent_desc"]
    assert out["impact"]["ata-inventory-v1"] == "0 HTTP 4xx/5xx responses in the proxy/response_count metric (of 632 requests)"
    assert "not measured on ata-inventory-v1" not in out["desc"]
    assert "no HTTP 4xx/5xx responses recorded" not in out["html"]
    assert out["alternatives"]["Upstream API Gateway Rate-Limiting"] == (
        "Apigee proxy/response_count queried on ata-inventory-v1 (429: 0 of 632 requests in the metric), "
        "ruling out SpikeArrest throttling."
    )
    # The proxy nobody queried stays "not measured" in both.
    assert out["impact"]["ata-orders-v1"] == NM


def _names(text: str) -> set[str]:
    return {n.strip() for n in text.split(",") if n.strip()}


def _problem_states(problem: str) -> tuple[set[str], set[str]]:
    """(components the problem statement says were not measured, components it states a value for)."""
    import re

    not_measured: set[str] = set()
    for m in re.finditer(r"not measured on ([^;)]+)", problem):
        not_measured |= _names(m.group(1))
    measured: set[str] = set()
    for m in re.finditer(r"no HTTP 5xx errors measured on ([^;)]+)", problem):
        measured |= _names(m.group(1))
    for m in re.finditer(r"(?:requests\)|metric|partial\)) on ([\w-]+)", problem):
        measured.add(m.group(1))
    for m in re.finditer(r"HTTP 5xx (?:rate on dependent services|responses on dependent proxies): ([^;]+)", problem):
        measured |= {part.split()[0] for part in m.group(1).split(", ")}
    m = re.search(r"\(([^()]*%|[^()]*requests\))\) on ([\w-]+)", problem)
    if m:
        measured.add(m.group(2))
    return not_measured, measured


def _variant(base: str, **changes) -> dict:
    return dict(SCENARIOS[base], **changes)


def _log_entry(status: str, ts: str) -> MagicMock:
    from datetime import datetime

    e = MagicMock()
    e.payload = {"status_code": status, "proxy": "ata-inventory-v1", "revision": "152",
                 "environment": "prod", "client_ip": "10.0.0.1", "target_latency_ms": "16"}
    e.timestamp = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return e


# Three backend 500s logged by the access log while proxy/response_count has no 5xx yet (the
# metric lags 1-3 min at alert onset): the access-log 5xx fallback fires (fresh-rev-n5 R1').
S1_LOG_500S = [_log_entry("500", "2026-10-28T04:30:00Z") for _ in range(3)]

# Shapes besides S1-S5 that each take a different branch of the measurement statement.
VARIANTS = {
    "S1-log-only-5xx": _variant("S1", codes=[], log_entries=S1_LOG_500S),
    "S1-log-only-5xx-metric-200": _variant("S1", codes=[("200", 10, None)], log_entries=S1_LOG_500S),
    "S1-log-only-5xx-metric-unreadable": _variant("S1", codes_error=True, log_entries=S1_LOG_500S),
    "S4-proxy-no-traffic": _variant("S4", codes=[], lat=[]),
    "S4-proxy-unavailable": _variant("S4", codes_error=True),
    "S4-proxy-4xx-only": _variant("S4", codes=[("200", 600, None), ("404", 7, None)]),
    "S4-proxy-5xx": _variant("S4", codes=[("200", 600, None), ("503", 9, None)]),
    "S4-cloud-run-0.1%": _variant("S4", others=lambda: [
        not_applicable_dossier("CloudRunSpecialist", "ata-inventory-db", "Cloud SQL"), _sql(24, 13.7, anomalous=True),
        _cr("ata-inventory-api", "0.1%"), _cr("ata-orders-api", "0.0%"),
    ]),
}


def _out_any(name: str) -> dict:
    import asyncio

    return asyncio.run(build_outputs(name, scenario=VARIANTS.get(name)))


@pytest.mark.parametrize("name", list(SCENARIOS) + list(VARIANTS))
def test_ticket_and_html_measurement_statements_agree_for_every_component(name):
    out = _out_any(name)
    not_measured, measured = _problem_states(out["problem"])
    assert not (not_measured & measured), out["problem"]
    for component in out["blast"]:
        impact = out["impact"].get(component)
        if impact is None:
            continue
        assert out["problem"] in out["desc"]  # the ticket carries this statement
        if component in not_measured:
            assert impact == NM, (component, impact, out["problem"])
        if component in measured:
            assert impact != NM, (component, impact, out["problem"])
        if impact == NM:
            assert component not in measured
    # The HTML 429 alternative reads the same proxy counts as the impact column.
    alt = out["alternatives"].get("Upstream API Gateway Rate-Limiting")
    proxy = next((d.target_resource for d in out["dossiers"] if d.specialist_name == "ApigeeSpecialist"), None)
    if alt and proxy:
        # No metric counts: "not measured", or only the access-log count with the metric unavailable (O1).
        impact = out["impact"].get(proxy, NM)
        no_metric = impact == NM or "proxy/response_count unavailable" in impact
        assert ("not available" in alt) == no_metric, (alt, impact)


def test_variant_statements():
    no_traffic = _out_any("S4-proxy-no-traffic")
    assert no_traffic["impact"]["ata-inventory-v1"] == "no responses in the proxy/response_count metric"
    assert "; no responses in the proxy/response_count metric on ata-inventory-v1;" in no_traffic["problem"]
    # A metric with no responses rules nothing out (fresh-rev-n5 R1', earlier O1).
    assert no_traffic["alternatives"]["Upstream API Gateway Rate-Limiting"] == (
        "Apigee 429 count on ata-inventory-v1: no responses in the proxy/response_count metric; "
        "SpikeArrest throttling not ruled out."
    )

    unavailable = _out_any("S4-proxy-unavailable")
    assert unavailable["impact"]["ata-inventory-v1"] == NM
    assert "HTTP 5xx rate not measured on ata-inventory-v1, ata-orders-v1" in unavailable["problem"]
    assert "not available" in unavailable["alternatives"]["Upstream API Gateway Rate-Limiting"]

    four = _out_any("S4-proxy-4xx-only")
    assert four["impact"]["ata-inventory-v1"] == "HTTP 404: 7" + IN_METRIC
    assert "0 HTTP 5xx responses in the proxy/response_count metric (of 607 requests) on ata-inventory-v1" in four["problem"]

    five = _out_any("S4-proxy-5xx")
    assert five["impact"]["ata-inventory-v1"] == "HTTP 503: 9" + IN_METRIC
    assert five["problem"].endswith("; HTTP 5xx responses on dependent proxies: ata-inventory-v1 9 HTTP 5xx responses "
                                  "in the proxy/response_count metric (of 609 requests)")

    tenth = _out_any("S4-cloud-run-0.1%")
    assert tenth["impact"]["ata-inventory-api"] == "HTTP 5xx error rate 0.1%"
    assert "not measured on ata-inventory-api" not in tenth["problem"]
    assert "HTTP 5xx rate on dependent services: ata-inventory-api 0.1%" in tenth["problem"]


# --- R1 (fresh-rev-n4): N in "of N requests" is the proxy/response_count total only -------------
#
# Live S4 (final-qa3): the access log and proxy/response_count each counted the same ~285 HTTP 200
# responses and both were added into one dict, so the ticket said "of 570 requests" for ~299 sent.
# The harness stubbed the access log empty, so this path was never exercised.

def test_s4_n_is_the_metric_total_when_the_access_log_logs_the_same_requests():
    import asyncio

    # One access-log entry per request the metric counted (632 HTTP 200), as live.
    entries = [_log_entry("200", "2026-10-28T05:35:00Z") for _ in range(632)]
    out = asyncio.run(build_outputs("S4", scenario=dict(SCENARIOS["S4"], log_entries=entries)))
    apigee = next(d for d in out["dossiers"] if d.specialist_name == "ApigeeSpecialist")
    # The log was read and its counts kept apart, labelled as such.
    assert apigee.metrics["access_log_status_codes"] == {"200": 632}
    assert apigee.metrics["response_codes"] == {"200": 632}
    assert out["problem"] == S4_PROBLEM
    assert out["impact"]["ata-inventory-v1"] == "0 HTTP 4xx/5xx responses in the proxy/response_count metric (of 632 requests)"
    assert out["alternatives"]["Upstream API Gateway Rate-Limiting"] == (
        "Apigee proxy/response_count queried on ata-inventory-v1 (429: 0 of 632 requests in the metric), "
        "ruling out SpikeArrest throttling."
    )
    assert "1264" not in out["desc"] and "1264" not in out["html"]
    assert "Observed response codes in window: HTTP 200: 632." in apigee.findings
    assert any(e.content == "Apigee proxy/response_count for `ata-inventory-v1`: HTTP 200: 632."
               for e in apigee.evidence_items)


def test_fault_counts_use_the_metric_only_when_the_access_log_has_status_codes():
    """5xx (S1 shape) and 429 (S3-orders shape) counts come from proxy/response_count only."""
    import asyncio

    s1_log = [_log_entry("500", "2026-10-28T04:30:00Z") for _ in range(40)] + \
             [_log_entry("200", "2026-10-28T04:30:00Z") for _ in range(116)]
    s1 = asyncio.run(build_outputs("S1", scenario=dict(SCENARIOS["S1"], log_entries=s1_log)))
    a1 = next(d for d in s1["dossiers"] if d.specialist_name == "ApigeeSpecialist")
    assert a1.metrics["response_codes"] == {"500": 629, "200": 116}
    assert a1.metrics["proxy_response_code_5xx"] == 629
    assert a1.metrics["proxy_response_codes_5xx"] == {"500": 629}

    s3_log = [_log_entry("429", "2026-10-28T05:17:00Z") for _ in range(30)]
    s3 = asyncio.run(build_outputs("S3-orders-429", scenario=dict(SCENARIOS["S3-orders-429"], log_entries=s3_log)))
    a3 = next(d for d in s3["dossiers"] if d.specialist_name == "ApigeeSpecialist")
    assert a3.metrics["response_codes"] == {"429": 512}
    assert a3.metrics["proxy_response_code_429"] == 512
    assert a3.metrics["access_log_status_codes"] == {"429": 30}


# --- R1' (fresh-rev-n5): the access-log 5xx fallback and the metric statement never contradict --
#
# When proxy/response_count has no 5xx but the access log does, the report states the log count
# ("access log shows 3 5xx responses (log-based, may be partial)"). Before, the same proxy's
# metric statement said "no traffic recorded" / "0 HTTP 4xx/5xx responses (of N requests)" with
# no source, and the 429 alternative said "ruling out" on a metric with no responses at all.

LOG_3 = "access log shows 3 5xx responses (log-based, may be partial)"
METRIC = "the proxy/response_count metric"
LOG_ONLY = {
    "S1-log-only-5xx": f"no responses in {METRIC}, while the {LOG_3}",
    "S1-log-only-5xx-metric-200": f"0 HTTP 4xx/5xx responses in {METRIC} (of 10 requests), while the {LOG_3}",
}


def _html_lines(html: str) -> list[str]:
    import html as html_lib
    import re

    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S)
    text = html_lib.unescape(re.sub(r"<[^>]+>", "\n", text))
    return [ln.strip() for ln in text.splitlines() if ln.strip()]


@pytest.mark.parametrize("name", list(LOG_ONLY))
def test_log_only_5xx_ticket_and_html_do_not_contradict_the_log(name):
    out = _out_any(name)
    apigee = next(d for d in out["dossiers"] if d.specialist_name == "ApigeeSpecialist")
    assert apigee.metrics["access_log_5xx"]["count"] == 3
    statement = LOG_ONLY[name]
    # One statement for the proxy, from measurements.ApigeeResponses, in the impact column.
    assert out["impact"]["ata-inventory-v1"] == statement
    assert f"Apigee proxy ata-inventory-v1: HTTP 5xx: not in proxy/response_count; {LOG_3}" in out["observations"]
    ticket = out["desc"].splitlines()
    html = _html_lines(out["html"])
    assert out["desc"] == out["agent_desc"]
    for where, lines in (("ticket", ticket), ("html", html)):
        text = "\n".join(lines)
        assert LOG_3 in text, where  # the log count reaches both
        assert "no traffic recorded" not in text, where
        assert "0 HTTP 4xx/5xx responses (of" not in text, where  # never without its source
        for ln in lines:
            if "0 HTTP 4xx/5xx" in ln or "no responses in" in ln:
                assert METRIC in ln and LOG_3 in ln, (where, ln)
    assert statement in "\n".join(html)
    alt = out["alternatives"]["Upstream API Gateway Rate-Limiting"]
    if name == "S1-log-only-5xx":
        # proxy/response_count total 0: nothing is ruled out on it.
        assert "ruling out" not in alt and "not ruled out" in alt, alt
        assert alt == f"Apigee 429 count on ata-inventory-v1: {statement}; SpikeArrest throttling not ruled out."
    else:
        assert alt == (
            "Apigee proxy/response_count queried on ata-inventory-v1 (429: 0 of 10 requests in the metric), "
            "ruling out SpikeArrest throttling."
        )
    assert alt in "\n".join(html)


def test_log_only_5xx_names_the_metric_in_the_problem_of_a_cloud_sql_origin():
    """The ticket problem line for a dependent proxy of a Cloud SQL origin (S4 shape)."""
    import asyncio

    s = _variant("S4", codes=[("200", 10, None)], policy="ata-cloudsql-connections-critical",
                 log_entries=[_log_entry("500", "2026-10-28T05:38:00Z") for _ in range(3)])
    out = asyncio.run(build_outputs("S4-log", scenario=s))
    # A non-5xx policy: the fallback does not fire, so the statement has no log clause.
    assert f"0 HTTP 4xx/5xx responses in {METRIC} (of 10 requests) on ata-inventory-v1" in out["problem"]
    assert "access log shows" not in out["problem"]


# --- fresh-rev-n6 O1 / N1 / case D -------------------------------------------------------------

LOG_3_UNAVAILABLE = f"HTTP 5xx: proxy/response_count unavailable; {LOG_3}"


def test_unreadable_metric_with_log_5xx_states_the_log_in_the_impact_column():
    """O1 (probe case C): S1 shape, proxy/response_count read fails, the access log shows 3 500s.
    The impact cell states the log count as the metrics row does, never "not measured"."""
    out = _out_any("S1-log-only-5xx-metric-unreadable")
    assert out["impact"]["ata-inventory-v1"] == LOG_3_UNAVAILABLE
    row = next(r for r in out["rows"] if r[0] == "Apigee" and r[2] == "ata-inventory-v1")
    assert row[3] == LOG_3_UNAVAILABLE
    assert out["desc"] == out["agent_desc"]
    html = "\n".join(_html_lines(out["html"]))
    assert LOG_3_UNAVAILABLE in html
    # The 429 alternative has no metric total to rule anything out with.
    alt = out["alternatives"]["Upstream API Gateway Rate-Limiting"]
    assert "ruling out" not in alt and "not ruled out" in alt, alt


def _sql_origin_5xx_policy(policy: str, codes, logs) -> dict:
    return _variant("S4", policy=policy, codes=codes, log_entries=logs)


@pytest.mark.parametrize(
    "policy, codes, logs, stated",
    [
        # Metric without 5xx: no_5xx_statement() with the log suffix (_synthesize_problem, proxies branch).
        ("ata-inventory-db-5xx", [("200", 10, None)], [_log_entry("500", "2026-10-28T05:38:00Z")] * 3,
         f"0 HTTP 4xx/5xx responses in {METRIC} (of 10 requests), while the {LOG_3} on ata-inventory-v1"),
        # 504 policy, metric 500s but no 504: five_xx_statement() with the log 504 suffix (erroring proxies).
        ("ata-inventory-db-504", [("500", 5, None), ("200", 10, None)], [_log_entry("504", "2026-10-28T05:38:00Z")] * 2,
         f"ata-inventory-v1 5 HTTP 5xx responses in {METRIC} (of 15 requests), "
         "while the access log shows 2 504 responses (log-based, may be partial)"),
    ],
)
def test_problem_line_states_the_log_fallback_end_to_end(policy, codes, logs, stated):
    """N1: the fallback fires in ApigeeSpecialist and its log suffix reaches the ticket problem line."""
    import asyncio

    out = asyncio.run(build_outputs("S4-log-5xx", scenario=_sql_origin_5xx_policy(policy, codes, logs)))
    apigee = next(d for d in out["dossiers"] if d.specialist_name == "ApigeeSpecialist")
    assert apigee.metrics["access_log_5xx"]["count"] == len(logs)  # the fallback fired
    assert stated in out["problem"], out["problem"]
    assert out["problem"] in out["desc"] and out["desc"] == out["agent_desc"]


def _proxy_origin(**changes) -> dict:
    """A 5xx alert on the proxy itself (origin = proxy), fresh-rev-n6 probe case D."""
    return _variant("S1", policy="ata-apigee-inventory-5xx", resource="ata-inventory-v1",
                    origin="ata-inventory-v1", **changes)


@pytest.mark.parametrize(
    "changes, rate",
    [
        (dict(codes=[("500", 5, None), ("200", 10, None)]), f"5 HTTP 5xx responses in {METRIC} (of 15 requests)"),
        (dict(codes=[("200", 10, None)], log_entries=S1_LOG_500S),
         f"0 HTTP 4xx/5xx responses in {METRIC} (of 10 requests), while the {LOG_3}"),
        (dict(codes_error=True, log_entries=S1_LOG_500S, others=lambda: [_sql(3, 10.2)]), LOG_3_UNAVAILABLE),
    ],
    ids=["metric-5xx", "log-only", "metric-unreadable-no-cloud-run"],
)
def test_proxy_origin_5xx_problem_is_not_a_spike_arrest_violation_without_429s(changes, rate):
    """Case D: with 0 429s observed the problem line states the proxy's 5xx count, not SpikeArrest."""
    import asyncio

    out = asyncio.run(build_outputs("D", scenario=_proxy_origin(**changes)))
    assert "SpikeArrest" not in out["problem"], out["problem"]
    assert out["problem"].startswith(f"Elevated HTTP 5xx error rate ({rate}) on ata-inventory-v1 "), out["problem"]
    assert out["desc"] == out["agent_desc"]


def test_proxy_origin_with_429s_is_still_a_spike_arrest_violation():
    """429s observed on a proxy origin under a non-429 policy: SpikeArrest stays the problem."""
    import asyncio

    out = asyncio.run(build_outputs("D429", scenario=_proxy_origin(
        codes=[("429", 40, "policies.ratelimit.SpikeArrestViolation"), ("200", 10, None)])))
    assert out["problem"].startswith("SpikeArrest rate-limit violation"), out["problem"]


# --- fresh-rev-n6 O2: every proxy count line names its source ------------------------------------

OBSERVED_COUNT = {
    "S1": "Apigee proxy ata-inventory-v1 recorded 629 HTTP 5xx responses in the proxy/response_count metric",
    "S3-inventory": "Apigee proxy ata-inventory-v1 recorded 360 HTTP 5xx responses in the proxy/response_count metric",
    "S3-orders-429": "Apigee proxy ata-orders-v1 recorded 512 HTTP 429 rate limit responses in the proxy/response_count metric",
    "S5": "Apigee proxy ata-inventory-v1 recorded 553 HTTP 5xx responses in the proxy/response_count metric",
}


@pytest.mark.parametrize("name", list(OBSERVED_COUNT))
def test_observed_proxy_count_names_the_metric(name):
    out = _out(name)
    assert OBSERVED_COUNT[name] in out["observations"]
    assert OBSERVED_COUNT[name] in out["desc"] and out["desc"] == out["agent_desc"]
    assert OBSERVED_COUNT[name] in "\n".join(_html_lines(out["html"]))


@pytest.mark.parametrize("name", list(SCENARIOS) + list(VARIANTS))
def test_every_proxy_count_line_in_the_ticket_and_html_names_its_source(name):
    """Observation counts, impact per-code facts and the fallback line each name proxy/response_count."""
    import re

    out = _out_any(name)
    for where, lines in (("ticket", out["desc"].splitlines()), ("html", _html_lines(out["html"]))):
        for ln in lines:
            for part in ln.split(" | "):
                if re.search(r"Apigee proxy \S+ recorded \d+ HTTP", part):
                    assert "in the proxy/response_count metric" in part, (where, part)
                if re.search(r"HTTP [0-9x]{3}: (\d+|not in|metric)", part):
                    assert "proxy/response_count" in part, (where, part)
                if "ruling out SpikeArrest" in part:  # only with a known proxy/response_count total
                    assert "requests in the metric)" in part, (where, part)
    for component, impact in out["impact"].items():
        if re.search(r"HTTP [0-9]{3}: \d+", impact):
            assert "in the proxy/response_count metric" in impact, (component, impact)
