"""Regression tests for QA batch 2 (S5 INC0010326 rollback target and follow-ups).

Bug 1: the S5 rollback named ata-inventory-api-00140-p9m, the previously *created* revision
(retired, 0% traffic), and claimed it "served before". Traffic was pinned 100% to
ata-inventory-api-00073-9vb. The rollback target must come from traffic history.
"""

from datetime import datetime, timezone
import logging
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.change import ChangeSpecialist, previous_serving_revision
from agent.app.specialists.synthesizer import SynthesizerAgent

from tests.traffic_fakes import FakeRequestCountClient

# --- S5 shape (live 2026-10-27, cloud-assist-fde-2) -------------------------------------
PROJECT = "cloud-assist-fde-2"
REGION = "us-west1"
SERVICE = "ata-inventory-api"
S5_REV = "ata-inventory-api-s5-1793093161"
S5_CREATED = "2026-10-27T09:26:02.841876Z"
PINNED_REV = "ata-inventory-api-00073-9vb"  # served 100% before the S5 deploy
RETIRED_NEWER_REV = "ata-inventory-api-00140-p9m"  # created 05:08Z, never served
RETIRED_OLDER_REV = "ata-inventory-api-00126-745"  # latestReady, 0% traffic
ALERT_STARTED = "2026-10-27T09:31:17Z"
# Live request_count per minute on 00073-9vb, points ending 08:57..09:26 (225 requests).
PINNED_PER_MINUTE = [97, 40, 0, 8, 0, 8, 8, 8, 0, 0, 8, 8, 0, 8, 0, 8, 0, 0, 0, 8, 1, 0, 0, 8, 0, 0, 0, 0, 3, 4]
SVC_PATH = f"projects/{PROJECT}/locations/{REGION}/services/{SERVICE}"


def _revision(name: str, created: str, db_user: str) -> dict:
    return {
        "name": f"{SVC_PATH}/revisions/{name}",
        "createTime": created,
        "containers": [{"image": "us-west1-docker.pkg.dev/x/ata/inventory@sha256:abc",
                        "env": [{"name": "DB_USER", "value": db_user}]}],
    }


S5_SERVICE = {
    "name": SVC_PATH,
    "traffic": [{"revision": S5_REV, "percent": 100}],
    "latestReadyRevision": f"{SVC_PATH}/revisions/{RETIRED_OLDER_REV}",
    "updateTime": "2026-10-27T09:26:06Z",
}
S5_REVISIONS = [
    _revision(S5_REV, S5_CREATED, "invalid-iam-user"),
    _revision(RETIRED_NEWER_REV, "2026-10-27T05:08:00Z", "ata-inventory-sa"),
    _revision(RETIRED_OLDER_REV, "2026-10-26T18:00:00Z", "ata-inventory-sa"),
    _revision(PINNED_REV, "2026-10-23T10:00:00Z", "ata-inventory-sa"),
]


def _alert() -> AlertEvent:
    return AlertEvent(
        incident_id="0.oeb71clke0b0",
        policy_name="ata-inventory-5xx",
        resource_name=SERVICE,
        started_at=ALERT_STARTED,
    )


def _cfg(**kw) -> AgentConfig:
    return AgentConfig(project_id=PROJECT, region=REGION, is_local=False, **kw)


class _Resp:
    def __init__(self, payload: dict):
        self.status_code = 200
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _admin_api_get(url, headers=None, timeout=None):
    if url.endswith("/revisions"):
        return _Resp({"revisions": S5_REVISIONS})
    return _Resp({"services": [S5_SERVICE]})


async def _s5_change_dossier(client) -> EvidenceDossier:
    """Run ChangeSpecialist on the live code path: tools timeline absent (not shipped in the
    agent image), direct Cloud Run Admin API, audit-log query returning nothing."""
    creds = SimpleNamespace(valid=True, token="t")
    logging_client = SimpleNamespace(list_entries=lambda **kw: [])
    with patch("tools.app.server.get_change_timeline", side_effect=ImportError("not shipped")), \
            patch("google.auth.default", return_value=(creds, PROJECT)), \
            patch("requests.get", side_effect=_admin_api_get), \
            patch("google.cloud.logging.Client", return_value=logging_client):
        return await ChangeSpecialist(metric_client=client).investigate(_alert(), _cfg())


def _cr_dossier() -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="CloudRunSpecialist",
        target_resource=SERVICE,
        findings=["Application error logs indicate database authentication failure."],
        metrics={"has_db_auth_error": True, "error_rate_5xx": "97.3%"},
        anomaly_detected=True,
    )


def _rollback(dossier: EvidenceDossier):
    return SynthesizerAgent()._synthesize_recommendations(
        [dossier, _cr_dossier()], SERVICE, _alert(), region=REGION, project_id=PROJECT
    )[0]


# ---------------------------------------------------------------------------------------
# Bug 1: rollback target from traffic history
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_s5_rollback_targets_the_revision_that_served_not_the_previously_created_one():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: PINNED_PER_MINUTE, RETIRED_NEWER_REV: [0] * 30}})
    dossier = await _s5_change_dossier(client)

    assert dossier.anomaly_detected is True
    assert dossier.metrics["recent_revision"] == S5_REV
    # Creation order says 00140-p9m; it is recorded as such, never as the serving revision.
    assert dossier.metrics["previous_created_revision"] == RETIRED_NEWER_REV
    assert dossier.metrics["previous_serving_revision"] == PINNED_REV
    assert "previous_revision" not in dossier.metrics
    evidence = dossier.metrics["previous_serving_evidence"]
    assert PINNED_REV in evidence and "225 requests" in evidence
    assert "2026-10-27T08:56:02Z" in evidence and "2026-10-27T09:26:02Z" in evidence
    assert evidence in dossier.findings

    rollback = _rollback(dossier)
    assert rollback.command == (
        f"gcloud run services update-traffic {SERVICE} --to-revisions={PINNED_REV}=100 --region={REGION}"
    )
    assert RETIRED_NEWER_REV not in rollback.title + rollback.command + (rollback.rollback or "")
    assert evidence in rollback.title
    assert f"--to-revisions={S5_REV}=100" in rollback.rollback
    assert "--to-latest" not in rollback.command + rollback.rollback


@pytest.mark.asyncio
async def test_traffic_window_ends_at_the_deploy_and_uses_the_configured_lookback():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [5]}})
    await _s5_change_dossier(client)

    req = client.calls[0]
    deploy = datetime(2026, 10, 27, 9, 26, 2, tzinfo=timezone.utc)
    assert req.interval.end_time.timestamp() == int(deploy.timestamp())
    assert req.interval.start_time.timestamp() == int(deploy.timestamp()) - 30 * 60
    assert 'metric.type = "run.googleapis.com/request_count"' in req.filter
    assert f'resource.labels.service_name = "{SERVICE}"' in req.filter
    assert 'resource.type = "cloud_run_revision"' in req.filter
    assert f'resource.labels.location = "{REGION}"' in req.filter
    assert req.name == f"projects/{PROJECT}"

    other = previous_serving_revision(client, PROJECT, REGION, SERVICE, S5_REV, S5_CREATED, lookback_minutes=10)
    assert other["window_start"] == "2026-10-27T09:16:02Z"


@pytest.mark.asyncio
async def test_split_traffic_before_the_deploy_names_no_target_and_states_the_split(caplog):
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [90], RETIRED_OLDER_REV: [10]}})
    with caplog.at_level(logging.WARNING):
        dossier = await _s5_change_dossier(client)

    assert dossier.metrics["previous_serving_revision"] is None
    evidence = dossier.metrics["previous_serving_evidence"]
    assert "not determined as a single revision" in evidence
    assert f"{PINNED_REV} (90 requests)" in evidence and f"{RETIRED_OLDER_REV} (10 requests)" in evidence
    assert any("not determined" in r.getMessage() for r in caplog.records)

    rollback = _rollback(dossier)
    assert "could not be identified" in rollback.title
    assert evidence in rollback.title
    assert "update-traffic" not in rollback.command
    assert RETIRED_NEWER_REV not in rollback.title + rollback.command


@pytest.mark.asyncio
async def test_no_requests_before_the_deploy_says_so_instead_of_guessing():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [0, 0], RETIRED_NEWER_REV: [0]}})
    dossier = await _s5_change_dossier(client)

    assert dossier.metrics["previous_serving_revision"] is None
    assert "recorded no requests on any other revision" in dossier.metrics["previous_serving_evidence"]
    rollback = _rollback(dossier)
    assert "update-traffic" not in rollback.command
    assert "served before" not in rollback.title.replace("which revision served before", "")


@pytest.mark.asyncio
async def test_traffic_query_failure_is_logged_and_names_no_target(caplog):
    client = FakeRequestCountClient(error=RuntimeError("permission denied"))
    with caplog.at_level(logging.WARNING):
        dossier = await _s5_change_dossier(client)

    assert dossier.metrics["previous_serving_revision"] is None
    assert "query" in dossier.metrics["previous_serving_evidence"]
    assert "failed (RuntimeError)" in dossier.metrics["previous_serving_evidence"]
    assert any("Traffic history query" in r.getMessage() for r in caplog.records)
    assert "update-traffic" not in _rollback(dossier).command


def test_new_revision_requests_are_not_counted_as_previous_serving():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [8], S5_REV: [3]}})
    out = previous_serving_revision(client, PROJECT, REGION, SERVICE, S5_REV, S5_CREATED, 30)
    assert out["revision"] == PINNED_REV
    assert out["requests_by_revision"] == {PINNED_REV: 8}


def test_unknown_deploy_time_names_no_target():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [8]}})
    out = previous_serving_revision(client, PROJECT, REGION, SERVICE, S5_REV, None, 30)
    assert out["revision"] is None
    assert "creation time" in out["evidence"]
    assert client.calls == []


def test_synthesizer_never_names_a_revision_without_traffic_evidence(caplog):
    dossier = EvidenceDossier(
        specialist_name="ChangeSpecialist",
        target_resource=SERVICE,
        findings=[f"Recent revision deployment detected on `{SERVICE}` (5m ago): `{S5_REV}`."],
        metrics={
            "recent_revision": S5_REV,
            "change_service": SERVICE,
            "previous_created_revision": RETIRED_NEWER_REV,
            "previous_serving_revision": PINNED_REV,
            "previous_serving_evidence": "",
            "all_changes": [{"service": SERVICE, "revision": S5_REV, "previous_created_revision": RETIRED_NEWER_REV}],
        },
        anomaly_detected=True,
    )
    with caplog.at_level(logging.WARNING):
        rollback = _rollback(dossier)
    assert "update-traffic" not in rollback.command
    assert PINNED_REV not in rollback.command and RETIRED_NEWER_REV not in rollback.command
    assert "could not be identified" in rollback.title
    assert any("no traffic evidence" in r.getMessage() for r in caplog.records)


def test_local_mode_without_a_client_reports_traffic_history_not_queried(caplog):
    import asyncio

    with patch("tools.app.server.get_change_timeline", return_value={"changes": [{
        "source": "cloud_run", "service_name": SERVICE, "revision_name": S5_REV,
        "previous_revision": RETIRED_NEWER_REV, "timestamp": S5_CREATED,
        "is_serving": True, "differs_from_previous": True,
    }]}), caplog.at_level(logging.WARNING):
        dossier = asyncio.run(ChangeSpecialist().investigate(
            _alert(), AgentConfig(project_id=PROJECT, region=REGION, is_local=True)
        ))
    assert dossier.metrics["previous_created_revision"] == RETIRED_NEWER_REV
    assert dossier.metrics["previous_serving_revision"] is None
    assert "was not queried" in dossier.metrics["previous_serving_evidence"]


def test_revision_diff_shows_the_serving_revision_not_the_created_one():
    dossier = EvidenceDossier(
        specialist_name="ChangeSpecialist",
        target_resource=SERVICE,
        metrics={"recent_revision": S5_REV, "previous_created_revision": RETIRED_NEWER_REV},
        anomaly_detected=True,
    )
    diffs = SynthesizerAgent()._build_revision_diffs([dossier], SERVICE)
    assert diffs[0]["previous_revision"] == "previous serving revision could not be identified"
    dossier.metrics["previous_serving_revision"] = PINNED_REV
    assert SynthesizerAgent()._build_revision_diffs([dossier], SERVICE)[0]["previous_revision"] == PINNED_REV


def test_lookback_is_configurable_with_a_sane_default(monkeypatch):
    assert AgentConfig().rollback_traffic_lookback_minutes == 30
    monkeypatch.setenv("ROLLBACK_TRAFFIC_LOOKBACK_MINUTES", "45")
    assert AgentConfig().rollback_traffic_lookback_minutes == 45


# ---------------------------------------------------------------------------------------
# Bug 2: recovery note states what is known, never implies a fix
# ---------------------------------------------------------------------------------------

from agent.app import recovery  # noqa: E402


class _CodeSeriesClient:
    """request_count series per (service, response_code)."""

    def __init__(self, by_service: dict[str, dict[str, list[int]]], error: Exception | None = None):
        self.by_service = by_service
        self.error = error
        self.calls = []

    def list_time_series(self, request=None, **kw):
        import re as _re

        self.calls.append(request)
        if self.error:
            raise self.error
        svc = _re.search(r'service_name = "([^"]+)"', request.filter).group(1)
        return [
            SimpleNamespace(
                metric=SimpleNamespace(labels={"response_code": code}),
                points=[SimpleNamespace(value=SimpleNamespace(int64_value=c)) for c in counts],
            )
            for code, counts in self.by_service.get(svc, {}).items()
        ]


# S5 group: latency (inventory), inventory-5xx, orders-5xx; last close 09:59:53Z.
S5_GROUP_ALERTS = [
    {"incident_id": "0.oeb70rdzff26", "resource_name": "ata-inventory-api", "state": "closed"},
    {"incident_id": "0.oeb71clke0b0", "resource_name": "ata-inventory-api", "state": "closed"},
    {"incident_id": "0.oeb72jihl76i", "resource_name": "ata-orders-api", "state": "closed"},
]
S5_RECOVERED = datetime(2026, 10, 27, 10, 0, 13, tzinfo=timezone.utc).timestamp()
S5_NOTE_AT = S5_RECOVERED + 180


def _note(alerts, client=None, error_factory=False):
    cfg = AgentConfig(project_id=PROJECT, region=REGION, is_local=True)
    recovery.set_recovery_metric_client_factory(
        (lambda s: (_ for _ in ()).throw(RuntimeError("no creds"))) if error_factory else (lambda s: client)
    )
    try:
        return recovery.recovery_note_for_group(alerts, S5_RECOVERED, S5_NOTE_AT, 180, cfg)
    finally:
        recovery.set_recovery_metric_client_factory(None)


def test_s5_recovery_note_says_alerts_closed_with_no_traffic_and_asks_for_verification():
    client = _CodeSeriesClient({})  # traffic generator stopped: no request_count points
    note = _note(S5_GROUP_ALERTS, client)

    assert note.startswith("Monitoring alerts closed: all 3 alerts of this incident closed by 2026-10-27T10:00:13Z")
    assert "Monitoring recovered" not in note
    assert "no recurrence" not in note and "verify the fix" not in note
    assert "not proof of a fix" in note
    window = "between 2026-10-27T09:55:13Z and 2026-10-27T10:03:13Z"
    assert f"Cloud Monitoring recorded no requests on ata-inventory-api {window}" in note
    assert f"no requests on ata-orders-api {window}" in note
    assert "(missing data)" in note
    assert note.endswith("A human must verify the remediation (for example, that the faulty change was reverted "
                         "or the injected fault was cleared) before resolving this incident.")
    # One query per distinct Cloud Run service, not per alert.
    assert len(client.calls) == 2
    # Scoped to the configured project and region (review R2).
    for req in client.calls:
        assert req.name == f"projects/{PROJECT}"
        assert 'resource.type = "cloud_run_revision"' in req.filter
        assert f'resource.labels.location = "{REGION}"' in req.filter


OTHER_REGION = "europe-west4"


def test_rollback_traffic_query_uses_the_given_region_not_a_default():
    """Review R2: a same-named service in another region must not be counted."""
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [5]}})
    out = previous_serving_revision(client, PROJECT, OTHER_REGION, SERVICE, S5_REV, S5_CREATED, 30)
    assert out["revision"] == PINNED_REV
    (req,) = client.calls
    assert f'resource.labels.location = "{OTHER_REGION}"' in req.filter
    assert f'location = "{REGION}"' not in req.filter


def test_change_specialist_passes_config_region_to_the_traffic_query():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [5]}})
    cfg = AgentConfig(project_id=PROJECT, region=OTHER_REGION, is_local=False)
    chg = {"service": SERVICE, "revision": S5_REV, "timestamp": S5_CREATED}
    out = ChangeSpecialist(metric_client=client)._previous_serving(chg, cfg)
    assert out["revision"] == PINNED_REV
    (req,) = client.calls
    assert f'resource.labels.location = "{OTHER_REGION}"' in req.filter


def test_recovery_note_query_uses_the_configured_region():
    client = _CodeSeriesClient({})
    cfg = AgentConfig(project_id=PROJECT, region=OTHER_REGION, is_local=True)
    recovery.set_recovery_metric_client_factory(lambda s: client)
    try:
        recovery.recovery_note_for_group(S5_GROUP_ALERTS[:1], S5_RECOVERED, S5_NOTE_AT, 180, cfg)
    finally:
        recovery.set_recovery_metric_client_factory(None)
    (req,) = client.calls
    assert f'resource.labels.location = "{OTHER_REGION}"' in req.filter
    assert 'resource.labels.service_name = "ata-inventory-api"' in req.filter


def test_rollback_and_recovery_share_one_request_count_filter():
    """Review C1: one helper builds the request_count query for both callers."""
    from agent.app import request_count
    from agent.app.specialists import change

    assert change.REQUEST_COUNT_METRIC is request_count.REQUEST_COUNT_METRIC
    assert change.request_count_series is request_count.request_count_series
    assert recovery.request_count_series is request_count.request_count_series
    assert request_count.request_count_filter("r1", "svc") == (
        'metric.type = "run.googleapis.com/request_count" AND resource.type = "cloud_run_revision" '
        'AND resource.labels.location = "r1" AND resource.labels.service_name = "svc"'
    )


def test_recovery_note_states_measured_traffic_and_5xx_when_requests_continue():
    client = _CodeSeriesClient({"ata-inventory-api": {"200": [40, 41], "500": [2, 1]}})
    note = _note(S5_GROUP_ALERTS[:2], client)
    assert "ata-inventory-api received 84 requests between" in note and "3 with HTTP 5xx" in note
    assert "A human must verify the remediation" in note


def test_recovery_note_does_not_measure_non_cloud_run_resources():
    note = _note([{"incident_id": "0.db", "resource_name": "ata-inventory-db"}], _CodeSeriesClient({}))
    assert note.startswith("Monitoring alerts closed: the alert of this incident")
    assert "not measured for ata-inventory-db (not a Cloud Run service)" in note
    assert "no requests" not in note


def test_recovery_note_query_failure_and_missing_client_are_stated(caplog):
    with caplog.at_level(logging.WARNING):
        failed = _note(S5_GROUP_ALERTS[:1], _CodeSeriesClient({}, error=RuntimeError("denied")))
        no_client = _note(S5_GROUP_ALERTS[:1], error_factory=True)
    assert "not checked for ata-inventory-api: the query failed (RuntimeError)" in failed
    assert "not checked for ata-inventory-api: no Cloud Monitoring client is available" in no_client
    assert any("Recovery traffic check" in r.getMessage() for r in caplog.records)


def test_recovery_lookback_is_configurable(monkeypatch):
    assert AgentConfig().recovery_traffic_lookback_seconds == 300
    monkeypatch.setenv("RECOVERY_TRAFFIC_LOOKBACK_SECONDS", "600")
    assert AgentConfig().recovery_traffic_lookback_seconds == 600


# ---------------------------------------------------------------------------------------
# Bug 4: review follow-ups (access-log cap, GatewayTimeout stated once, nits)
# ---------------------------------------------------------------------------------------

from tests.test_qafix1_quality import S2_GT, _s2_dossiers, _s3_apigee_dossier  # noqa: E402


@pytest.mark.asyncio
async def test_gateway_timeout_fact_is_stated_once_in_the_report(tmp_path):
    cr, ap = _s2_dossiers(S2_GT)
    empty = lambda name: EvidenceDossier(specialist_name=name, target_resource=SERVICE)  # noqa: E731
    synth = SynthesizerAgent()

    async def _ret(d):
        return d

    synth.cloud_run_specialist.investigate = lambda alert, config, target_service=None: _ret(
        cr if target_service in (None, SERVICE) else empty("CloudRunSpecialist")
    )
    synth.apigee_specialist.investigate = lambda alert, config: _ret(ap)
    synth.cloud_sql_specialist.investigate = lambda alert, config: _ret(empty("CloudSQLSpecialist"))
    synth.change_specialist.investigate = lambda alert, config: _ret(empty("ChangeSpecialist"))
    alert = AlertEvent(incident_id="0.oeb4hxzrsiqu", policy_name="ata-inventory-latency",
                       resource_name=SERVICE, started_at="2026-10-27T07:40:49Z")
    cfg = AgentConfig(project_id=PROJECT, region=REGION, is_local=True,
                      incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path))

    report = await synth.execute_investigation(alert, "g", "incident-g.md", cfg)

    marker = "did not complete within the proxy's target timeout"
    assert marker in report.probable_root_cause.text
    assert marker not in report.observations
    assert not any(marker in str(e) for e in report.root_cause_analysis.get("supporting_evidence", []))


@pytest.mark.asyncio
async def test_access_log_cap_hit_is_stated_in_findings_evidence_and_observations():
    dossier, alert = await _s3_apigee_dossier(apigee_access_log_max_entries=100)
    cap = dossier.metrics["access_log_cap_hit"]
    assert cap["cap"] == 100
    assert any("hit the 100-entry read cap (APIGEE_ACCESS_LOG_MAX_ENTRIES)" in f for f in dossier.findings)
    assert any(e.headline == "Apigee access log truncated: ata-orders-v1" for e in dossier.evidence_items)
    obs = SynthesizerAgent()._synthesize_observations([dossier], alert)
    assert "Apigee access log for ata-orders-v1 hit the 100-entry read cap" in obs
    assert "lower bounds" in obs


@pytest.mark.asyncio
async def test_default_cap_covers_the_s3_log_with_margin():
    assert AgentConfig().apigee_access_log_max_entries == 5000
    dossier, _ = await _s3_apigee_dossier()
    assert "access_log_cap_hit" not in dossier.metrics
    assert dossier.metrics["proxy_revision_change"]["new_revision"] == "162"


def test_spike_arrest_commands_use_the_shared_constants():
    from agent.app.specialists import apigee

    dossier = EvidenceDossier(specialist_name="ApigeeSpecialist", target_resource="ata-orders-v1", metrics={})
    actions = SynthesizerAgent()._apigee_recommendations([dossier], "ata-orders-v1", PROJECT)
    cmd = actions[-1].command
    assert apigee.access_log_name(PROJECT) in cmd
    assert f'jsonPayload.fault_source="{apigee.SPIKE_ARREST_FAULT}"' in cmd


def test_measured_restarts_stay_in_the_service_bullet():
    from agent.app.contracts.report import split_observations

    cr = EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource=SERVICE,
                         metrics={"error_rate_5xx": "84.0%", "request_count_rpm": 42, "container_restarts": 2})
    obs = SynthesizerAgent()._synthesize_observations([cr], _alert())
    line = next(o for o in split_observations(obs) if "HTTP 5xx error rate" in o)
    assert line == "HTTP 5xx error rate 84.0% (42 rpm) on ata-inventory-api, with 2 container restarts"
    assert ";" not in line


def test_triage_uses_the_module_level_db_caller_helper():
    from agent.app import triage
    from agent.app.specialists.synthesizer import db_caller_actions

    assert triage.db_caller_actions is db_caller_actions
    titles = [a.title for a in db_caller_actions("ata-inventory-db", PROJECT)]
    assert titles == ["Inspect connection pool release logic and pool size in ata-inventory-api, which calls ata-inventory-db"]


# ---------------------------------------------------------------------------------------
# QA minor/nit items (qa-results.md FINAL SUMMARY 10-13, 16)
# ---------------------------------------------------------------------------------------

from agent.app.contracts.report import InvestigationReport, ProbableRootCause, split_observations  # noqa: E402
from agent.app.specialists.synthesizer import (  # noqa: E402
    apply_correlation_note,
    apply_group_alerts,
    correlation_observation,
    highest_severity_priority,
)


def _sql_dossier(conns, max_conns=None):
    metrics = {"active_connections": conns}
    if max_conns is not None:
        metrics["max_connections"] = max_conns
    return EvidenceDossier(specialist_name="CloudSQLSpecialist", target_resource="ata-inventory-db", metrics=metrics)


def test_qa10_no_db_connections_line_on_a_spike_arrest_ticket():
    alert = AlertEvent(incident_id="0.oeb5qvfcvh5r", policy_name="ata-apigee-orders-429",
                       resource_name="ata-orders-v1", started_at="2026-10-27T08:35:28Z")
    obs = SynthesizerAgent()._synthesize_observations([_sql_dossier(2, 25)], alert)
    assert "Database active connections" not in obs


def test_qa10_unknown_connection_limit_is_never_filled_in():
    obs = SynthesizerAgent()._synthesize_observations([_sql_dossier(21, "N/A (not captured)")], _alert())
    assert "Database active connections: 21 on ata-inventory-db (max_connections not captured)" in obs
    assert "of 25" not in obs and "threshold" not in obs


def _s5_group_alerts():
    return [
        {"incident_id": "0.oeb70rdzff26", "policy_name": "ata-inventory-latency", "resource_name": SERVICE,
         "component": SERVICE, "signal": "latency", "labels": {"severity": "p2", "signal": "latency"}},
        {"incident_id": "0.oeb71clke0b0", "policy_name": "ata-inventory-5xx", "resource_name": SERVICE,
         "component": SERVICE, "signal": "5xx", "labels": {"severity": "p1", "signal": "5xx"}},
        {"incident_id": "0.oeb72jihl76i", "policy_name": "ata-orders-5xx", "resource_name": "ata-orders-api",
         "component": "ata-orders-api", "signal": "5xx", "labels": {"severity": "p1", "signal": "5xx"}},
    ]


def _s5_report(priority="P2"):
    return InvestigationReport(
        group_id="ata-inventory-latency-ata-inventory-api-20261027t093118z", summary="s", observations="o",
        probable_root_cause=ProbableRootCause(text="x", confidence=0.96), origin=SERVICE,
        blast_radius=[SERVICE], priority=priority, alerts=[dict(_s5_group_alerts()[0])],
    )


def test_qa11_priority_is_raised_to_the_most_severe_alert_in_the_group():
    assert highest_severity_priority(_s5_group_alerts()) == "P1"
    report = apply_group_alerts(_s5_report("P2"), _s5_group_alerts())
    assert report.priority == "P1"
    # Never lowered.
    p1 = apply_group_alerts(_s5_report("P1"), [_s5_group_alerts()[0]])
    assert p1.priority == "P1"
    assert highest_severity_priority([{"incident_id": "x"}]) is None


def test_qa11_signal_line_lists_every_signal_on_the_component():
    report = apply_group_alerts(_s5_report(), _s5_group_alerts())
    html = report.build_executive_summary_html()
    assert f"Component: <code>{SERVICE}</code>, Signal: <code>latency, 5xx</code>" in html


def test_qa11_s5_title_names_the_database_authentication_failure():
    chg = EvidenceDossier(specialist_name="ChangeSpecialist", target_resource=SERVICE, anomaly_detected=True,
                          metrics={"recent_revision": S5_REV, "change_service": SERVICE})
    title = SynthesizerAgent()._synthesize_title_problem(_alert(), [chg, _cr_dossier()], SERVICE)
    assert title == "Deployment regression: database authentication failure"
    plain = EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource=SERVICE, metrics={})
    assert SynthesizerAgent()._synthesize_title_problem(_alert(), [chg, plain], SERVICE) == "Deployment regression"


@pytest.mark.parametrize("corr, expected", [
    ({"decision": "new_group", "best_score": 0.0, "open_groups_compared": 0},
     "Correlation: opened a new incident group (no other open incident group to compare with)"),
    ({"decision": "new_group", "best_score": 22.5, "open_groups_compared": 1,
      "threshold_auto_new": 35.0, "threshold_auto_attach": 65.0},
     "Correlation: opened a new incident group; best correlation score 22.5 pts against 1 open group, "
     "below the 35 pts new-group threshold"),
    ({"decision": "new_group", "best_score": 50.0, "open_groups_compared": 2, "is_gray_zone": True,
      "threshold_auto_new": 35.0, "threshold_auto_attach": 65.0},
     "Correlation: opened a new incident group; best correlation score 50 pts against 2 open groups "
     "(between 35 and 65 pts; the gray-zone review decided a new group)"),
])
def test_qa12_correlation_decision_is_stated(corr, expected):
    assert correlation_observation(corr) == expected


def test_qa12_correlation_note_goes_after_the_alert_lines_once():
    report = _s5_report()
    report.observations = "Cloud Monitoring policy 'x' triggered | Correlated alert: y | HTTP 5xx error rate 97%"
    corr = {"decision": "new_group", "best_score": 0.0, "open_groups_compared": 0}
    apply_correlation_note(report, corr)
    apply_correlation_note(report, corr)
    items = split_observations(report.observations)
    assert items[2].startswith("Correlation: opened a new incident group")
    assert sum(i.startswith("Correlation:") for i in items) == 1
    apply_correlation_note(report, None)  # no decision recorded: nothing added


def test_qa12_new_group_records_the_correlation_decision(tmp_path):
    from agent.app.grouping.manager import GroupingManager
    from agent.app.grouping.store import OKFStore
    from agent.app.timers.scheduler import set_timer_scheduler_hook

    cfg = AgentConfig(project_id=PROJECT, region=REGION, is_local=True,
                      incident_store_path=str(tmp_path), fallback_store_path=str(tmp_path))
    set_timer_scheduler_hook(lambda **kw: "mock-task")
    try:
        mgr = GroupingManager(config=cfg)
        res = mgr.ingest_alert(AlertEvent(incident_id="0.s4", policy_name="ata-cloudsql-connections-critical",
                                           resource_name="ata-inventory-db", started_at="2026-10-27T08:58:25Z"))
    finally:
        set_timer_scheduler_hook(None)
    doc = OKFStore(config=cfg).read(res.group_id)
    assert doc.correlation["decision"] == "new_group"
    assert doc.correlation["open_groups_compared"] == 0
    assert correlation_observation(doc.correlation).startswith("Correlation: opened a new incident group")


def test_qa13_connection_failure_without_auth_signature_is_a_separate_weaker_signal():
    from agent.app.specialists.cloud_run import has_db_auth_signature, has_db_connect_failure_signature

    changed_text = "Failed to connect to Cloud SQL: connector error code 7"
    assert not has_db_auth_signature(changed_text)
    assert has_db_connect_failure_signature(changed_text)
    # The live S5 line still matches the specific signature.
    assert has_db_auth_signature("failed to connect to cloud sql: 'nonetype' object has no attribute 'decode'")

    cr = EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource=SERVICE, anomaly_detected=True,
                         metrics={"has_db_connect_error": True, "error_rate_5xx": "97.3%"})
    synth = SynthesizerAgent()
    facts = synth._format_dominant_signal_facts(_alert(), cr)
    assert facts == " (database connection failures in application logs (HTTP 5xx error rate: 97.3%))"
    assert "authentication" not in facts
    obs = synth._synthesize_observations([cr], _alert())
    assert "Database connection failures observed in application logs on ata-inventory-api" in obs
    chg = EvidenceDossier(specialist_name="ChangeSpecialist", target_resource=SERVICE, anomaly_detected=True,
                          metrics={"recent_revision": S5_REV, "change_service": SERVICE})
    actions = synth._synthesize_recommendations([chg, cr], SERVICE, _alert(), region=REGION, project_id=PROJECT)
    assert actions[1].title == f"Verify database connection settings and environment variables in {S5_REV}"


def test_qa16b_problem_uses_the_origin_latency_and_no_p95_label():
    origin_cr = EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource=SERVICE,
                                anomaly_detected=True, metrics={"latency_p95_ms": 6546.0, "error_rate_5xx": "0.0%"})
    caller_cr = EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource="ata-orders-api",
                                anomaly_detected=True, metrics={"latency_p95_ms": 6638.0, "error_rate_5xx": "0.0%"})
    alert = AlertEvent(incident_id="0.oeb4hxzrsiqu", policy_name="ata-inventory-latency",
                       resource_name=SERVICE, started_at="2026-10-27T07:40:49Z")
    synth = SynthesizerAgent()
    blast = [SERVICE, "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]
    problem = synth._synthesize_problem(alert, [origin_cr, caller_cr], SERVICE, blast)
    root, _ = synth._determine_root_cause([origin_cr, caller_cr], SERVICE, alert, blast_radius=blast)
    assert "6638" not in problem
    assert "P95" not in problem + root
    assert "peak observed latency: 6546ms" in root


def test_qa16c_5xx_ticket_has_no_latency_boilerplate():
    alert = AlertEvent(incident_id="0.s1", policy_name="ata-inventory-5xx", resource_name=SERVICE,
                       policy_user_labels={"signal": "5xx"}, started_at="2026-10-27T07:17:37Z")
    actions = SynthesizerAgent()._synthesize_recommendations([], SERVICE, alert, region=REGION, project_id=PROJECT)
    text = " ".join(f"{a.title} {a.verification}" for a in actions)
    assert "latency" not in text.lower() and "slow queries" not in text
    assert "HTTP 5xx error rate of ata-inventory-api returns below the alert threshold" in text
