"""Regression tests for the QA round-2 S3 bugs (INC0010330 inventory, INC0010331 orders).

1. Swallowed Cloud Logging quota error (ata-agent 2026-10-27T13:03:22Z): the ata-apigee-access
   read failed with "429 Quota exceeded ... Read requests per minute", the failure was swallowed
   and the ticket said "Change-Induced: No". Quota errors are now retried with bounded backoff;
   when the data stays unavailable the report says change detection was not assessed. The same
   pattern (a failed read turned into a negative finding) is fixed in every specialist.
2. Cross-group evidence leak: the orders 429 ticket carried the inventory 5xx rate and log
   excerpt. Upstream dependencies investigated only to choose the origin are no longer
   reported unless they are part of the incident.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from google.api_core import exceptions as gexc
import pytest

from agent.app.config import AgentConfig, reset_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.specialists import telemetry_reads
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.change import ChangeSpecialist
from agent.app.specialists.cloud_run import CloudRunSpecialist
from agent.app.specialists.cloud_sql import CloudSQLSpecialist
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.specialists.telemetry_reads import (
    UNAVAILABLE_KEY,
    call_with_quota_retry,
    is_quota_exhausted,
    read_log_entries,
)

QUOTA_MSG = (
    "Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute' "
    "of service 'logging.googleapis.com'"
)
NOT_ASSESSED = (
    "change detection incomplete: Cloud Logging read quota exceeded; change attribution not assessed"
)
SA_FAULT = "policies.ratelimit.SpikeArrestViolation"
# Live S3: orders-v1 rev 169 deployed 12:52:24Z, 429 alert opened 13:01:05Z.
DEPLOY = datetime(2026, 10, 27, 12, 52, 24, tzinfo=timezone.utc)
ORDERS_ALERT_AT = "2026-10-27T13:01:05Z"
INVENTORY_ALERT_AT = "2026-10-27T12:58:40Z"


def _quota_error() -> Exception:
    return gexc.ResourceExhausted(QUOTA_MSG)


@pytest.fixture
def cfg(tmp_path):
    config = AgentConfig(
        project_id="cloud-assist-fde-2",
        region="us-west1",
        incident_store_path=str(tmp_path / "knowledge/incident-manager"),
        fallback_store_path=str(tmp_path / "fallback"),
        is_local=True,
        gcp_read_retry_attempts=3,
        gcp_read_retry_base_seconds=0.0,
        gcp_read_retry_max_seconds=0.0,
    )
    reset_agent_settings(config)
    yield config
    reset_agent_settings(None)


@pytest.fixture(autouse=True)
def no_tools_timeline():
    # The agent image does not ship ata-tools; keep tests off any live change timeline.
    with patch("tools.app.server.get_change_timeline", side_effect=ImportError("not in agent image")):
        yield


# ---------------------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------------------


def test_quota_errors_are_recognized():
    assert is_quota_exhausted(gexc.ResourceExhausted("x"))
    assert is_quota_exhausted(gexc.TooManyRequests("x"))
    assert not is_quota_exhausted(gexc.PermissionDenied("x"))
    assert not is_quota_exhausted(ValueError("429 in the text is not enough"))


@pytest.mark.asyncio
async def test_429_then_success_retries_with_bounded_jittered_backoff():
    config = AgentConfig(gcp_read_retry_attempts=5, gcp_read_retry_base_seconds=2.0, gcp_read_retry_max_seconds=5.0)
    calls = {"n": 0}
    sleeps: list[float] = []
    caps: list[float] = []

    def fn():
        calls["n"] += 1
        if calls["n"] <= 3:
            raise _quota_error()
        return "entries"

    async def fake_sleep(s):
        sleeps.append(s)

    def jitter(lo, hi):
        caps.append(hi)
        return hi / 2

    result = await call_with_quota_retry(fn, what="t", config=config, sleep=fake_sleep, jitter=jitter)
    assert result == "entries"
    assert calls["n"] == 4
    # Exponential caps 2, 4, then bounded by GCP_READ_RETRY_MAX_SECONDS; full jitter below the cap.
    assert caps == [2.0, 4.0, 5.0]
    assert sleeps == [1.0, 2.0, 2.5]


@pytest.mark.asyncio
async def test_429_exhausted_raises_after_configured_attempts():
    config = AgentConfig(gcp_read_retry_attempts=3, gcp_read_retry_base_seconds=0.0, gcp_read_retry_max_seconds=0.0)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise _quota_error()

    async def fake_sleep(_):
        return None

    with pytest.raises(gexc.ResourceExhausted):
        await call_with_quota_retry(fn, what="t", config=config, sleep=fake_sleep)
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_non_quota_error_is_not_retried():
    config = AgentConfig(gcp_read_retry_attempts=5)
    calls = {"n": 0}

    def fn():
        calls["n"] += 1
        raise gexc.PermissionDenied("no")

    with pytest.raises(gexc.PermissionDenied):
        await call_with_quota_retry(fn, what="t", config=config)
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_log_read_uses_configured_page_size_not_api_default():
    client = MagicMock()
    client.list_entries.return_value = iter([1, 2])
    config = AgentConfig(logging_read_page_size=1000)
    out = await read_log_entries(client, filter_="f", max_results=5000, config=config, what="t", order_by="asc")
    assert out == [1, 2]
    client.list_entries.assert_called_once_with(filter_="f", max_results=5000, page_size=1000, order_by="asc")
    small = MagicMock()
    small.list_entries.return_value = []
    await read_log_entries(small, filter_="f", max_results=20, config=config, what="t")
    assert small.list_entries.call_args.kwargs["page_size"] == 20


# ---------------------------------------------------------------------------------------
# Fake GCP layer for the S3 shape (routes by query filter, so two investigations can share it)
# ---------------------------------------------------------------------------------------


def _series(labels: dict, values: list[int], start: datetime, resource_labels: dict | None = None):
    ts = MagicMock()
    ts.metric.labels = labels
    ts.resource.labels = resource_labels or {}
    pts = []
    for i, v in enumerate(values):
        pt = MagicMock()
        pt.value.int64_value = v
        pt.value.distribution_value = None
        pt.interval.start_time.seconds = int((start + timedelta(minutes=i)).timestamp())
        pt.interval.start_time.timestamp = lambda s=int((start + timedelta(minutes=i)).timestamp()): float(s)
        pts.append(pt)
    ts.points = pts
    return ts


def _inventory_log(i: int):
    e = MagicMock()
    e.log_name = "projects/cloud-assist-fde-2/logs/run.googleapis.com%2Fstderr"
    e.payload = f"ERROR: inventory reservation failed: injected fault 500 (request {i})"
    e.text_payload = None
    e.http_request = None
    e.severity = "ERROR"
    e.timestamp = datetime(2026, 10, 27, 12, 57, tzinfo=timezone.utc) + timedelta(seconds=i)
    return e


def _access_log():
    entries = []
    for i in range(32):  # revision 168 before the deploy
        e = MagicMock()
        e.timestamp = DEPLOY - timedelta(seconds=60) + timedelta(seconds=i)
        e.payload = {"proxy": "ata-orders-v1", "revision": "168", "environment": "prod",
                     "status_code": "200", "fault_source": "", "client_ip": "34.61.176.32"}
        entries.append(e)
    for i in range(619):  # revision 169 after it, mostly SpikeArrest
        e = MagicMock()
        e.timestamp = DEPLOY + timedelta(seconds=i * 0.6)
        e.payload = {"proxy": "ata-orders-v1", "revision": "169", "environment": "prod",
                     "status_code": "429" if i < 595 else "200",
                     "fault_source": "SpikeArrestViolation" if i < 595 else "", "client_ip": "34.61.176.32"}
        entries.append(e)
    return entries


class FakeGCP:
    """Cloud Monitoring and Cloud Logging fakes keyed by the query filter."""

    def __init__(self, access_log_failures: int = 0):
        self.access_log_failures = access_log_failures
        self.access_log_calls = 0
        self.log_kwargs: list[dict] = []

    def list_time_series(self, request):
        f = request.filter
        t0 = datetime(2026, 10, 27, 12, 53, tzinfo=timezone.utc)
        if "run.googleapis.com/request_count" in f and '"ata-inventory-api"' in f:
            return [_series({"response_code": "500", "response_code_class": "5xx"}, [40, 50, 50, 49], t0),
                    _series({"response_code": "200", "response_code_class": "2xx"}, [7, 7, 7, 7], t0)]
        if "run.googleapis.com/request_count" in f and '"ata-orders-api"' in f:
            return [_series({"response_code": "200", "response_code_class": "2xx"}, [1, 1], t0)]
        if "apigee.googleapis.com/proxy/response_count" in f and '"ata-orders-v1"' in f:
            return [_series({"response_code": "429", "fault_code": SA_FAULT, "fault_source": "policy"},
                            [300, 295], t0, {"org": "cloud-assist-fde-2", "env": "prod"})]
        return []

    def list_entries(self, **kwargs):
        self.log_kwargs.append(kwargs)
        f = kwargs["filter_"]
        if "ata-apigee-access" in f:
            self.access_log_calls += 1
            if self.access_log_calls <= self.access_log_failures:
                raise _quota_error()
            return iter(_access_log() if "ata-orders-v1" in f else [])
        if 'service_name="ata-inventory-api"' in f:
            return iter([_inventory_log(i) for i in range(5)])
        return iter([])

    def patches(self):
        mon = patch("google.cloud.monitoring_v3.MetricServiceClient")
        log = patch("google.cloud.logging.Client")
        return mon, log

    def __enter__(self):
        self._mon, self._log = self.patches()
        mon_cls = self._mon.__enter__()
        log_cls = self._log.__enter__()
        mon_cls.return_value.list_time_series.side_effect = self.list_time_series
        log_cls.return_value.list_entries.side_effect = self.list_entries
        return self

    def __exit__(self, *exc):
        self._log.__exit__(*exc)
        self._mon.__exit__(*exc)


def _orders_alert() -> AlertEvent:
    return AlertEvent(
        incident_id="0.oebbt8o8w36t", policy_name="ata-apigee-orders-429", resource_name="ata-orders-v1",
        started_at=ORDERS_ALERT_AT, state="open", policy_user_labels={"signal": "rate-limit"},
    )


def _inventory_alert() -> AlertEvent:
    return AlertEvent(
        incident_id="0.oebbr95dk47e", policy_name="ata-inventory-5xx", resource_name="ata-inventory-api",
        started_at=INVENTORY_ALERT_AT, state="open", resource_labels={"service_name": "ata-inventory-api"},
        policy_user_labels={"signal": "5xx"},
    )


async def _orders_report(cfg, gcp: FakeGCP):
    with gcp:
        return await SynthesizerAgent().execute_investigation(
            _orders_alert(), "grp-orders", "incident-orders.md", cfg
        )


# ---------------------------------------------------------------------------------------
# Bug 1: quota error on the access-log read
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_access_log_429_then_success_keeps_change_detection(cfg):
    gcp = FakeGCP(access_log_failures=2)
    with gcp:
        dossier = await ApigeeSpecialist().investigate(_orders_alert(), cfg)
    assert gcp.access_log_calls == 3
    assert UNAVAILABLE_KEY not in dossier.metrics
    change = dossier.metrics["proxy_revision_change"]
    assert (change["previous_revision"], change["new_revision"]) == ("168", "169")
    assert (change["previous_requests"], change["new_requests"]) == (32, 619)


@pytest.mark.asyncio
async def test_access_log_single_read_per_window_with_large_pages(cfg):
    gcp = FakeGCP()
    with gcp:
        await ApigeeSpecialist().investigate(_orders_alert(), cfg)
    access = [k for k in gcp.log_kwargs if "ata-apigee-access" in k["filter_"]]
    assert len(access) == 1
    assert access[0]["page_size"] == cfg.logging_read_page_size
    assert access[0]["max_results"] == cfg.apigee_access_log_max_entries


@pytest.mark.asyncio
async def test_access_log_429_exhausted_reports_change_not_assessed(cfg):
    gcp = FakeGCP(access_log_failures=99)
    report = await _orders_report(cfg, gcp)
    assert gcp.access_log_calls == cfg.gcp_read_retry_attempts

    assert report.change_induced == f"Not assessed ({NOT_ASSESSED})"
    desc = report.build_structured_description(project_id=cfg.project_id, incidents_bucket="test-project-ata-incidents")
    assert f"Change-Induced: Not assessed ({NOT_ASSESSED})" in desc
    assert "Change-Induced: No\n" not in desc
    # Visible in the ticket observations (work note, description) and in the HTML evidence.
    assert "Evidence degraded: Apigee access log ata-apigee-access for ata-orders-v1 unavailable" in report.observations
    assert NOT_ASSESSED in report.observations
    change_items = report.evidence_by_area.get("change", [])
    assert any(i.headline == "Change detection incomplete" for i in change_items)
    apigee_items = report.evidence_by_area.get("apigee", [])
    assert any("Apigee access log unavailable" in i.headline for i in apigee_items)


@pytest.mark.asyncio
async def test_access_log_ok_does_not_mark_change_not_assessed(cfg):
    report = await _orders_report(cfg, FakeGCP())
    assert report.change_induced is None or not report.change_induced.startswith("Not assessed")
    assert "Evidence degraded" not in report.observations


# ---------------------------------------------------------------------------------------
# Bug 1 audit: no specialist turns a failed read into a negative finding
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cloud_run_failed_log_read_is_not_no_error_logs(cfg):
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls:
        log_cls.return_value.list_entries.side_effect = _quota_error()
        mon_cls.return_value.list_time_series.return_value = []
        dossier = await CloudRunSpecialist().investigate(_inventory_alert(), cfg)
    joined = " ".join(dossier.findings)
    assert "No active error logs" not in joined
    assert "could not be read (Cloud Logging read quota exceeded)" in joined
    assert dossier.metrics[UNAVAILABLE_KEY][0]["source"] == "Cloud Run application logs"


@pytest.mark.asyncio
async def test_cloud_run_failed_monitoring_is_not_baseline_operation(cfg):
    alert = AlertEvent(incident_id="0.x", policy_name="ata-apigee-orders-429", resource_name="ata-orders-v1",
                       started_at=ORDERS_ALERT_AT)
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls:
        log_cls.return_value.list_entries.return_value = []
        mon_cls.return_value.list_time_series.side_effect = gexc.ServiceUnavailable("down")
        dossier = await CloudRunSpecialist().investigate(alert, cfg, target_service="ata-orders-api")
    blob = " ".join(dossier.findings) + " ".join(i.content for i in dossier.evidence_items)
    assert "baseline operation" not in blob
    assert "health not assessed" in blob


@pytest.mark.asyncio
async def test_cloud_sql_failed_reads_are_not_healthy(cfg):
    config = cfg.model_copy(update={"is_local": False})
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.cloud.monitoring_v3.MetricServiceClient") as mon_cls, \
         patch("google.auth.default", side_effect=RuntimeError("no creds")):
        log_cls.return_value.list_entries.side_effect = _quota_error()
        mon_cls.return_value.list_time_series.side_effect = _quota_error()
        dossier = await CloudSQLSpecialist().investigate(_orders_alert(), config)
    joined = " ".join(dossier.findings)
    assert "are healthy" not in joined
    assert "health not assessed" in joined
    sources = {g["source"] for g in dossier.metrics[UNAVAILABLE_KEY]}
    assert "Cloud SQL lock-wait logs" in sources


@pytest.mark.asyncio
async def test_change_failed_audit_read_is_not_no_deployments(cfg):
    config = cfg.model_copy(update={"is_local": False})
    resp = MagicMock(status_code=200)
    resp.json.return_value = {"services": []}
    creds = MagicMock(valid=True, token="t")
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.auth.default", return_value=(creds, "p")), \
         patch("requests.get", return_value=resp):
        log_cls.return_value.list_entries.side_effect = _quota_error()
        dossier = await ChangeSpecialist(metric_client=MagicMock()).investigate(_orders_alert(), config)
    joined = " ".join(dossier.findings)
    assert "No configuration or revision deployments detected" not in joined
    assert "No Cloud Run revision deployments detected" not in joined
    assert NOT_ASSESSED.capitalize().split(":")[0] in joined
    assert "Cloud Logging read quota exceeded" in joined
    assert dossier.metrics[UNAVAILABLE_KEY][0]["affects_change_detection"] is True


@pytest.mark.asyncio
async def test_change_failed_admin_api_status_is_recorded(cfg):
    config = cfg.model_copy(update={"is_local": False})
    resp = MagicMock(status_code=403)
    creds = MagicMock(valid=True, token="t")
    with patch("google.cloud.logging.Client") as log_cls, \
         patch("google.auth.default", return_value=(creds, "p")), \
         patch("requests.get", return_value=resp):
        log_cls.return_value.list_entries.return_value = []
        dossier = await ChangeSpecialist(metric_client=MagicMock()).investigate(_orders_alert(), config)
    joined = " ".join(dossier.findings)
    assert "Cloud Run Admin API returned HTTP 403" in joined
    assert "No Cloud Run revision deployments detected" not in joined


def test_specialists_record_gaps_through_one_helper():
    # Every specialist reads Cloud Logging through the retrying helper.
    import inspect

    from agent.app.specialists import apigee, change, cloud_run, cloud_sql

    for mod in (apigee, change, cloud_run, cloud_sql):
        src = inspect.getsource(mod)
        assert "read_log_entries(" in src, mod.__name__
        assert ".list_entries(" not in src, mod.__name__
    assert telemetry_reads.UNAVAILABLE_KEY == UNAVAILABLE_KEY


# ---------------------------------------------------------------------------------------
# Bug 2: concurrent disjoint investigations keep their own evidence
# ---------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_disjoint_investigations_keep_their_own_evidence(cfg):
    gcp = FakeGCP()
    with gcp:
        inventory, orders = await asyncio.gather(
            SynthesizerAgent().execute_investigation(_inventory_alert(), "grp-inv", "incident-inv.md", cfg),
            SynthesizerAgent().execute_investigation(_orders_alert(), "grp-ord", "incident-ord.md", cfg),
        )

    # Inventory report: its own 5xx rate and its own 500 log excerpt.
    assert inventory.origin == "ata-inventory-api"
    assert "on ata-inventory-api" in inventory.observations
    assert "Log excerpt (ata-inventory-api)" in inventory.observations

    # Orders report: only the orders proxy's evidence. No inventory (or orders-api) observation,
    # log excerpt or evidence item, though both are upstream of ata-orders-v1 in the topology.
    assert orders.origin == "ata-orders-v1"
    assert "ata-inventory-api" not in orders.observations
    assert "ata-orders-api" not in orders.observations
    assert "HTTP 5xx error rate" not in orders.observations
    for items in orders.evidence_by_area.values():
        for item in items:
            assert "ata-inventory-api" not in item.headline and "ata-inventory-api" not in item.content
            assert "injected fault 500" not in item.content
    assert "HTTP 429" in orders.observations


@pytest.mark.asyncio
async def test_upstream_origin_evidence_is_still_reported(cfg):
    # An upstream dependency that IS the origin (or in its blast radius) stays in the report.
    alert = AlertEvent(
        incident_id="0.orders5xx", policy_name="ata-orders-5xx", resource_name="ata-orders-api",
        started_at=INVENTORY_ALERT_AT, state="open", resource_labels={"service_name": "ata-orders-api"},
    )
    group_alerts = [
        {"incident_id": "0.orders5xx", "policy_name": "ata-orders-5xx", "resource_name": "ata-orders-api",
         "started_at": "2026-10-27T13:00:00Z", "state": "open"},
        {"incident_id": "0.inv5xx", "policy_name": "ata-inventory-5xx", "resource_name": "ata-inventory-api",
         "started_at": INVENTORY_ALERT_AT, "state": "open"},
    ]
    with FakeGCP():
        report = await SynthesizerAgent().execute_investigation(
            alert, "grp-up", "incident-up.md", cfg, group_alerts=group_alerts
        )
    assert report.origin == "ata-inventory-api"
    assert "on ata-inventory-api" in report.observations
