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
      was healthy: HTTP 5xx share under CHANGE_HEALTHY_MAX_5XX_SHARE (0.05) and mean latency
      under CHANGE_HEALTHY_MAX_LATENCY_MS (2000) (Cloud Monitoring, per revision). These are
      agent heuristics, not the alert-policy thresholds (5xx rate > 0.05/s, p99 latency).

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
    A mean latency of None gives that minute no request_latencies point. With `latency=False`
    request_latencies returns no series; with `requests=False` request_count returns none.
    Like the real API, only points inside the request interval of the filtered service are
    returned, each with an interval end time.
    """

    def __init__(
        self, minutes: dict[str, dict[str, list[tuple]]] | None = None, error: Exception | None = None,
        latency: bool = True, requests: bool = True,
    ):
        self.minutes = minutes or {}
        self.error = error
        self.latency = latency
        self.requests = requests
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
                if not self.latency:
                    continue
                pts = [
                    SimpleNamespace(
                        interval=SimpleNamespace(end_time=m + timedelta(minutes=1)),
                        value=SimpleNamespace(distribution_value=SimpleNamespace(count=ok + bad, mean=lat)),
                    )
                    for m, ok, bad, lat in rows if ok + bad and lat is not None
                ]
                series.append(SimpleNamespace(resource=res, metric=SimpleNamespace(labels={}), points=pts))
                continue
            if not self.requests:
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
THRESHOLDS_NOTE = (
    "CHANGE_HEALTHY_MAX_5XX_SHARE 0.05, CHANGE_HEALTHY_MAX_LATENCY_MS 2000; agent heuristics, not the alert-policy thresholds"
)
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


NOT_ATTRIBUTED_HEADLINE = "Recent changes (not attributed: healthy period undetermined)"


def _assert_not_attributed(report) -> None:
    """A deploy whose healthy period is undetermined is "not attributed", never "likely unrelated"."""
    assert report.change_induced.startswith("No (not attributed: revision ")
    assert "so only the onset-window rule was applied; checked: " in report.change_induced
    assert "likely unrelated" not in report.change_induced
    desc = _desc(report)
    assert "likely unrelated" not in desc
    items = report.evidence_by_area["change"]
    assert any(i.headline == NOT_ATTRIBUTED_HEADLINE and PIN_REV in i.content for i in items)
    assert not any(i.headline == "Recent changes (likely unrelated)" for i in items)
    # Not attributed is not ruled out: no "deployment regression" alternative is listed as ruled out.
    alts = report.root_cause_analysis["alternatives_ruled_out"]
    assert not any(a["alternative"] == "Recent Application Deployment Regression" for a in alts)
    assert report.infer_issue_type() != "change_induced"
    assert "update-traffic" not in _all_action_text(report)


# ---------------------------------------------------------------------------------------
# INC0010343-shaped: deploy, 59 min healthy, then the fault => not change-induced
# ---------------------------------------------------------------------------------------


def test_inc0010343_deploy_then_healthy_period_then_fault_is_not_change_induced(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)

    expected = (
        f"revision {PIN_REV} on ata-inventory-api deployed {INC0010343_DEPLOY}: deployed 59m before the alert onset, "
        "beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); healthy period: Cloud Monitoring "
        f"shows {PIN_REV} served 41 requests from {INC0010343_DEPLOY} to {_minute(-6):%Y-%m-%dT%H:%M:%SZ} "
        "(53 one-minute intervals, at least 10; 10 of them with requests, at least 3) with HTTP 5xx under 5% of requests and mean latency under 2000 ms "
        f"({THRESHOLDS_NOTE}) in every minute with requests, "
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
    # The root cause names no revision: the unrelated deploy is not the origin change.
    assert PIN_REV not in report.probable_root_cause.text
    assert "revision deployment" not in report.probable_root_cause.text


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


@pytest.mark.parametrize("span,status", [(9, "unavailable"), (10, "healthy")])
def test_healthy_period_boundary_at_m(span, status):
    """9 healthy minutes then the fault: too short a span, but 9 healthy minutes are not "no healthy period"."""
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: (
        [(_minute(-40 + i), 5, 0, 80.0) for i in range(span)] + [(_minute(-5), 2, 9, 90.0)]
    )}})
    result = healthy_period(
        client, "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=41)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, 10,
    )
    assert result["status"] == status
    if status == "healthy":
        assert result["minutes"] == span
    else:
        assert result["reason"] == "inconclusive"
        assert f"its longest span of normal traffic was {span} one-minute intervals" in result["evidence"]


def test_unhealthy_minute_splits_the_period_and_empty_minutes_do_not():
    rows = (
        [(_minute(-50), 5, 0, 80.0), (_minute(-47), 5, 0, 80.0), (_minute(-44), 5, 0, 80.0)]  # 7-min span, gaps inside
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
    assert (strict["status"], strict["reason"], strict["active_minutes"]) == ("unavailable", "inconclusive", 4)
    assert "no healthy period" not in strict["evidence"]
    # The longest of the two runs is stated (7 min with 3 active, ending at the end of minute -44),
    # not the 1-minute run at -42 (mutant max->min would state that one).
    assert (
        f"4 of its 6 minutes with requests were healthy; its longest span of normal traffic was 7 one-minute "
        f"intervals ({_minute(-50):%Y-%m-%dT%H:%M:%SZ} to {_minute(-43):%Y-%m-%dT%H:%M:%SZ}) with requests in 3 of them"
    ) in strict["evidence"]
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


def test_healthy_threshold_config_defaults_and_validation(monkeypatch):
    monkeypatch.delenv("CHANGE_HEALTHY_MAX_5XX_SHARE", raising=False)
    monkeypatch.delenv("CHANGE_HEALTHY_MAX_LATENCY_MS", raising=False)
    default = _config()
    assert (default.change_healthy_max_5xx_share, default.change_healthy_max_latency_ms) == (0.05, 2000.0)
    monkeypatch.setenv("CHANGE_HEALTHY_MAX_5XX_SHARE", "0.01")
    monkeypatch.setenv("CHANGE_HEALTHY_MAX_LATENCY_MS", "500")
    assert (_config().change_healthy_max_5xx_share, _config().change_healthy_max_latency_ms) == (0.01, 500.0)
    for name, bads, good in (
        ("CHANGE_HEALTHY_MAX_5XX_SHARE", ("0", "-0.1", "1.5", "5%", "nan"), "0.05"),
        ("CHANGE_HEALTHY_MAX_LATENCY_MS", ("0", "-1", "300001", "abc", "nan"), "2000"),
    ):
        for bad in bads:
            monkeypatch.setenv(name, bad)
            with pytest.raises(ValidationError):
                _config()
        monkeypatch.setenv(name, good)


def test_configured_healthy_thresholds_are_applied_and_stated():
    """A 3 % 5xx minute is healthy at the default 0.05 and unhealthy at 0.02; the text states the values used."""
    rows = [(_minute(-30 + i), 97, 3, 900.0) for i in range(20)]
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}})
    default = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), client)
    assert default["causal"] is False
    assert f"({THRESHOLDS_NOTE})" in default["evidence"]
    strict = assess_deploy_causation(_deploy(31), ONSET_DT, _config(change_healthy_max_5xx_share=0.02), client)
    assert strict["causal"] is True and strict["rule"] == "b"
    assert "HTTP 5xx under 2% of requests" in strict["evidence"] and "CHANGE_HEALTHY_MAX_5XX_SHARE 0.02" in strict["evidence"]
    slow = assess_deploy_causation(_deploy(31), ONSET_DT, _config(change_healthy_max_latency_ms=800), client)
    assert slow["causal"] is True
    assert "mean latency 900 ms" in slow["evidence"] and "CHANGE_HEALTHY_MAX_LATENCY_MS 800" in slow["evidence"]


# ---------------------------------------------------------------------------------------
# Healthy-minute checks: latency breach, the exact 5xx boundary, this revision only
# ---------------------------------------------------------------------------------------


def _hp(client, deploy_mins: int = 41, min_minutes: int = 10):
    return healthy_period(
        client, "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=deploy_mins)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, min_minutes,
    )


def test_latency_over_the_threshold_alone_makes_a_minute_unhealthy():
    """Error-free traffic with a mean latency at 2000 ms every minute is no healthy period."""
    rows = [(_minute(-40 + i), 10, 0, 2000.0) for i in range(30)]
    result = _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
    assert result["status"] == "no_healthy_period"
    assert f"first minute over the thresholds {_minute(-40):%Y-%m-%dT%H:%M:%SZ} (mean latency 2000 ms)" in result["evidence"]
    just_under = _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(m, ok, bad, 1999.0) for m, ok, bad, _ in rows]}}))
    assert just_under["status"] == "healthy"


@pytest.mark.parametrize("errors,status", [(1, "no_healthy_period"), (0, "healthy")])
def test_a_5xx_share_of_exactly_the_threshold_is_unhealthy(errors, status):
    """1 in 20 is exactly 5 %: at the threshold is unhealthy, matching the measured-breach semantics."""
    rows = [(_minute(-40 + i), 20 - errors, errors, 80.0) for i in range(30)]
    assert _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))["status"] == status


def test_only_the_new_revisions_traffic_counts():
    """The previous revision served healthy traffic all along; the new one failed from its first request."""
    client = FakeTrafficHistory({"ata-inventory-api": {
        PIN_PREV: [(_minute(-40 + i), 400, 0, 80.0) for i in range(35)],
        PIN_REV: [(_minute(-40 + i), 1, 4, 300.0) for i in range(35)],
    }})
    # Mixed with the previous revision the service is healthy (4 of 405 requests are 5xx, 1 %).
    result = _hp(client)
    assert result["status"] == "no_healthy_period"
    assert "it had no minute of normal traffic" in result["evidence"]
    assert "HTTP 5xx 80.0% of 5 requests" in result["evidence"]
    assert assess_deploy_causation(_deploy(41), ONSET_DT, _config(), client)["rule"] == "b"
    # The same for latency: the new revision is slow (3000 ms) and error-free, the previous one fast.
    slow = FakeTrafficHistory({"ata-inventory-api": {
        PIN_PREV: [(_minute(-40 + i), 400, 0, 80.0) for i in range(35)],
        PIN_REV: [(_minute(-40 + i), 5, 0, 3000.0) for i in range(35)],
    }})
    slow_result = _hp(slow)
    assert slow_result["status"] == "no_healthy_period"
    assert "(mean latency 3000 ms)" in slow_result["evidence"]


def test_root_cause_names_the_causal_deploy_not_an_earlier_unrelated_one(agent_cfg, telemetry, monkeypatch):
    """An earlier healthy re-pin (40 min, listed first) and a bad revision 6 min before onset:
    the root cause and rollback name the bad revision, never the unrelated re-pin."""
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-inventory-api": {
        PIN_PREV: [(_minute(-50 + i), 8, 0, 90.0) for i in range(8)],
        PIN_REV: [(_minute(-40 + i), 8, 0, 90.0) for i in range(33)],
        INVENTORY_BAD_REV: [(_minute(-6 + i), 1, 7, 150.0) for i in range(6)],
    }}))
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    telemetry["changes"] = [
        _change("ata-inventory-api", PIN_REV, PIN_PREV, (ONSET_DT - timedelta(minutes=40)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        _change("ata-inventory-api", INVENTORY_BAD_REV, PIN_REV, (ONSET_DT - timedelta(minutes=6, seconds=30)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ]
    report = _investigate(_event(_entry("0.rc-inv", "ata-inventory-5xx", "ata-inventory-api", "5xx")), agent_cfg)

    assert report.infer_issue_type() == "change_induced"
    assert f"`{INVENTORY_BAD_REV}`" in report.probable_root_cause.text
    assert PIN_REV not in report.probable_root_cause.text
    assert any(i.headline == "Recent changes (likely unrelated)" and PIN_REV in i.content for i in report.evidence_by_area["change"])


# ---------------------------------------------------------------------------------------
# Partial data: latency or request_count missing must not imply "no healthy period"
# ---------------------------------------------------------------------------------------


def test_inc0010343_with_an_empty_latency_series_falls_back_to_the_onset_window(agent_cfg, telemetry, monkeypatch):
    """R1 regression: no request_latencies data is incomplete evidence, not "no minute of normal traffic"."""
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC, latency=False))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)

    assert report.change_induced.startswith("No (not attributed: ")
    _assert_not_attributed(report)
    assert (
        "deployed 59m before the alert onset, beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); "
        f"healthy-period data unavailable (Cloud Monitoring run.googleapis.com/request_latencies returned no data for {PIN_REV} "
        f"between {INC0010343_DEPLOY} (deploy) and {ONSET} (alert onset), although run.googleapis.com/request_count recorded "
        "340 requests on it), so only the onset-window rule was applied"
    ) in report.change_induced
    assert "no minute of normal traffic" not in report.change_induced
    assert "no healthy period" not in report.change_induced
    assert report.infer_issue_type() != "change_induced"
    assert "update-traffic" not in _all_action_text(report)
    assert "Deployment regression" not in _desc(report)


def test_request_count_without_latency_beyond_the_window_is_not_causal():
    """Even a failing-looking revision is not blamed by rule (b) when its latency read is empty."""
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-30 + i), 1, 7, 150.0) for i in range(20)]}}, latency=False)
    result = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), client)
    assert result["causal"] is False and result["rule"] is None
    assert result["healthy_period"]["status"] == "unavailable"
    assert "so only the onset-window rule was applied" in result["evidence"]


def test_latency_without_request_count_is_unavailable():
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-30 + i), 5, 0, 80.0) for i in range(20)]}}, requests=False)
    result = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), client)
    assert result["causal"] is False
    assert result["healthy_period"]["status"] == "unavailable"
    assert "run.googleapis.com/request_count recorded no requests on" in result["evidence"]


def test_minutes_missing_a_latency_point_are_judged_on_5xx_alone_and_counted():
    """Late or missing latency points must not shrink a healthy span into a "causal" verdict."""
    rows = [(_minute(-40 + i), 5, 0, 80.0 if i % 3 == 0 else None) for i in range(30)]
    result = _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
    assert result["status"] == "healthy" and result["minutes"] == 30
    assert "run.googleapis.com/request_latencies had no point for 20 of 30 minutes with requests, judged on the 5xx share alone" in result["evidence"]
    # A 5xx breach in a minute without latency still breaks the span.
    failing = [(m, 1, 4, None) for m, *_ in rows]
    failing[0] = (failing[0][0], 5, 0, 80.0)
    bad = _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: failing}}))
    assert (bad["status"], bad["reason"]) == ("unavailable", "insufficient_traffic")
    assert "1 of its 30 minutes with requests were healthy" in bad["evidence"]
    assert "had no point for 29 of 30 minutes" in bad["evidence"]


def test_unknown_deploy_time_is_stated_not_negative():
    chg = _deploy(40)
    del chg["deployed_minutes_ago"]
    result = assess_deploy_causation(chg, ONSET_DT, _config(), HEALTHY_SINCE_DEPLOY)
    assert "-1m" not in result["evidence"]
    assert result["evidence"].startswith(
        "deploy time relative to the alert onset unknown, so the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES) does not apply"
    )


# ---------------------------------------------------------------------------------------
# Missing healthy-period data: rule (a) only, and the ticket says so
# ---------------------------------------------------------------------------------------


def test_monitoring_failure_falls_back_to_onset_window_and_says_so(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(error=RuntimeError("503 backend unavailable")))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)

    assert report.change_induced.startswith("No (not attributed: ")
    _assert_not_attributed(report)
    assert (
        "deployed 59m before the alert onset, beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); "
        f"healthy-period data unavailable (the Cloud Monitoring query for {PIN_REV} between {INC0010343_DEPLOY} (deploy) "
        f"and {ONSET} (alert onset) failed (RuntimeError)), so only the onset-window rule was applied"
    ) in report.change_induced
    assert "Change-Induced: No (not attributed:" in _desc(report)
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
    assert "| `CHANGE_WINDOW_MINUTES` | `120` |" in agent_section
    assert "| `CHANGE_ONSET_WINDOW_MINUTES` | `15` |" in agent_section
    assert "| `CHANGE_HEALTHY_PERIOD_MINUTES` | `10` |" in agent_section
    assert "| `CHANGE_HEALTHY_MIN_ACTIVE_MINUTES` | `3` |" in agent_section
    assert "| `CHANGE_HEALTHY_MAX_5XX_SHARE` | `0.05` |" in agent_section
    assert "| `CHANGE_HEALTHY_MAX_LATENCY_MS` | `2000` |" in agent_section
    assert "These are the alert-policy thresholds" not in agent_section
    readme = (root / "README.md").read_text()
    assert "Recent changes (likely unrelated)" in readme and "CHANGE_HEALTHY_PERIOD_MINUTES" in readme
    assert "CHANGE_HEALTHY_MIN_ACTIVE_MINUTES" in readme
    assert "CHANGE_WINDOW_MINUTES" in (root / "agent" / "scripts" / "deploy.sh").read_text()
    assert "CHANGE_WINDOW_MINUTES" in (root / "DEPLOY.md").read_text()
    assert "CHANGE_WINDOW_MINUTES" in (root / "DEMO.md").read_text()


# ---------------------------------------------------------------------------------------
# Traffic floor (CHANGE_HEALTHY_MIN_ACTIVE_MINUTES): sparse traffic is insufficient evidence
# ---------------------------------------------------------------------------------------


def test_two_requests_40_minutes_apart_are_insufficient_traffic_so_rule_a_only():
    """A 41-minute span with requests in 2 minutes is not "a sustained period of normal traffic"."""
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-44), 1, 0, 80.0), (_minute(-4), 1, 0, 80.0)]}})
    result = assess_deploy_causation(_deploy(45), ONSET_DT, _config(), client)
    assert result["causal"] is False and result["rule"] is None
    hp = result["healthy_period"]
    assert (hp["status"], hp["reason"], hp["active_minutes"]) == ("unavailable", "insufficient_traffic", 2)
    assert (
        "deployed 45m before the alert onset, beyond the 15-minute change onset window (CHANGE_ONSET_WINDOW_MINUTES); "
        "insufficient traffic to establish a healthy period (2 healthy active minutes < 3, CHANGE_HEALTHY_MIN_ACTIVE_MINUTES 3): "
        f"Cloud Monitoring shows {PIN_REV} between {_deploy(45)['timestamp']} (deploy) and {ONSET} (alert onset): "
        "2 of its 2 minutes with requests were healthy; its longest span of normal traffic was 41 one-minute intervals "
        f"({_minute(-44):%Y-%m-%dT%H:%M:%SZ} to {_minute(-3):%Y-%m-%dT%H:%M:%SZ}) with requests in 2 of them"
    ) in result["evidence"]
    assert result["evidence"].endswith(", so only the onset-window rule was applied")
    assert "no healthy period" not in result["evidence"]


def test_few_healthy_minutes_in_total_are_insufficient_not_no_healthy_period():
    """Two error-free minutes 5 min apart: too short a span, but too little traffic to blame the deploy."""
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-30), 1, 0, 80.0), (_minute(-25), 2, 0, 80.0)]}})
    result = assess_deploy_causation(_deploy(40), ONSET_DT, _config(), client)
    assert result["causal"] is False
    assert result["healthy_period"]["reason"] == "insufficient_traffic"
    assert "insufficient traffic to establish a healthy period (2 healthy active minutes < 3" in result["evidence"]
    assert "2 of its 2 minutes with requests were healthy; its longest span of normal traffic was 6 one-minute" in result["evidence"]
    assert "no healthy period" not in result["evidence"]


@pytest.mark.parametrize("active,status", [(2, "unavailable"), (3, "healthy")])
def test_traffic_floor_boundary(active, status):
    rows = [(_minute(-40), 5, 0, 80.0), (_minute(-30), 5, 0, 80.0)] + ([(_minute(-28), 5, 0, 80.0)] if active == 3 else [])
    result = _hp(FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
    assert result["status"] == status
    if status == "healthy":
        assert (result["minutes"], result["active_minutes"]) == (13, 3)
        assert "(13 one-minute intervals, at least 10; 3 of them with requests, at least 3)" in result["evidence"]


def test_traffic_floor_follows_config(monkeypatch):
    monkeypatch.delenv("CHANGE_HEALTHY_MIN_ACTIVE_MINUTES", raising=False)
    assert _config().change_healthy_min_active_minutes == 3
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-40 + 5 * i), 5, 0, 80.0) for i in range(4)]}})
    assert assess_deploy_causation(_deploy(41), ONSET_DT, _config(), client)["healthy_period"]["status"] == "healthy"
    strict = assess_deploy_causation(_deploy(41), ONSET_DT, _config(change_healthy_min_active_minutes=5), client)
    assert strict["causal"] is False
    assert "(4 healthy active minutes < 5, CHANGE_HEALTHY_MIN_ACTIVE_MINUTES 5)" in strict["evidence"]
    monkeypatch.setenv("CHANGE_HEALTHY_MIN_ACTIVE_MINUTES", "7")
    assert _config().change_healthy_min_active_minutes == 7
    for bad in ("0", "121", "abc", "2.5"):
        monkeypatch.setenv("CHANGE_HEALTHY_MIN_ACTIVE_MINUTES", bad)
        with pytest.raises(ValidationError):
            _config()


def test_sparse_failing_revision_with_enough_minutes_is_still_no_healthy_period():
    """The floor does not hide failures: S5 at low traffic (failing from its first request) stays causal."""
    client = FakeTrafficHistory({"ata-inventory-api": {PIN_REV: [(_minute(-24 + 6 * i), 1, 3, 150.0) for i in range(4)]}})
    result = assess_deploy_causation(_deploy(25), ONSET_DT, _config(), client)
    assert result["causal"] is True and result["rule"] == "b"
    assert "it had no minute of normal traffic" in result["evidence"]


def test_inc0010343_shape_meets_the_traffic_floor():
    result = _hp(FakeTrafficHistory(INC0010343_TRAFFIC), deploy_mins=60)
    assert (result["status"], result["minutes"], result["active_minutes"]) == ("healthy", 53, 10)


def test_sparse_healthy_span_then_fault_in_a_ticket_is_not_change_induced(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-inventory-api": {
        PIN_REV: [(_minute(-50), 1, 0, 80.0), (_minute(-10), 1, 0, 80.0), (_minute(-6), 16, 58, 300.0), (_minute(-5), 16, 98, 250.0)],
    }}))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    assert report.change_induced.startswith("No (not attributed: ")
    _assert_not_attributed(report)
    assert "insufficient traffic to establish a healthy period (2 healthy active minutes < 3" in report.change_induced
    assert "so only the onset-window rule was applied" in report.change_induced
    assert report.infer_issue_type() != "change_induced"
    assert "update-traffic" not in _all_action_text(report)
    assert "Deployment regression" not in _desc(report)


INCONCLUSIVE_ROWS = (
    [(_minute(-50), 5, 0, 80.0), (_minute(-48), 5, 0, 80.0), (_minute(-46), 5, 0, 80.0)]  # 5-min run, 3 active
    + [(_minute(-44), 1, 5, 80.0)]  # 5xx 83 %
    + [(_minute(-42), 5, 0, 80.0), (_minute(-40), 5, 0, 80.0)]  # 3-min run, 2 active
    + [(_minute(-6), 16, 58, 300.0), (_minute(-5), 16, 98, 250.0)]  # the fault
)


def test_inconclusive_healthy_period_in_a_ticket_is_not_attributed(agent_cfg, telemetry, monkeypatch):
    """O2: 5 healthy minutes but no 10-minute span: undetermined, so "not attributed", not "likely unrelated"."""
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-inventory-api": {PIN_REV: INCONCLUSIVE_ROWS}}))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    _assert_not_attributed(report)
    assert "healthy period inconclusive (5 healthy active minutes, but no span of at least 10" in report.change_induced
    assert (
        f"its longest span of normal traffic was 5 one-minute intervals ({_minute(-50):%Y-%m-%dT%H:%M:%SZ} to "
        f"{_minute(-45):%Y-%m-%dT%H:%M:%SZ}) with requests in 3 of them"
    ) in report.change_induced
    assert f"Change-Induced: No (not attributed: revision {PIN_REV} on ata-inventory-api" in _desc(report)


def test_healthy_and_undetermined_deploys_are_listed_under_separate_headings(agent_cfg, telemetry, monkeypatch):
    """O2: only the deploy with an established healthy period is "likely unrelated"."""
    _use_traffic(monkeypatch, FakeTrafficHistory({"ata-inventory-api": {
        PIN_PREV: [(_minute(-90 + i), 8, 0, 90.0) for i in range(30)],
        INVENTORY_BAD_REV: INCONCLUSIVE_ROWS,
    }}))
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    telemetry["changes"] = [
        _change("ata-inventory-api", PIN_PREV, "ata-inventory-api-00072-xxx", (ONSET_DT - timedelta(minutes=90)).strftime("%Y-%m-%dT%H:%M:%SZ")),
        _change("ata-inventory-api", INVENTORY_BAD_REV, PIN_PREV, (ONSET_DT - timedelta(minutes=51)).strftime("%Y-%m-%dT%H:%M:%SZ")),
    ]
    report = _investigate(_event(_entry("0.cr-inv", "ata-inventory-5xx", "ata-inventory-api", "5xx")), agent_cfg)

    ci = report.change_induced
    assert ci.startswith(f"No (recent changes likely unrelated: revision {PIN_PREV} on ata-inventory-api ")
    assert f"; not attributed: revision {INVENTORY_BAD_REV} on ata-inventory-api " in ci
    assert ci.index(INVENTORY_BAD_REV) > ci.index("; not attributed: ")
    assert "so only the onset-window rule was applied; checked: " in ci
    items = {i.headline: i.content for i in report.evidence_by_area["change"]}
    assert PIN_PREV in items["Recent changes (likely unrelated)"]
    assert INVENTORY_BAD_REV not in items["Recent changes (likely unrelated)"]
    assert INVENTORY_BAD_REV in items[NOT_ATTRIBUTED_HEADLINE]
    assert PIN_PREV not in items[NOT_ATTRIBUTED_HEADLINE]
    alts = report.root_cause_analysis["alternatives_ruled_out"]
    assert not any(a["alternative"] == "Recent Application Deployment Regression" for a in alts)
    assert report.infer_issue_type() != "change_induced"
    assert "update-traffic" not in _all_action_text(report)


# ---------------------------------------------------------------------------------------
# Bounded Monitoring reads: per-revision filter, explicit timeout, off the event loop
# ---------------------------------------------------------------------------------------


class RecordingTraffic(FakeTrafficHistory):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.kwargs: list[dict[str, Any]] = []
        self.threads: list[str] = []

    def list_time_series(self, request: Any = None, **kwargs: Any) -> list[Any]:
        import threading

        self.kwargs.append(kwargs)
        self.threads.append(threading.current_thread().name)
        return super().list_time_series(request=request, **kwargs)


def test_healthy_period_reads_are_filtered_to_the_revision_and_have_a_timeout():
    from agent.app.specialists.change import MONITORING_READ_TIMEOUT_SECONDS

    client = RecordingTraffic(INC0010343_TRAFFIC)
    _hp(client, deploy_mins=60)
    assert len(client.calls) == 2
    assert {("request_latencies" in r.filter) for r in client.calls} == {True, False}
    for request in client.calls:
        assert f'resource.labels.revision_name = "{PIN_REV}"' in request.filter
        assert 'resource.labels.service_name = "ata-inventory-api"' in request.filter
    assert client.kwargs == [{"timeout": MONITORING_READ_TIMEOUT_SECONDS}] * 2
    assert MONITORING_READ_TIMEOUT_SECONDS == 30.0


def test_request_count_helper_without_revision_or_timeout_is_unchanged():
    """The rollback-target lookup and recovery note still read every revision, as before."""
    from agent.app.request_count import request_count_filter, request_count_series

    assert "revision_name" not in request_count_filter("us-west1", "svc")
    client = RecordingTraffic({})
    request_count_series(client, "p", "us-west1", "svc", 0, 60)
    assert client.kwargs == [{}] and "revision_name" not in client.calls[0].filter


def test_monitoring_timeout_falls_back_to_onset_window_and_says_so():
    from google.api_core.exceptions import DeadlineExceeded

    client = FakeTrafficHistory(error=DeadlineExceeded("deadline 30s exceeded"))
    result = assess_deploy_causation(_deploy(40), ONSET_DT, _config(), client)
    assert result["causal"] is False and result["healthy_period"]["status"] == "unavailable"
    assert "failed (DeadlineExceeded)), so only the onset-window rule was applied" in result["evidence"]
    # Inside the onset window a timeout changes nothing: rule (a) needs no read.
    assert assess_deploy_causation(_deploy(5), ONSET_DT, _config(), client)["causal"] is True


def test_healthy_period_reads_run_off_the_event_loop_thread(agent_cfg, telemetry, monkeypatch):
    import threading

    client = _use_traffic(monkeypatch, RecordingTraffic(INC0010343_TRAFFIC))
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    assert report.change_induced.startswith("No (recent changes likely unrelated: ")
    latency_reads = [t for t, r in zip(client.threads, client.calls) if "request_latencies" in r.filter]
    assert latency_reads and threading.main_thread().name not in latency_reads


# ---------------------------------------------------------------------------------------
# The floor counts HEALTHY active minutes (reviewer probes at c18b7b6)
#
#   healthy minutes  qualifying span  verdict                     causal beyond window
#   0 (>= 1 failing) -                no_healthy_period            yes, rule (b) (S5)
#   1 .. floor-1     no               unavailable/insufficient     no, rule (a) only
#   >= floor         no               unavailable/inconclusive     no, rule (a) only
#   any              yes              healthy                      no
#
# A qualifying span: >= CHANGE_HEALTHY_PERIOD_MINUTES long, >= CHANGE_HEALTHY_MIN_ACTIVE_MINUTES
# healthy minutes with requests, no failing minute inside. Failing minutes (the fault's lead-in
# included) never count toward health.
# ---------------------------------------------------------------------------------------

LEAD_IN = [(_minute(-2), 2, 9, 300.0), (_minute(-1), 3, 12, 300.0)]


def test_probe1_idle_then_fails_from_its_first_request_is_causal():
    """Idle for 25 min after the deploy, then 1-2 failing minutes: S5 at zero traffic, rule (b)."""
    for rows in (LEAD_IN[:1], LEAD_IN):
        result = assess_deploy_causation(_deploy(30), ONSET_DT, _config(), FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
        assert (result["causal"], result["rule"]) == (True, "b")
        hp = result["healthy_period"]
        assert (hp["status"], hp["active_minutes"], hp["unhealthy_minutes"]) == ("no_healthy_period", 0, len(rows))
        assert f"it had no minute of normal traffic (all {len(rows)} minutes with requests were over a threshold)" in result["evidence"]
        assert "insufficient traffic" not in result["evidence"]


def test_probe2_lead_in_does_not_turn_two_healthy_minutes_into_rule_b():
    """2 healthy minutes, with and without the fault's lead-in: the same verdict, rule (a) only."""
    healthy = [(_minute(-30), 5, 0, 80.0), (_minute(-28), 5, 0, 80.0)]
    verdicts = []
    for rows in (healthy, healthy + LEAD_IN):
        result = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
        assert (result["causal"], result["rule"]) == (False, None)
        assert result["healthy_period"]["reason"] == "insufficient_traffic"
        assert "(2 healthy active minutes < 3, CHANGE_HEALTHY_MIN_ACTIVE_MINUTES 3)" in result["evidence"]
        assert "no healthy period" not in result["evidence"]
        verdicts.append((result["causal"], result["healthy_period"]["status"], result["healthy_period"]["reason"]))
    assert verdicts[0] == verdicts[1]


@pytest.mark.parametrize("healthy", [1, 2, 3, 4, 9])
def test_probe3_more_healthy_minutes_then_idle_then_fault_is_never_blamed(healthy):
    """k consecutive healthy minutes (span < 10), idle, then the fault: 3 is not blamed where 2 is not."""
    rows = [(_minute(-40 + i), 5, 0, 80.0) for i in range(healthy)] + LEAD_IN
    result = assess_deploy_causation(_deploy(41), ONSET_DT, _config(), FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
    assert (result["causal"], result["rule"]) == (False, None)
    expected = "insufficient_traffic" if healthy < 3 else "inconclusive"
    assert result["healthy_period"]["reason"] == expected
    assert result["evidence"].endswith(", so only the onset-window rule was applied")
    # Undetermined, not missing data: the verdict is stated as such, not as "data unavailable".
    assert "healthy-period data unavailable" not in result["evidence"]
    if expected == "inconclusive":
        assert f"healthy period inconclusive ({healthy} healthy active minutes, but no span of at least 10" in result["evidence"]


def test_inc0010343_is_healthy_so_not_causal():
    result = assess_deploy_causation(_deploy(59), ONSET_DT, _config(), FakeTrafficHistory(INC0010343_TRAFFIC))
    assert (result["causal"], result["rule"]) == (False, None)
    assert (result["healthy_period"]["status"], result["healthy_period"]["active_minutes"]) == ("healthy", 10)


def test_s5_failing_from_its_first_request_is_causal():
    rows = [(_minute(-30 + i), 1, 9, 400.0) for i in range(30)]
    result = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}))
    assert (result["causal"], result["rule"]) == (True, "b")
    assert "all 30 minutes with requests were over a threshold" in result["evidence"]


def test_missing_data_is_never_blamed():
    failing = {"ata-inventory-api": {PIN_REV: [(_minute(-30 + i), 1, 9, 400.0) for i in range(30)]}}
    for client in (
        FakeTrafficHistory(failing, latency=False),
        FakeTrafficHistory(failing, requests=False),
        FakeTrafficHistory(error=TimeoutError("deadline exceeded")),
        None,
    ):
        result = assess_deploy_causation(_deploy(31), ONSET_DT, _config(), client)
        assert (result["causal"], result["healthy_period"]["status"]) == (False, "unavailable")
        assert "healthy-period data unavailable (" in result["evidence"]


def _blamed(cells: tuple[str, ...], min_minutes: int, floor: int) -> bool:
    """Rule (b) verdict for a minute sequence: '.' idle, 'h' healthy, 'f' failing (ending at the onset)."""
    n = len(cells)
    rows = [
        (_minute(-n + i), 5 if c == "h" else 1, 0 if c == "h" else 9, 80.0)
        for i, c in enumerate(cells) if c != "."
    ]
    result = healthy_period(
        FakeTrafficHistory({"ata-inventory-api": {PIN_REV: rows}}), "p", "us-west1", "ata-inventory-api", PIN_REV,
        (ONSET_DT - timedelta(minutes=n)).strftime("%Y-%m-%dT%H:%M:%SZ"), ONSET_DT, min_minutes,
        min_active_minutes=floor,
    )
    return result["status"] == "no_healthy_period"


def test_adding_a_healthy_minute_never_makes_blame_more_likely():
    """Exhaustive over every 7-minute sequence of idle/healthy/failing minutes (period 4, floor 2):
    turning any idle or failing minute healthy never turns "not blamed" into "blamed"."""
    from itertools import product

    n, period, floor = 7, 4, 2
    verdict = {cells: _blamed(cells, period, floor) for cells in product(".hf", repeat=n)}
    assert any(verdict.values()) and not all(verdict.values())
    violations = [
        (cells, i)
        for cells, blamed in verdict.items() if not blamed
        for i, c in enumerate(cells) if c != "h" and verdict[cells[:i] + ("h",) + cells[i + 1:]]
    ]
    assert violations == []
    # Blamed exactly when there is no healthy minute and at least one failing one.
    assert all(blamed == ("h" not in cells and "f" in cells) for cells, blamed in verdict.items())


# ---------------------------------------------------------------------------------------
# The change window (CHANGE_WINDOW_MINUTES, default 120): which deploys the rule evaluates
# ---------------------------------------------------------------------------------------


def test_change_window_config_default_and_validation(monkeypatch):
    monkeypatch.delenv("CHANGE_WINDOW_MINUTES", raising=False)
    assert _config().change_window_minutes == 120
    monkeypatch.setenv("CHANGE_WINDOW_MINUTES", "90")
    assert _config().change_window_minutes == 90
    for bad in ("0", "1441", "abc", "1.5"):
        monkeypatch.setenv("CHANGE_WINDOW_MINUTES", bad)
        with pytest.raises(ValidationError):
            _config()


def test_is_genuine_change_applies_the_window_argument_then_the_record_then_120():
    from agent.app.specialists.change import change_window_hours, change_window_label, is_genuine_change

    chg = {"deployed_minutes_ago": 100}
    assert is_genuine_change(chg)  # default 120
    assert not is_genuine_change(chg, 90)
    assert not is_genuine_change({**chg, "change_window_minutes": 90})
    assert is_genuine_change({**chg, "change_window_minutes": 90}, 100)
    assert not is_genuine_change({"deployed_minutes_ago": 121})
    assert (change_window_hours(120), change_window_hours(90), change_window_hours(30)) == (2, 2, 1)
    assert (change_window_label(120), change_window_label(90)) == ("2-hour", "90-minute")


def _record_window_hours(monkeypatch) -> list[int]:
    from agent.app.specialists import change as change_module

    seen: list[int] = []
    inner = change_module.fetch_change_timeline

    async def fetch(config, window_hours=2, **kwargs):
        seen.append(window_hours)
        return await inner(config, window_hours=window_hours, **kwargs)

    monkeypatch.setattr(change_module, "fetch_change_timeline", fetch)
    return seen


def test_deploy_beyond_a_shorter_change_window_is_not_evaluated(agent_cfg, telemetry, monkeypatch):
    """CHANGE_WINDOW_MINUTES 30: the INC0010343 re-pin (59 min before onset) is outside the window."""
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC))
    seen = _record_window_hours(monkeypatch)
    cfg30 = agent_cfg.model_copy(update={"change_window_minutes": 30})
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), cfg30)

    assert seen == [1]
    assert report.change_induced.startswith(
        "No (no deployment or configuration change on ata-inventory-api or its upstream dependencies in the "
        "30 min before the alert; checked: "
    )
    assert PIN_REV not in report.change_induced
    findings = [f for d in report.evidence_by_area.values() for i in d for f in [i.content or ""]]
    assert any("No recent Cloud Run deployments detected" in f for f in findings)
    assert not any(i.headline.startswith("Recent changes") for i in report.evidence_by_area["change"])
    assert "2 h before the alert" not in _desc(report)


def test_default_change_window_evaluates_the_same_deploy(agent_cfg, telemetry, monkeypatch):
    _use_traffic(monkeypatch, FakeTrafficHistory(INC0010343_TRAFFIC))
    seen = _record_window_hours(monkeypatch)
    report = _investigate(_inventory_5xx(telemetry, INC0010343_DEPLOY), agent_cfg)
    assert seen == [2]
    assert report.change_induced.startswith(f"No (recent changes likely unrelated: revision {PIN_REV} ")


def test_no_change_finding_and_apigee_history_state_the_window():
    from agent.app.specialists.change import (
        NO_CLOUD_RUN_CHANGES_FINDING,
        apigee_history_text,
        no_cloud_run_changes_finding,
        timeline_apigee_deployments,
    )

    assert NO_CLOUD_RUN_CHANGES_FINDING == no_cloud_run_changes_finding(120) == (
        "No Cloud Run revision deployments detected in the 2-hour window prior to alert onset."
    )
    assert "in the 90-minute window" in no_cloud_run_changes_finding(90)
    timeline = {"changes": [{
        "source": "apigee_deployment", "api_name": "ata-orders-v1", "revision": "169",
        "environment": "prod", "timestamp": (ONSET_DT - timedelta(minutes=100)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }], "apigee": {"org": "cloud-assist-fde-2", "environment": "prod", "proxy_prefix": "ata-", "deployments": []}}
    assert len(timeline_apigee_deployments(timeline, ONSET_DT)["in_window"]) == 1
    history = timeline_apigee_deployments(timeline, ONSET_DT, 90)
    assert history["in_window"] == []
    assert "no proxy deployments in the 90-minute window prior to alert onset" in apigee_history_text(history)
