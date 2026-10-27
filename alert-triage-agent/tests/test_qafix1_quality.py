"""Regression tests for the quality bugs found by the live QA run (S1 INC0010321, S2 INC0010322).

1. Description 'Dependency Path' is the topology path from impacted entry points to the origin,
   not the reversed blast radius.
2. Work-note alert lines are distinguishable when one policy fires twice on one resource.
3. The RCA states the Apigee gateway timeout only from gathered data (never a guessed value).
4. Log excerpts label only genuine error lines as errors (severity- and status-aware).
"""

from agent.app.tools.tools_client import ToolsNotConfigured
from agent.app.contracts.report import InvestigationReport, ProbableRootCause
from agent.app.specialists.synthesizer import SynthesizerAgent, dependency_path_for
from topology.topology_graph import get_loader

# --- S1/S2 shape: origin ata-inventory-api, blast radius as the agent reported it ---------
S1_ORIGIN = "ata-inventory-api"
S1_BLAST_RADIUS = ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]
S1_EXPECTED_PATH = (
    "ata-inventory-v1 -> ata-inventory-api; "
    "ata-orders-v1 -> ata-orders-api -> ata-inventory-api"
)


def _report(**kwargs) -> InvestigationReport:
    base = dict(
        group_id="ata-inventory-5xx-ata-inventory-api-20261027t071737z",
        summary="Service degradation on ata-inventory-api",
        observations="Cloud Monitoring policy 'ata-inventory-5xx' triggered on 'ata-inventory-api'",
        probable_root_cause=ProbableRootCause(text="x", confidence=0.81),
        origin=S1_ORIGIN,
        blast_radius=S1_BLAST_RADIUS,
        priority="P1",
    )
    base.update(kwargs)
    return InvestigationReport(**base)


# ---------------------------------------------------------------------------------------
# Bug 1: Dependency Path
# ---------------------------------------------------------------------------------------


def test_dependency_paths_follow_topology_edges_to_origin():
    paths = get_loader().dependency_paths(S1_ORIGIN)
    assert paths == [
        ["ata-inventory-v1", "ata-inventory-api"],
        ["ata-orders-v1", "ata-orders-api", "ata-inventory-api"],
    ]
    # Every hop is a real TOPOLOGY.md edge (caller -> callee).
    edges = {(e.from_node, e.to_node) for e in get_loader().edges}
    for p in paths:
        for a, b in zip(p, p[1:]):
            assert (a, b) in edges


def test_dependency_paths_for_db_origin_and_leaf_and_unknown():
    db_paths = get_loader().dependency_paths("ata-inventory-db")
    assert ["ata-orders-v1", "ata-orders-api", "ata-inventory-api", "ata-inventory-db"] in db_paths
    assert ["ata-inventory-v1", "ata-inventory-api", "ata-inventory-db"] in db_paths
    # Nothing depends on an Apigee proxy: the path is the proxy itself.
    assert get_loader().dependency_paths("ata-orders-v1") == [["ata-orders-v1"]]
    # Aliases resolve to the canonical component.
    assert get_loader().dependency_paths("inventory-api") == get_loader().dependency_paths(S1_ORIGIN)
    assert get_loader().dependency_paths("not-a-component") == []
    assert get_loader().format_dependency_paths("not-a-component") is None


def test_synthesizer_dependency_path_is_not_reversed_blast_radius():
    dep = SynthesizerAgent()._determine_dependency_path(S1_ORIGIN, S1_BLAST_RADIUS)
    assert dep == S1_EXPECTED_PATH
    # The live S1 ticket showed this false chain (orders-api does not call inventory-v1).
    assert dep != " -> ".join(reversed(S1_BLAST_RADIUS))
    assert "ata-orders-api -> ata-inventory-v1" not in dep


def test_dependency_path_for_unknown_origin_is_none_and_logged(caplog):
    with caplog.at_level("WARNING"):
        assert dependency_path_for("not-a-component") is None
    assert "No topology dependency path for not-a-component" in caplog.text


def test_description_renders_topology_path():
    desc = _report(dependency_path=S1_EXPECTED_PATH).build_structured_description()
    assert f"  - Dependency Path: {S1_EXPECTED_PATH}\n" in desc
    assert "ata-orders-v1 -> ata-orders-api -> ata-inventory-v1" not in desc


def test_description_without_path_omits_line_instead_of_guessing():
    desc = _report().build_structured_description()
    assert "Dependency Path" not in desc
    assert "Client -> Ingress" not in desc
    assert "ata-orders-v1 -> ata-orders-api -> ata-inventory-v1 -> ata-inventory-api" not in desc


# ---------------------------------------------------------------------------------------
# Bug 2: two alerts of one policy on one resource (S2: one per response_code series)
# ---------------------------------------------------------------------------------------

from agent.app.contracts.alert import AlertEvent  # noqa: E402
from agent.app.specialists.synthesizer import apply_group_alerts, format_alert_trigger_line  # noqa: E402


def _s2_alert(inc_id: str, policy: str, resource: str, started_at: str) -> dict:
    return {
        "incident_id": inc_id,
        "policy": policy,
        "policy_name": policy,
        "resource": resource,
        "resource_name": resource,
        "component": resource,
        "signal": "latency",
        "state": "open",
        "started_at": started_at,
    }


S2_ALERTS = [
    _s2_alert("0.oeb4hxzrsiqu", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:40:49Z"),
    _s2_alert("0.oeb4hxzrsiqp", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:40:49Z"),
    _s2_alert("0.oeb4j8ur7t63", "ata-orders-latency", "ata-orders-api", "2026-10-27T07:42:24Z"),
]


def _s2_report() -> InvestigationReport:
    primary = S2_ALERTS[0]
    trigger = format_alert_trigger_line(
        primary["policy"], primary["resource"], primary["started_at"], "open", primary["incident_id"]
    )
    return _report(
        group_id="ata-inventory-latency-ata-inventory-api-20261027t074049z",
        observations=f"{trigger} | HTTP 5xx error rate reached 0.0% (33 rpm) on ata-inventory-api",
        priority="P2",
        alerts=[dict(primary)],
    )


def test_trigger_observation_carries_incident_id():
    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-27T07:40:49Z",
        state="open",
    )
    obs = SynthesizerAgent()._synthesize_observations([], alert)
    assert obs.split(" | ")[0] == (
        "Cloud Monitoring policy 'ata-inventory-latency' triggered on 'ata-inventory-api' "
        "at 2026-10-27T07:40:49Z (state: open, incident 0.oeb4hxzrsiqu)"
    )


def test_same_policy_same_resource_alert_lines_are_distinguishable():
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS])
    items = report.observations.split(" | ")
    alert_lines = [i for i in items if "Cloud Monitoring policy" in i]
    assert len(alert_lines) == 3
    assert len(set(alert_lines)) == 3
    # Stripping the correlated prefix must not make two lines identical (the S2 work note did).
    assert len({ln.replace("Correlated alert: ", "") for ln in alert_lines}) == 3
    for a in S2_ALERTS:
        assert sum(f"incident {a['incident_id']})" in ln for ln in alert_lines) == 1
    # The title count still equals the alerts in the group.
    assert report.alert_count() == 3
    assert report.build_short_description().endswith("(3 alerts)")
    html = report.build_executive_summary_html()
    for a in S2_ALERTS:
        assert a["incident_id"] in html


def test_third_alert_of_same_policy_is_not_dropped():
    extra = _s2_alert("0.third", "ata-inventory-latency", "ata-inventory-api", "2026-10-27T07:41:00Z")
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS] + [extra])
    assert "incident 0.third)" in report.observations
    assert report.alert_count() == 4


def test_apply_group_alerts_stays_idempotent_with_incident_ids():
    report = apply_group_alerts(_s2_report(), [dict(a) for a in S2_ALERTS])
    once = report.observations
    report = apply_group_alerts(report, [dict(a) for a in S2_ALERTS])
    assert report.observations == once
    assert report.alert_count() == 3


# ---------------------------------------------------------------------------------------
# Bug 4: uvicorn INFO access lines on stderr are stored by Cloud Run as severity ERROR
# ---------------------------------------------------------------------------------------

from datetime import datetime, timezone  # noqa: E402

from agent.app.specialists.logs import (  # noqa: E402
    format_single_line_log_excerpt,
    parse_access_log_line,
    parse_embedded_level,
)

STDERR_LOG = "projects/cloud-assist-fde-2/logs/run.googleapis.com%2Fstderr"
REQUESTS_LOG = "projects/cloud-assist-fde-2/logs/run.googleapis.com%2Frequests"


class _Entry:
    def __init__(self, payload, severity="ERROR", log_name=STDERR_LOG, http_request=None):
        self.log_name = log_name
        self.severity = severity
        self.timestamp = datetime(2026, 10, 27, 7, 42, 5, tzinfo=timezone.utc)
        self.http_request = http_request
        self.payload = payload


def test_s2_200_ok_access_line_is_not_an_error_excerpt():
    entry = _Entry('INFO:     10.1.2.3:37442 - "GET /items HTTP/1.1" 200 OK')
    assert format_single_line_log_excerpt(entry) is None


def test_s1_500_access_line_is_labelled_error_without_info_prefix():
    entry = _Entry('INFO:     10.1.2.3:50318 - "POST /items/reserve HTTP/1.1" 500 Internal Server Error')
    line = format_single_line_log_excerpt(entry)
    assert line is not None
    assert line.startswith("[2026-10-27T07:42:05Z] ERROR HTTP 500 /items/reserve - ")
    assert "INFO:" not in line
    assert "[REDACTED_IP]:50318" in line
    assert "10.1.2.3" not in line


def test_stderr_info_message_without_status_is_not_an_error():
    assert format_single_line_log_excerpt(_Entry("INFO:     Shutting down")) is None
    assert format_single_line_log_excerpt(_Entry("WARNING: slow query 1200ms")) is None


def test_genuine_error_lines_are_kept():
    # Traceback text on stderr with no level of its own keeps the entry severity.
    tb = format_single_line_log_excerpt(_Entry("Traceback (most recent call last): ValueError: boom"))
    assert tb is not None and tb.split(" ")[1] == "ERROR"
    # A line declaring ERROR itself.
    err = format_single_line_log_excerpt(_Entry("ERROR:    Exception in ASGI application", severity="INFO"))
    assert err is not None and " ERROR - Exception in ASGI application" in err
    # Request log with a 5xx status.
    req = format_single_line_log_excerpt(
        _Entry(None, severity="ERROR", log_name=REQUESTS_LOG,
               http_request={"requestUrl": "https://x.run.app/items", "status": 503})
    )
    assert req is not None and "ERROR HTTP 503 /items" in req


def test_request_log_4xx_or_2xx_with_error_severity_is_not_an_error_excerpt():
    for status in (200, 404, 429):
        entry = _Entry(None, severity="ERROR", log_name=REQUESTS_LOG,
                       http_request={"requestUrl": "https://x.run.app/items", "status": status})
        assert format_single_line_log_excerpt(entry) is None


def test_parsers():
    assert parse_embedded_level("INFO:     x") == "INFO"
    assert parse_embedded_level("warn: x") == "WARNING"
    assert parse_embedded_level("no level here") is None
    assert parse_access_log_line('1.2.3.4:1 - "GET /items?x=1 HTTP/1.1" 200 OK') == ("GET", "/items", 200)
    assert parse_access_log_line("no request") is None


def test_description_keeps_redaction_token_and_timestamp_in_log_excerpt():
    excerpt = (
        "Log excerpt (ata-inventory-api): [2026-10-27T07:12:01Z] ERROR HTTP 500 /items/reserve - "
        '[REDACTED_IP]:50318 - "POST /items/reserve HTTP/1.1" 500 Internal Server Error'
    )
    desc = _report(observations=f"first | {excerpt}").build_structured_description()
    assert "[2026-10-27T07:12:01Z]" in desc
    assert "[REDACTED_IP]:50318" in desc
    assert " :50318" not in desc


async def _cloud_run_dossier_with_stderr(payloads: list[str], policy: str):
    from unittest.mock import MagicMock, patch

    from agent.app.config import AgentConfig
    from agent.app.specialists.cloud_run import CloudRunSpecialist

    entries = []
    for p in payloads:
        e = MagicMock()
        e.log_name = STDERR_LOG
        e.payload = p
        e.text_payload = None
        e.http_request = None
        e.severity = "ERROR"
        e.timestamp = datetime(2026, 10, 27, 7, 42, 5, tzinfo=timezone.utc)
        entries.append(e)
    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu",
        policy_name=policy,
        resource_name="ata-inventory-api",
        started_at="2026-10-27T07:40:49Z",
    )
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as metric_cls:
        log_cls.return_value.list_entries.return_value = entries
        metric_cls.return_value.list_time_series.return_value = []
        return await CloudRunSpecialist().investigate(alert, config)


import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_s2_cloud_run_sample_logs_exclude_200_access_lines():
    dossier = await _cloud_run_dossier_with_stderr(
        ['INFO:     10.1.2.3:37442 - "GET /items HTTP/1.1" 200 OK', "INFO:     Shutting down"],
        "ata-inventory-latency",
    )
    content = "\n".join(e.content for e in dossier.evidence_items)
    assert "200 OK" not in content
    assert "Sample Logs (excluding /admin/*):\n- N/A (not captured)" in content
    # The synthesizer then shows no log excerpt rather than a mislabelled one.
    obs = SynthesizerAgent()._synthesize_observations([dossier], AlertEvent(
        incident_id="0.oeb4hxzrsiqu", policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api", started_at="2026-10-27T07:40:49Z",
    ))
    assert "Log excerpt" not in obs


@pytest.mark.asyncio
async def test_s1_cloud_run_sample_logs_keep_500_and_drop_admin_lines():
    dossier = await _cloud_run_dossier_with_stderr(
        [
            'INFO:     10.1.2.3:50318 - "POST /items/reserve HTTP/1.1" 500 Internal Server Error',
            'INFO:     10.1.2.3:50319 - "POST /admin/fault HTTP/1.1" 500 Internal Server Error',
        ],
        "ata-inventory-5xx",
    )
    content = "\n".join(e.content for e in dossier.evidence_items)
    assert "ERROR HTTP 500 /items/reserve" in content
    assert "/admin/fault" not in content


# ---------------------------------------------------------------------------------------
# Bug 3: Apigee GatewayTimeout context in the S2 RCA (facts only, no timeout value)
# ---------------------------------------------------------------------------------------

from unittest.mock import MagicMock as _MagicMock, patch as _patch  # noqa: E402

from agent.app.specialists.base import EvidenceDossier  # noqa: E402

S2_GT_FAULT = "messaging.adaptors.http.flow.GatewayTimeout"


def _ts(labels: dict, values: list[int]):
    ts = _MagicMock()
    ts.metric.labels = labels
    pts = []
    for v in values:
        pt = _MagicMock()
        pt.value.int64_value = v
        pts.append(pt)
    ts.points = pts
    return ts


async def _s2_apigee_dossier(series, log_payloads):
    from agent.app.config import AgentConfig
    from agent.app.specialists.apigee import ApigeeSpecialist

    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        started_at="2026-10-27T07:40:49Z",
    )
    entries = []
    for p in log_payloads:
        e = _MagicMock()
        e.payload = p
        entries.append(e)
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with _patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         _patch("google.cloud.logging.Client") as log_cls:
        mon_cls.return_value.list_time_series.side_effect = (
            lambda request: series if "response_count" in request.filter else []
        )
        log_cls.return_value.list_entries.return_value = entries
        return await ApigeeSpecialist().investigate(alert, config)


S2_SERIES = [
    _ts({"response_code": "504", "method": "GET", "fault_code": S2_GT_FAULT,
         "fault_source": "target", "apigee_fault": "false"}, [60, 70, 70]),
    _ts({"response_code": "200", "method": "GET", "apigee_fault": "false"}, [5]),
]
S2_LOGS = [
    {"proxy": "ata-inventory-v1", "revision": "132", "environment": "prod", "status_code": "",
     "target_latency_ms": "", "fault_source": "GatewayTimeout", "client_ip": "34.61.176.32"}
    for _ in range(3)
]


@pytest.mark.asyncio
async def test_apigee_specialist_records_gateway_timeout_evidence():
    dossier = await _s2_apigee_dossier(S2_SERIES, S2_LOGS)
    assert dossier.target_resource == "ata-inventory-v1"
    gt = dossier.metrics["gateway_timeout"]
    assert gt == {
        "proxy": "ata-inventory-v1",
        "count": 200,
        "count_source": "apigee.googleapis.com/proxy/response_count",
        "response_code": "504",
        "fault_code": S2_GT_FAULT,
        "fault_source": "target",
        "count_is_lower_bound": False,
        "revision": "132",
    }


@pytest.mark.asyncio
async def test_apigee_specialist_without_timeouts_records_nothing():
    dossier = await _s2_apigee_dossier([S2_SERIES[1]], [])
    assert "gateway_timeout" not in dossier.metrics


def _s2_dossiers(gt: dict | None, p95=6546.0):
    cr = EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource="ata-inventory-api",
        findings=[], metrics={"latency_p95_ms": p95, "error_rate_5xx": "0.0%"},
        evidence_items=[], queries_executed={}, anomaly_detected=True, confidence=0.9,
    )
    ap = EvidenceDossier(
        specialist_name="ApigeeSpecialist", target_resource="ata-inventory-v1",
        findings=[], metrics={"gateway_timeout": gt} if gt else {},
        evidence_items=[], queries_executed={}, anomaly_detected=False, confidence=0.9,
    )
    return [cr, ap]


S2_GT = {
    "proxy": "ata-inventory-v1", "count": 200,
    "count_source": "apigee.googleapis.com/proxy/response_count",
    "response_code": "504", "fault_code": S2_GT_FAULT, "fault_source": "target", "revision": "132",
}


def test_s2_root_cause_states_gateway_timeout_fact_without_timeout_value():
    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu", policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api", started_at="2026-10-27T07:40:49Z",
    )
    text, _ = SynthesizerAgent()._determine_root_cause(
        _s2_dossiers(S2_GT), "ata-inventory-api", alert, blast_radius=S1_BLAST_RADIUS
    )
    assert (
        "Apigee proxy `ata-inventory-v1` (revision 132) returned 200 HTTP 504 responses with fault "
        f"`{S2_GT_FAULT}` (fault source: target). Requests on ata-inventory-v1 -> ata-inventory-api "
        "did not complete within the proxy's target timeout; `ata-inventory-api` peak observed latency was 6546ms."
    ) in text
    # The timeout value is not in the gathered data, so none is stated.
    assert "5000" not in text and "5 s" not in text and "5s" not in text


def test_no_gateway_timeout_fact_without_evidence():
    alert = AlertEvent(
        incident_id="0.oeb4hxzrsiqu", policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api", started_at="2026-10-27T07:40:49Z",
    )
    text, _ = SynthesizerAgent()._determine_root_cause(
        _s2_dossiers(None), "ata-inventory-api", alert, blast_radius=S1_BLAST_RADIUS
    )
    assert "GatewayTimeout" not in text and "Apigee" not in text


def test_gateway_timeout_fact_requires_topology_path_and_omits_missing_fields():
    synth = SynthesizerAgent()
    # A proxy that is not on a path to the origin: no claim.
    other = dict(S2_GT, proxy="ata-orders-v1")
    assert synth._gateway_timeout_fact(_s2_dossiers(other), "ata-inventory-v1") is None
    # Log-only evidence (no metric labels, no revision, no latency): only what was observed.
    log_only = {"proxy": "ata-inventory-v1", "count": 3, "count_source": "ata-apigee-access log entries",
                "response_code": None, "fault_code": "GatewayTimeout", "fault_source": None, "revision": None}
    fact = synth._gateway_timeout_fact(_s2_dossiers(log_only, p95="N/A (not captured)"), "ata-inventory-api")
    assert fact == (
        "Apigee proxy `ata-inventory-v1` returned 3 responses with fault `GatewayTimeout`. Requests on "
        "ata-inventory-v1 -> ata-inventory-api did not complete within the proxy's target timeout."
    )


# ---------------------------------------------------------------------------------------
# Bugs 6/7 (S3, INC0010324): SpikeArrest 429s after a new ata-orders-v1 proxy revision
# ---------------------------------------------------------------------------------------

from datetime import timedelta  # noqa: E402

S3_SA_FAULT = "policies.ratelimit.SpikeArrestViolation"
S3_DEPLOY = datetime(2026, 10, 27, 8, 27, 26, tzinfo=timezone.utc)


def _s3_log_entries(prev_faults: int = 0):
    entries = []
    # Revision 161: 48 requests over 51 s before the switch (live S3: 47x 404, 1x 200, no SpikeArrest).
    for i in range(48):
        e = _MagicMock()
        e.timestamp = S3_DEPLOY - timedelta(seconds=51) + timedelta(seconds=i)
        e.payload = {"proxy": "ata-orders-v1", "revision": "161", "environment": "prod",
                     "status_code": "404", "fault_source": "SpikeArrestViolation" if i < prev_faults else "ErrorResponseCode",
                     "client_ip": "34.61.176.32"}
        entries.append(e)
    # Revision 162: 586 requests over 6 min 24 s, 572 SpikeArrest faults.
    for i in range(586):
        e = _MagicMock()
        e.timestamp = S3_DEPLOY + timedelta(seconds=i * 384 / 585)
        e.payload = {"proxy": "ata-orders-v1", "revision": "162", "environment": "prod",
                     "status_code": "", "fault_source": "SpikeArrestViolation" if i < 572 else "ErrorResponseCode",
                     "client_ip": "34.61.176.32"}
        entries.append(e)
    return entries


def _s3_series():
    sa = _ts({"response_code": "429", "method": "GET", "fault_code": S3_SA_FAULT,
              "fault_source": "policy", "apigee_fault": "false"}, [300, 272])
    sa.resource.labels = {"org": "cloud-assist-fde-2", "env": "prod", "proxy_name": "ata-orders-v1"}
    return [sa]


async def _s3_apigee_dossier(prev_faults: int = 0, **config_kwargs):
    from agent.app.config import AgentConfig
    from agent.app.specialists.apigee import ApigeeSpecialist

    alert = AlertEvent(
        incident_id="0.oeb5qvfcvh5r", policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z",
    )
    config = AgentConfig(project_id="cloud-assist-fde-2", region="us-west1", is_local=True, **config_kwargs)
    with _patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         _patch("google.cloud.logging.Client") as log_cls:
        series = _s3_series()
        mon_cls.return_value.list_time_series.side_effect = (
            lambda request: series if "response_count" in request.filter else []
        )
        log_cls.return_value.list_entries.return_value = _s3_log_entries(prev_faults)
        return await ApigeeSpecialist().investigate(alert, config), alert


@pytest.mark.asyncio
async def test_s3_apigee_specialist_records_revision_change_and_spike_arrest():
    dossier, _ = await _s3_apigee_dossier()
    assert dossier.metrics["apigee_org"] == "cloud-assist-fde-2"
    assert dossier.metrics["apigee_environment"] == "prod"
    assert dossier.metrics["spike_arrest"] == {
        "count": 572, "count_source": "apigee.googleapis.com/proxy/response_count",
        "count_is_lower_bound": False, "response_code": "429", "fault_code": S3_SA_FAULT,
    }
    change = dossier.metrics["proxy_revision_change"]
    assert change["previous_revision"] == "161" and change["new_revision"] == "162"
    assert change["new_first_served_at"] == "2026-10-27T08:27:26Z"
    assert change["previous_requests"] == 48 and change["previous_spike_arrest_faults"] == 0
    assert change["new_requests"] == 586 and change["new_spike_arrest_faults"] == 572
    # Revision 161 served for under a minute: no rate is claimed for it.
    assert change["previous_requests_per_minute"] is None
    assert change["new_requests_per_minute"] is not None


@pytest.mark.asyncio
async def test_s3_root_cause_is_proxy_config_change_not_traffic_volume():
    dossier, alert = await _s3_apigee_dossier()
    text, _ = SynthesizerAgent()._determine_root_cause([dossier], "ata-orders-v1", alert)
    assert text.startswith(
        "Configuration change on Apigee proxy `ata-orders-v1`: revision 162 "
        "(first served 2026-10-27T08:27:26Z, replacing revision 161)"
    )
    assert f"572 HTTP 429 responses with fault `{S3_SA_FAULT}`" in text
    assert "revision 161 served 48 requests with none" in text
    assert "request rate before the switch was not measured" in text
    for claim in ("traffic burst", "surge", "abusive"):
        assert claim not in text.lower()


@pytest.mark.asyncio
async def test_s3_actions_roll_back_to_explicit_previous_revision_without_cloud_armor():
    dossier, alert = await _s3_apigee_dossier()
    actions = SynthesizerAgent()._synthesize_recommendations(
        [dossier], "ata-orders-v1", alert, region="us-west1", project_id="cloud-assist-fde-2"
    )
    first = actions[0]
    assert first.command == (
        "gcloud apigee apis deploy 161 --api=ata-orders-v1 --environment=prod "
        "--organization=cloud-assist-fde-2 --override"
    )
    assert "revision 161" in first.title and "revision 162" in first.title
    assert first.rollback.endswith(
        "gcloud apigee apis deploy 162 --api=ata-orders-v1 --environment=prod "
        "--organization=cloud-assist-fde-2 --override"
    )
    blob = " ".join(f"{a.title} {a.command} {a.verification} {a.rollback}" for a in actions)
    for bad in ("security-policies", "ata-edge-policy", "34.61.176.32", "CLIENT_IP", "apiproxies", "surge"):
        assert bad not in blob


@pytest.mark.asyncio
async def test_s3_without_org_env_gives_rollback_without_guessed_command(caplog):
    dossier, alert = await _s3_apigee_dossier()
    dossier.metrics.pop("apigee_org")
    with caplog.at_level("WARNING"):
        actions = SynthesizerAgent()._synthesize_recommendations(
            [dossier], "ata-orders-v1", alert, region="us-west1", project_id="cloud-assist-fde-2"
        )
    assert actions[0].command is None
    assert "revision 161" in actions[0].title
    assert "Apigee org/environment not captured" in caplog.text


@pytest.mark.asyncio
async def test_spike_arrest_on_both_revisions_does_not_blame_the_new_revision():
    dossier, alert = await _s3_apigee_dossier(prev_faults=10)
    text, _ = SynthesizerAgent()._determine_root_cause([dossier], "ata-orders-v1", alert)
    assert "Configuration change" not in text
    assert text.startswith("SpikeArrest rate-limit violations at Apigee ingress proxy `ata-orders-v1` (572 HTTP 429")
    actions = SynthesizerAgent()._synthesize_recommendations(
        [dossier], "ata-orders-v1", alert, region="us-west1", project_id="cloud-assist-fde-2"
    )
    assert not any("apis deploy" in (a.command or "") for a in actions)


def test_change_specialist_no_deploy_finding_names_what_was_queried():
    import asyncio

    from agent.app.config import AgentConfig
    from agent.app.specialists.change import ChangeSpecialist

    alert = AlertEvent(
        incident_id="0.oeb5qvfcvh5r", policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z",
    )
    config = AgentConfig(project_id="test-proj", region="test-region", is_local=True)
    with _patch("agent.app.specialists.change.fetch_change_timeline", side_effect=ToolsNotConfigured("TOOLS_URL is not set")):
        dossier = asyncio.run(ChangeSpecialist().investigate(alert, config))
    joined = " ".join(dossier.findings)
    assert "No configuration or revision deployments detected" not in joined
    assert "Change history not queried" in joined
    assert "Apigee deployment history was not read" in joined


# ---------------------------------------------------------------------------------------
# Bug 5: latency policies reduce across series; latency/5xx close promptly on missing data
# ---------------------------------------------------------------------------------------

import re as _re  # noqa: E402
from pathlib import Path as _Path  # noqa: E402

_OBS = _Path(__file__).resolve().parents[1] / "infra/terraform/observability"


def _policy_block(main_tf: str, name: str) -> str:
    start = main_tf.index(f'resource "google_monitoring_alert_policy" "{name}"')
    nxt = main_tf.find('resource "google_monitoring_alert_policy"', start + 1)
    return main_tf[start: nxt if nxt != -1 else len(main_tf)]


def test_latency_policies_reduce_series_to_one_per_service():
    main_tf = (_OBS / "main.tf").read_text()
    for name in ("orders_latency", "inventory_latency"):
        block = _policy_block(main_tf, name)
        assert "per_series_aligner   = var.latency_per_series_aligner" in block
        assert "cross_series_reducer = var.latency_cross_series_reducer" in block
        assert "group_by_fields      = var.latency_group_by" in block
        assert "ALIGN_PERCENTILE_99" not in block
    variables = (_OBS / "variables.tf").read_text()
    assert _re.search(r'variable "latency_per_series_aligner"[^}]*default\s*=\s*"ALIGN_DELTA"', variables)
    assert _re.search(r'variable "latency_cross_series_reducer"[^}]*default\s*=\s*"REDUCE_PERCENTILE_99"', variables)
    assert _re.search(
        r'variable "latency_group_by"[^}]*default\s*=\s*\["resource\.label\.service_name"\]', variables
    )


def test_latency_and_5xx_policies_set_missing_data_behaviour_from_variable():
    main_tf = (_OBS / "main.tf").read_text()
    for name in ("orders_latency", "inventory_latency", "orders_5xx", "inventory_5xx", "apigee_orders_5xx"):
        block = _policy_block(main_tf, name)
        assert "evaluation_missing_data = var.alert_evaluation_missing_data" in block, name
        # The API requires duration >= 60s for evaluation_missing_data.
        # Latency policies take it from var.latency_duration_secs (validated >= 60, see
        # tests/test_stray_alerts.py); the others keep a literal.
        m = _re.search(r'duration\s*=\s*"(\d+)s"', block)
        if name.endswith("_latency"):
            assert 'duration                = "${var.latency_duration_secs}s"' in block, name
        else:
            assert m and int(m.group(1)) >= 60, name
    assert len(_re.findall(r"^\s*evaluation_missing_data\s*=", main_tf, _re.M)) == 5
    variables = (_OBS / "variables.tf").read_text()
    assert _re.search(
        r'variable "alert_evaluation_missing_data"[^}]*default\s*=\s*"EVALUATION_MISSING_DATA_INACTIVE"', variables
    )
    assert "validation {" in variables.split('variable "alert_evaluation_missing_data"')[1]


# ---------------------------------------------------------------------------------------
# S4 (INC0010325): Cloud SQL connection saturation without any observed 5xx
# ---------------------------------------------------------------------------------------


def _s4_dossiers(caller_5xx: str = "0.0%"):
    sql = EvidenceDossier(
        specialist_name="CloudSQLSpecialist", target_resource="ata-inventory-db", findings=[],
        metrics={"active_connections": 24, "max_connections": 25}, evidence_items=[],
        queries_executed={}, anomaly_detected=True, confidence=0.9,
    )
    # CloudRunSpecialist run against the DB alert: nothing measurable (live S4 showed N/A).
    cr_db = EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource="ata-inventory-db", findings=[],
        metrics={"error_rate_5xx": "N/A (not captured)", "request_count_rpm": "N/A (not captured)",
                 "container_restarts": "N/A (not captured)"},
        evidence_items=[], queries_executed={}, anomaly_detected=False, confidence=0.9,
    )
    cr_api = EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource="ata-inventory-api", findings=[],
        metrics={"error_rate_5xx": caller_5xx, "request_count_rpm": 87,
                 "container_restarts": "N/A (not captured)"},
        evidence_items=[], queries_executed={}, anomaly_detected=False, confidence=0.9,
    )
    return [sql, cr_db, cr_api]


S4_ALERT = AlertEvent(
    incident_id="0.oeb69qnqwd4o", policy_name="ata-cloudsql-connections-critical",
    resource_name="ata-inventory-db", started_at="2026-10-27T08:58:25Z",
)
S4_BLAST = ["ata-inventory-db", "ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]


def test_s4_problem_does_not_claim_unobserved_5xx_cascade():
    text = SynthesizerAgent()._synthesize_problem(S4_ALERT, _s4_dossiers(), "ata-inventory-db", S4_BLAST)
    assert "5xx failures" not in text and "cascading" not in text
    assert text.startswith("Database connection pool saturation (connections at 24/25) on ata-inventory-db")
    assert "no HTTP 5xx errors measured on ata-inventory-api" in text
    assert "HTTP 5xx rate not measured on ata-inventory-v1, ata-orders-api, ata-orders-v1" in text


def test_s4_problem_states_cascade_only_with_measured_caller_5xx():
    text = SynthesizerAgent()._synthesize_problem(S4_ALERT, _s4_dossiers("42.0%"), "ata-inventory-db", S4_BLAST)
    assert text.endswith("; HTTP 5xx rate on dependent services: ata-inventory-api 42.0%")


def test_s4_problem_states_low_measured_5xx_and_never_claims_none():
    # Review finding: a 3% rate must not become "no HTTP 5xx errors".
    text = SynthesizerAgent()._synthesize_problem(S4_ALERT, _s4_dossiers("3.0%"), "ata-inventory-db", S4_BLAST)
    assert "ata-inventory-api 3.0%" in text
    assert "no HTTP 5xx" not in text
    # Nothing measured on any dependent: say so, claim nothing.
    text = SynthesizerAgent()._synthesize_problem(
        S4_ALERT, _s4_dossiers("N/A (not captured)"), "ata-inventory-db", S4_BLAST
    )
    assert "no HTTP 5xx" not in text
    assert "HTTP 5xx rate not measured on ata-inventory-api" in text


def test_s4_problem_ignores_5xx_on_services_that_are_not_dependents():
    dossiers = _s4_dossiers()
    dossiers.append(EvidenceDossier(
        specialist_name="CloudRunSpecialist", target_resource="unrelated-svc", findings=[],
        metrics={"error_rate_5xx": "50.0%"}, evidence_items=[], queries_executed={},
        anomaly_detected=True, confidence=0.9,
    ))
    text = SynthesizerAgent()._synthesize_problem(S4_ALERT, dossiers, "ata-inventory-db", S4_BLAST)
    assert "unrelated-svc" not in text


def test_s4_observations_skip_unmeasured_values_and_never_fill_in_numbers():
    obs = SynthesizerAgent()._synthesize_observations(_s4_dossiers(), S4_ALERT)
    assert "N/A (not captured) rpm" not in obs
    assert "on ata-inventory-db; container restarts" not in obs
    assert "HTTP 5xx error rate N/A" not in obs
    assert "HTTP 5xx error rate 0.0% (87 rpm) on ata-inventory-api" in obs
    assert "320 rpm" not in obs and "container restarts: 0" not in obs


def test_s4_actions_never_patch_database_flags():
    actions = SynthesizerAgent()._synthesize_recommendations(
        _s4_dossiers(), "ata-inventory-db", S4_ALERT, region="us-west1", project_id="cloud-assist-fde-2"
    )
    commands = [a.command or "" for a in actions]
    assert not any("instances patch" in c or "--database-flags" in c for c in commands)
    assert 'gcloud sql instances describe ata-inventory-db --format="yaml(settings.tier,settings.databaseFlags)"' in commands
    leak = next(a for a in actions if a.title.startswith("Inspect connection pool release logic"))
    assert "in ata-inventory-api, which calls ata-inventory-db" in leak.title
    assert 'resource.labels.service_name="ata-inventory-api"' in leak.command
    assert "--project=cloud-assist-fde-2" in leak.command



# ---------------------------------------------------------------------------------------
# Review findings on the Apigee evidence
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_org_and_env_read_from_real_monitoring_time_series_labels():
    from google.cloud import monitoring_v3

    from agent.app.config import AgentConfig
    from agent.app.specialists.apigee import ApigeeSpecialist

    ts = monitoring_v3.TimeSeries()
    ts.metric.labels["response_code"] = "429"
    ts.metric.labels["fault_code"] = S3_SA_FAULT
    ts.resource.labels["org"] = "cloud-assist-fde-2"
    ts.resource.labels["env"] = "prod"
    pt = monitoring_v3.Point()
    pt.value.int64_value = 5
    ts.points.append(pt)
    alert = AlertEvent(incident_id="0.x", policy_name="ata-apigee-orders-429",
                       resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z")
    config = AgentConfig(project_id="cloud-assist-fde-2", region="us-west1", is_local=True)
    with _patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         _patch("google.cloud.logging.Client") as log_cls:
        mon_cls.return_value.list_time_series.side_effect = (
            lambda request: [ts] if "response_count" in request.filter else []
        )
        log_cls.return_value.list_entries.return_value = []
        dossier = await ApigeeSpecialist().investigate(alert, config)
    assert dossier.metrics["apigee_org"] == "cloud-assist-fde-2"
    assert dossier.metrics["apigee_environment"] == "prod"
    assert dossier.metrics["spike_arrest"]["count"] == 5


@pytest.mark.asyncio
async def test_truncated_access_log_reports_no_revision_switch(caplog):
    with caplog.at_level("WARNING"):
        dossier, alert = await _s3_apigee_dossier(apigee_access_log_max_entries=100)
    assert "proxy_revision_change" not in dossier.metrics
    assert "hit the 100-entry cap" in caplog.text
    text, _ = SynthesizerAgent()._determine_root_cause([dossier], "ata-orders-v1", alert)
    assert "Configuration change" not in text


def test_summarize_revision_change_rejects_switch_back():
    from agent.app.specialists.apigee import summarize_revision_change

    t0 = S3_DEPLOY
    stats = {
        "161": {"first": t0, "last": t0 + timedelta(minutes=9), "requests": 50, "spike_arrest_faults": 0},
        "162": {"first": t0 + timedelta(minutes=2), "last": t0 + timedelta(minutes=5), "requests": 40, "spike_arrest_faults": 30},
    }
    # A -> B -> A: the newest entry is back on 161.
    assert summarize_revision_change(stats, latest_revision="161") is None
    assert summarize_revision_change(stats, latest_revision="162")["new_revision"] == "162"


@pytest.mark.asyncio
async def test_two_environments_record_no_environment_or_switch():
    from agent.app.config import AgentConfig
    from agent.app.specialists.apigee import ApigeeSpecialist

    entries = _s3_log_entries()
    for e in entries[:10]:
        e.payload = dict(e.payload, environment="test")
    alert = AlertEvent(incident_id="0.x", policy_name="ata-apigee-orders-429",
                       resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z")
    config = AgentConfig(project_id="cloud-assist-fde-2", region="us-west1", is_local=True)
    with _patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         _patch("google.cloud.logging.Client") as log_cls:
        mon_cls.return_value.list_time_series.return_value = []
        log_cls.return_value.list_entries.return_value = entries
        dossier = await ApigeeSpecialist().investigate(alert, config)
    assert "apigee_environment" not in dossier.metrics
    assert "proxy_revision_change" not in dossier.metrics


def test_log_only_gateway_timeout_count_is_stated_as_lower_bound():
    gt = dict(S2_GT, count_source="ata-apigee-access log entries", count_is_lower_bound=True, revision=None)
    fact = SynthesizerAgent()._gateway_timeout_fact(_s2_dossiers(gt), "ata-inventory-api")
    assert "returned at least 200 HTTP 504 responses" in fact


def test_topology_failure_in_new_helpers_is_logged_not_raised(caplog):
    synth = SynthesizerAgent()
    with _patch("agent.app.specialists.synthesizer.get_loader", side_effect=RuntimeError("boom")), \
         caplog.at_level("ERROR"):
        assert synth._db_caller_actions("ata-inventory-db", "p") == []
        assert synth._gateway_timeout_fact(_s2_dossiers(S2_GT), "ata-inventory-api") is None
    assert "Topology caller lookup failed" in caplog.text
    assert "Topology dependency path lookup failed" in caplog.text
