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
