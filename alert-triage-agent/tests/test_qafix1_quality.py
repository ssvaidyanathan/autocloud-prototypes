"""Regression tests for the quality bugs found by the live QA run (S1 INC0010321, S2 INC0010322).

1. Description 'Dependency Path' is the topology path from impacted entry points to the origin,
   not the reversed blast radius.
2. Work-note alert lines are distinguishable when one policy fires twice on one resource.
3. The RCA states the Apigee gateway timeout only from gathered data (never a guessed value).
4. Log excerpts label only genuine error lines as errors (severity- and status-aware).
"""

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
        "did not complete within the proxy's target timeout; `ata-inventory-api` P95 latency was 6546ms."
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


async def _s3_apigee_dossier(prev_faults: int = 0):
    from agent.app.config import AgentConfig
    from agent.app.specialists.apigee import ApigeeSpecialist

    alert = AlertEvent(
        incident_id="0.oeb5qvfcvh5r", policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z",
    )
    config = AgentConfig(project_id="cloud-assist-fde-2", region="us-west1", is_local=True)
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
        "count": 572, "count_source": "apigee.googleapis.com/proxy/response_count", "fault_code": S3_SA_FAULT,
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
    with _patch("tools.app.server.get_change_timeline", side_effect=ImportError("no tools in agent image")):
        dossier = asyncio.run(ChangeSpecialist().investigate(alert, config))
    joined = " ".join(dossier.findings)
    assert "No configuration or revision deployments detected" not in joined
    assert "Apigee proxy deployment history not queried" in joined
