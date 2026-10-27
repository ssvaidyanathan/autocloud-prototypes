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

    other = previous_serving_revision(client, PROJECT, SERVICE, S5_REV, S5_CREATED, lookback_minutes=10)
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
    out = previous_serving_revision(client, PROJECT, SERVICE, S5_REV, S5_CREATED, 30)
    assert out["revision"] == PINNED_REV
    assert out["requests_by_revision"] == {PINNED_REV: 8}


def test_unknown_deploy_time_names_no_target():
    client = FakeRequestCountClient({SERVICE: {PINNED_REV: [8]}})
    out = previous_serving_revision(client, PROJECT, SERVICE, S5_REV, None, 30)
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
