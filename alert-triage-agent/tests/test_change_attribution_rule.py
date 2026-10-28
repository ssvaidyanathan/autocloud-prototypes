"""Change attribution rule: a deploy in the change window is the cause only by timing or health.

Before: any genuine deploy of an in-scope service in the 120-min change window made the
incident change_induced, with a rollback (live INC0010343: the 21:51Z re-pin of
ata-inventory-api was blamed for an S1 fault 59 min later, after 52 min of error-free
traffic; atat INC0010341/42 listed "Deployment regression on atat-orders-api-00001-qfx",
the stack's first revision, deployed about 55 min before onset). Now a deploy is the cause
only if

  (a) the alert onset is at most CHANGE_ONSET_WINDOW_MINUTES (default 15) after it, or
  (b) the new revision had no healthy period before the onset: no span of at least
      CHANGE_HEALTHY_PERIOD_MINUTES (default 10) in which every minute with requests on it
      stayed under the alert-policy thresholds (Cloud Monitoring, per revision).

Without healthy-period data only (a) applies, and the ticket says so. Other deploys are
listed under "Recent changes (likely unrelated)" with their evidence: no causation, no
rollback, Issue Type not change_induced.
"""

import asyncio
import re
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.app.config import AgentConfig
from agent.app.specialists.change import (
    ChangeSpecialist,
    assess_deploy_causation,
    healthy_period,
)
from agent.app.specialists.synthesizer import SynthesizerAgent
from tests.test_group_origin_selection import (  # noqa: F401  (fixtures)
    INVENTORY_BAD_REV,
    INVENTORY_GOOD_REV,
    ONSET,
    _all_action_text,
    _change,
    _entry,
    _event,
    agent_cfg,
    telemetry,
)
from tests.test_s3_evidence_quality import FakeGCP, _orders_report, cfg  # noqa: F401

ONSET_DT = datetime.fromisoformat(ONSET.replace("Z", "+00:00"))
PIN_REV = "ata-inventory-api-pin-20261027215128"
PIN_PREV = "ata-inventory-api-00073-9vb"


def _minute(offset_min: int) -> datetime:
    """The minute `offset_min` minutes before the onset (negative = before)."""
    return (ONSET_DT + timedelta(minutes=offset_min)).replace(second=0)


class FakeTrafficHistory:
    """Cloud Monitoring fake for request_count (by response code class) and request_latencies.

    `minutes` maps service -> revision -> [(minute, 2xx count, 5xx count, mean latency ms)].
    Like the real API, only points inside the request interval of the filtered service are
    returned, each with an interval end time.
    """

    def __init__(self, minutes: dict[str, dict[str, list[tuple]]] | None = None, error: Exception | None = None):
        self.minutes = minutes or {}
        self.error = error
        self.calls: list[Any] = []

    def list_time_series(self, request: Any = None, **kwargs: Any) -> list[Any]:
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        flt = request.filter
        service = re.search(r'resource\.labels\.service_name = "([^"]+)"', flt).group(1)
        start = request.interval.start_time.timestamp()
        end = request.interval.end_time.timestamp()
        series = []
        for revision, rows in self.minutes.get(service, {}).items():
            rows = [r for r in rows if start <= r[0].timestamp() + 60 <= end + 60]
            res = SimpleNamespace(labels={"revision_name": revision})
            if "request_latencies" in flt:
                pts = [
                    SimpleNamespace(
                        interval=SimpleNamespace(end_time=m + timedelta(minutes=1)),
                        value=SimpleNamespace(distribution_value=SimpleNamespace(count=ok + bad, mean=lat)),
                    )
                    for m, ok, bad, lat in rows if ok + bad
                ]
                series.append(SimpleNamespace(resource=res, metric=SimpleNamespace(labels={}), points=pts))
                continue
            for cls, idx in (("2xx", 1), ("5xx", 2)):
                pts = [
                    SimpleNamespace(
                        interval=SimpleNamespace(end_time=r[0] + timedelta(minutes=1)),
                        value=SimpleNamespace(int64_value=r[idx]),
                    )
                    for r in rows
                ]
                labels = {"response_code_class": cls, "response_code": "200" if cls == "2xx" else "500"}
                series.append(SimpleNamespace(resource=res, metric=SimpleNamespace(labels=labels), points=pts))
        return series


def _use_traffic(monkeypatch, client: FakeTrafficHistory) -> FakeTrafficHistory:
    monkeypatch.setattr(ChangeSpecialist, "_get_metric_client", lambda self, config: client)
    return client


# Live INC0010343, shifted to the fixture onset: re-pin 59 min before the onset; the pin
# revision served sparse error-free traffic (57 requests over 53 min), then 5xx from -6 min.
INC0010343_PIN = [
    (_minute(-59), 3, 0, 120.0), (_minute(-58), 7, 0, 95.0), (_minute(-52), 5, 0, 90.0),
    (_minute(-50), 1, 0, 80.0), (_minute(-49), 5, 0, 85.0), (_minute(-46), 5, 0, 88.0),
    (_minute(-45), 1, 0, 70.0), (_minute(-39), 4, 0, 90.0), (_minute(-38), 7, 0, 91.0),
    (_minute(-7), 3, 0, 110.0),
    (_minute(-6), 16, 58, 300.0), (_minute(-5), 16, 98, 250.0), (_minute(-4), 27, 84, 260.0),
]
INC0010343_TRAFFIC = {
    "ata-inventory-api": {
        PIN_PREV: [(_minute(-61), 1, 0, 90.0), (_minute(-60), 1, 0, 90.0)],
        PIN_REV: INC0010343_PIN,
    }
}
INC0010343_DEPLOY = (ONSET_DT - timedelta(minutes=59, seconds=2)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _inventory_5xx(telemetry, deploy_at: str, revision: str = PIN_REV, previous: str = PIN_PREV):
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    telemetry["changes"] = [_change("ata-inventory-api", revision, previous, deploy_at)]
    return _event(_entry("0.cr-inv", "ata-inventory-5xx", "ata-inventory-api", "5xx"))


def _investigate(alert, agent_cfg, group_alerts=None):
    return asyncio.run(
        SynthesizerAgent().execute_investigation(alert, "grp-cr", "incident-grp-cr.md", agent_cfg, group_alerts=group_alerts)
    )


def _desc(report) -> str:
    return report.build_structured_description(project_id="cloud-assist-fde-2", incidents_bucket="test-project-ata-incidents")


def _config(**kw) -> AgentConfig:
    return AgentConfig(project_id="p", name_prefix="ata", region="us-west1", is_local=True, **kw)


# ---------------------------------------------------------------------------------------
# INC0010343-shaped: deploy, 59 min healthy, then the fault => not change-induced
# ---------------------------------------------------------------------------------------


def test_inc0010343_deploy_then_healthy_period_then_fault_is_not_change_induced(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)

    expected = (
        f"revision {PIN_REV} on ata-inventory-api deployed {INC0010343_DEPLOY}: deployed 59m before the alert onset, "
        "beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); healthy period: Cloud Monitoring "
        f"shows {PIN_REV} served 41 requests from {_minute(-59):%Y-%m-%dT%H:%M:%SZ} to {_minute(-6):%Y-%m-%dT%H:%M:%SZ} "
        "(53 min, at least 10) with HTTP 5xx under 5% and mean latency under 2000 ms in every minute with requests, "
        f"before the alert onset at {ONSET}"
    )
    assert report.change_induced == (
        f"No (recent changes likely unrelated: {expected}; checked: ata-tools change timeline (Cloud Run revision "
        "history), ata-tools change timeline (Apigee deployment history))"
    )
    assert report.infer_issue_type() != "change_induced"
    desc = _desc(report)
    assert "Issue Type: change_induced" not in desc
    assert "Change-Induced: No (recent changes likely unrelated:" in desc
    assert "Deployment regression" not in desc
    assert "Deployment regression" not in (report.title_problem or "")
    # No rollback, no revision config check of the unrelated deploy.
    actions = _all_action_text(report)
    assert "update-traffic" not in actions
    assert f"revisions describe {PIN_REV}" not in actions
    # The deploy is listed with its evidence under "Recent changes (likely unrelated)".
    items = report.evidence_by_area["change"]
    unrelated = [i for i in items if i.headline == "Recent changes (likely unrelated)"]
    assert unrelated and expected in unrelated[0].content
    assert not any(i.headline.startswith("Recent Deployment Correlation") for i in items)
    assert not any("No recent Cloud Run deployments" in (i.content or "") for i in items)
    # The ruled-out deployment alternative carries the same evidence.
    alts = report.root_cause_analysis["alternatives_ruled_out"]
    assert any(a["reason"] == f"Recent changes (likely unrelated): {expected}." for a in alts)
    assert not report.revision_diffs


def test_inc0010343_shape_old_rule_window_still_reports_the_deploy_time(agent_cfg, telemetry, monkeypatch):
    """The listing states the deploy time and the healthy-period window (facts, not causation)."""
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    text = report.change_induced
    assert f"deployed {INC0010343_DEPLOY}" in text
    assert "caused" not in text.lower() and "regression" not in text.lower()


# ---------------------------------------------------------------------------------------
# S5-shaped: a bad revision failing immediately => change-induced, rollback
# ---------------------------------------------------------------------------------------


def test_s5_bad_revision_inside_onset_window_is_change_induced_with_rollback(agent_cfg, telemetry, monkeypatch):
    client = _use_traffic(monkeypatch, FakeTrafficHistory({
        "ata-inventory-api": {INVENTORY_GOOD_REV: [(_minute(-20 - i), 8, 0, 90.0) for i in range(10)]},
    }))
    deploy = (ONSET_DT - timedelta(minutes=6, seconds=52)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _investigate(_inventory_5xx(telemetry, deploy, INVENTORY_BAD_REV, INVENTORY_GOOD_REV), agent_cfg)

    assert report.change_induced.startswith(f"Yes (revision {INVENTORY_BAD_REV} on ata-inventory-api deployed {deploy} (6m")
    assert report.infer_issue_type() == "change_induced"
    first = report.get_normalized_actions()[0].command
    assert f"update-traffic ata-inventory-api --to-revisions={INVENTORY_GOOD_REV}=100" in first
    items = report.evidence_by_area["change"]
    corr = next(i for i in items if i.headline.startswith("Recent Deployment Correlation"))
    assert "within the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES)" in corr.content
    # Rule (a) needs no healthy-period read: only the previous-serving lookup hit Monitoring.
    assert all("request_latencies" not in c.filter for c in client.calls)


def test_s5_bad_revision_failing_from_its_first_request_beyond_window_is_change_induced(agent_cfg, telemetry, monkeypatch):
    """Low traffic: the alert fired 25 min after the deploy, but the revision never served normally."""
    _use_traffic(monkeypatch, FakeTrafficHistory({
        "ata-inventory-api": {
            INVENTORY_GOOD_REV: [(_minute(-30 - i), 8, 0, 90.0) for i in range(10)],
            INVENTORY_BAD_REV: [(_minute(-24 + i), 1, 7, 150.0) for i in range(20)],
        },
    }))
    deploy = (ONSET_DT - timedelta(minutes=25)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _investigate(_inventory_5xx(telemetry, deploy, INVENTORY_BAD_REV, INVENTORY_GOOD_REV), agent_cfg)

    assert report.change_induced.startswith(f"Yes (revision {INVENTORY_BAD_REV} on ata-inventory-api")
    assert report.infer_issue_type() == "change_induced"
    assert f"--to-revisions={INVENTORY_GOOD_REV}=100" in report.get_normalized_actions()[0].command
    corr = next(i for i in report.evidence_by_area["change"] if i.headline.startswith("Recent Deployment Correlation"))
    assert "no healthy period" in corr.content and "it had no minute of normal traffic" in corr.content
    assert "CHANGE_HEALTHY_PERIOD_MINUTES 10" in corr.content


# ---------------------------------------------------------------------------------------
# The boundary at N, both sides
# ---------------------------------------------------------------------------------------


def _deploy(mins: int, revision: str = PIN_REV, service: str = "ata-inventory-api") -> dict:
    return {
        "service": service, "revision": revision, "deployed_minutes_ago": mins,
        "timestamp": (ONSET_DT - timedelta(minutes=mins)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


HEALTHY_SINCE_DEPLOY = FakeTrafficHistory({
    "ata-inventory-api": {PIN_REV: [(_minute(-30 + i), 5, 0, 80.0) for i in range(28)]},
})


@pytest.mark.parametrize("mins,causal", [(15, True), (16, False)])
def test_onset_window_boundary_default(mins, causal):
    result = assess_deploy_causation(_deploy(mins), ONSET_DT, _config(), HEALTHY_SINCE_DEPLOY)
    assert result["causal"] is causal
    assert result["rule"] == ("a" if causal else None)
    if causal:
        assert result["evidence"] == (
            f"deployed {mins}m before the alert onset, within the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES)"
        )
    else:
        assert result["evidence"].startswith(
            f"deployed {mins}m before the alert onset, beyond the 15-minute change onset window"
        )
        assert result["healthy_period"]["status"] == "healthy"


@pytest.mark.parametrize("mins,causal", [(20, True), (21, False)])
def test_onset_window_boundary_follows_config(mins, causal):
    result = assess_deploy_causation(_deploy(mins), ONSET_DT, _config(change_onset_window_minutes=20), HEALTHY_SINCE_DEPLOY)
    assert result["causal"] is causal


@pytest.mark.parametrize("span,status", [(9, "no_healthy_period"), (10, "healthy")])
def test_healthy_period_boundary_at_m(span, status):
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: (
        [(_minute(-40 + i), 5, 0, 80.0) for i in range(span)] + [(_minute(-5), 2, 9, 90.0)]
    )}})
    result = healthy_period(
        client, "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=41)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, 10,
    )
    assert result["status"] == status
    assert result["minutes"] == span


def test_unhealthy_minute_splits_the_period_and_empty_minutes_do_not():
    rows = (
        [(_minute(-50), 5, 0, 80.0), (_minute(-44), 5, 0, 80.0)]  # 7-min span, gap inside
        + [(_minute(-43), 1, 5, 80.0)]  # 5xx 83 %
        + [(_minute(-42), 5, 0, 80.0), (_minute(-38), 5, 0, 2500.0)]  # latency breach
    )
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}})
    result = healthy_period(
        client, "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=51)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, 5,
    )
    assert result["status"] == "healthy" and result["minutes"] == 7
    strict = healthy_period(
        client, "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=51)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, 8,
    )
    assert strict["status"] == "no_healthy_period"
    assert f"first minute over the thresholds {_minute(-43):%Y-%m-%dT%H:%M:%SZ} (HTTP 5xx 83.3% of 6 requests)" in strict["evidence"]


def test_config_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("CHANGE_ONSET_WINDOW_MINUTES", raising=False)
    monkeypatch.delenv("CHANGE_HEALTHY_PERIOD_MINUTES", raising=False)
    cfg_default = _config()
    assert (cfg_default.change_onset_window_minutes, cfg_default.change_healthy_period_minutes) == (15, 10)
    monkeypatch.setenv("CHANGE_ONSET_WINDOW_MINUTES", "30")
    monkeypatch.setenv("CHANGE_HEALTHY_PERIOD_MINUTES", "5")
    assert (_config().change_onset_window_minutes, _config().change_healthy_period_minutes) == (30, 5)
    for name in ("CHANGE_ONSET_WINDOW_MINUTES", "CHANGE_HEALTHY_PERIOD_MINUTES"):
        for bad in ("0", "121", "abc"):
            monkeypatch.setenv(name, bad)
            with pytest.raises(ValidationError):
                _config()
        monkeypatch.setenv(name, "10")


# ---------------------------------------------------------------------------------------
# Missing healthy-period data: rule (a) only, and the ticket says so
# ---------------------------------------------------------------------------------------


def test_monitoring_failure_falls_back_to_onset_window_and_says_so(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(error=RuntimeError("503 backend unavailable")))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)

    assert report.change_induced.startswith("No (recent changes likely unrelated: ")
    assert (
        "deployed 59m before the alert onset, beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); "
        f"healthy-period data unavailable (the Cloud Monitoring query for {PIN_REV} between {INC0010343_DEPLOY} (deploy) "
        f"and {ONSET} (alert onset) failed (RuntimeError)), so only the onset-window rule was applied"
    ) in report.change_induced
    assert "Change-Induced: No (recent changes likely unrelated:" in _desc(report)
    assert "update-traffic" not in _all_action_text(report)


def test_no_requests_on_the_new_revision_falls_back_and_says_so(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-inventory-api": {PIN_PREV: INC0010343_TRAFFIC["ata-inventory-api"][PIN_PREV]}}))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    assert (
        f"healthy-period data unavailable (Cloud Monitoring run.googleapis.com/request_count recorded no requests on {PIN_REV} "
        f"between {INC0010343_DEPLOY} (deploy) and {ONSET} (alert onset)), so only the onset-window rule was applied"
    ) in report.change_induced
    assert report.infer_issue_type() != "change_induced"


def test_missing_data_inside_the_onset_window_is_still_change_induced(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(error=RuntimeError("503")))
    deploy = (ONSET_DT - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    report = _investigate(_inventory_5xx(telemetry, deploy, INVENTORY_BAD_REV, INVENTORY_GOOD_REV), agent_cfg)
    assert report.change_induced.startswith(f"Yes (revision {INVENTORY_BAD_REV}")


def test_no_metric_client_is_stated():
    result = assess_deploy_causation(_deploy(40), ONSET_DT, _config(), None)
    assert result["causal"] is False
    assert "healthy-period data unavailable (Cloud Monitoring was not queried (no metric client))" in result["evidence"]


# ---------------------------------------------------------------------------------------
# atat fresh stack: the first-ever revision, deployed long before onset, is not a regression
# ---------------------------------------------------------------------------------------


def test_fresh_stack_first_revision_long_before_onset_is_not_a_regression(agent_cfg, telemetry, monkeypatch):
    """atat INC0010341 shape: orders-api 00001 (no previous revision) served 19:28-20:13Z, 502s from 20:16Z."""
    first_rev = "ata-orders-api-00001-qfx"
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-orders-api": {first_rev: [
        (_minute(-54), 4, 0, 120.0), (_minute(-24), 4, 0, 90.0), (_minute(-23), 1, 0, 90.0),
        (_minute(-21), 2, 0, 90.0), (_minute(-12), 6, 0, 90.0), (_minute(-11), 1, 0, 90.0),
        (_minute(-9), 2, 0, 90.0), (_minute(-6), 6, 9, 300.0), (_minute(-5), 7, 48, 300.0),
    ]}}))
    telemetry["degraded"] = {"ata-inventory-api": "5xx", "ata-orders-api": "5xx"}
    telemetry["changes"] = [_change(
        "ata-orders-api", first_rev, None, (ONSET_DT - timedelta(minutes=55)).strftime("%Y-%m-%dT%H:%M:%SZ")
    )]
    orders = _entry("0.fs-ord", "ata-orders-5xx", "ata-orders-api", "5xx")
    inventory = _entry("0.fs-inv", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    report = _investigate(_event(orders), agent_cfg, group_alerts=[orders, inventory])

    assert report.origin == "ata-inventory-api"
    alternatives = [a["alternative"] for a in report.root_cause_analysis["alternatives_ruled_out"]]
    assert not any("Deployment regression" in a for a in alternatives)
    desc = _desc(report)
    assert "Deployment regression" not in desc
    assert report.infer_issue_type() != "change_induced"
    assert f"recent changes likely unrelated: revision {first_rev} on ata-orders-api" in report.change_induced
    assert "healthy period: Cloud Monitoring" in report.change_induced
    assert "update-traffic" not in _all_action_text(report)


# ---------------------------------------------------------------------------------------
# Apigee-only deploys, and S3's SpikeArrest revision attribution
# ---------------------------------------------------------------------------------------


def test_apigee_only_deploys_are_listed_and_not_attributed(agent_cfg, telemetry, monkeypatch):
    client = _use_traffic(monkeypatch, FakeTrafficHistory())
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    deployed = (ONSET_DT - timedelta(minutes=50)).strftime("%Y-%m-%dT%H:%M:%SZ")
    telemetry["changes"] = [{
        "source": "apigee_deployment", "api_name": "ata-inventory-v1", "revision": "151",
        "environment": "prod", "timestamp": deployed,
    }]
    report = _investigate(_event(_entry("0.ap-inv", "ata-inventory-5xx", "ata-inventory-api", "5xx")), agent_cfg)

    assert report.change_induced.startswith("No (no deployment or configuration change on ata-inventory-api")
    assert report.infer_issue_type() != "change_induced"
    history = next(i for i in report.evidence_by_area["change"] if i.headline == "Apigee Deployment History")
    assert f"proxy ata-inventory-v1 revision 151 deployed {deployed} (50m before alert onset)" in history.content
    assert not any(i.headline == "Recent changes (likely unrelated)" for i in report.evidence_by_area["change"])
    assert "update-traffic" not in _all_action_text(report)
    assert client.calls == []  # no Cloud Run deploy, no healthy-period read


@pytest.mark.asyncio
async def test_s3_spike_arrest_revision_is_still_attributed_with_apigee_history(cfg):
    """S3: proxy revision 169 brought the SpikeArrest faults (access log); attributed as before,
    also when the Apigee deploy is far outside the onset window, and the history is shown."""
    timeline = {
        "changes": [{
            "source": "apigee_deployment", "api_name": "ata-orders-v1", "revision": "169",
            "environment": "prod", "timestamp": "2026-10-27T12:20:00Z",
        }],
        "apigee": {"org": "cloud-assist-fde-2", "environment": "prod", "proxy_prefix": "ata-", "deployments": []},
    }

    async def fetch(config, window_hours=2, **kwargs):
        return timeline

    with patch("agent.app.specialists.change.fetch_change_timeline", side_effect=fetch):
        report = await _orders_report(cfg, FakeGCP())

    assert report.change_induced.startswith(
        "Yes (Apigee proxy ata-orders-v1 revision 169 first served 2026-10-27T12:52:24Z, replacing revision 168;"
    )
    assert report.infer_issue_type() == "change_induced"
    history = next(i for i in report.evidence_by_area["change"] if i.headline == "Apigee Deployment History")
    assert "proxy ata-orders-v1 revision 169 deployed 2026-10-27T12:20:00Z (41m before alert onset)" in history.content



def test_configuration_md_documents_the_settings_with_their_defaults():
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    agent_section = (root / "CONFIGURATION.md").read_text().split("### 1.2", 1)[1].split("### 1.3", 1)[0]
    assert "| `CHANGE_ONSET_WINDOW_MINUTES` | `15` |" in agent_section
    assert "| `CHANGE_HEALTHY_PERIOD_MINUTES` | `10` |" in agent_section
    readme = (root / "README.md").read_text()
    assert "Recent changes (likely unrelated)" in readme and "CHANGE_HEALTHY_PERIOD_MINUTES" in readme
