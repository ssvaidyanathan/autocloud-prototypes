"""Origin review follow-ups (origin-review.md on bb481e0/0b64daa).

R1 (blocker): an UNALERTED upstream whose Cloud Run dossier is anomalous only because of
log-derived signals (has_db_auth_error) must not explain away a genuine downstream bad deploy.
An unalerted upstream explains only with a MEASURED 5xx/latency breach.

Onset ordering: an upstream service whose onset is later than the downstream onset plus
ORIGIN_ONSET_TOLERANCE_SECONDS does not explain the downstream service.

Nits: the AlertEvent-from-entry helper is shared, resource_type is not inherited from another
service's alert, a single-service change verdict is labelled "Change assessment", and
_assess_change runs once per investigation.
"""

import asyncio
import logging

import pytest

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.grouping.models import OKFDocument
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import CloudRunSpecialist, measured_breaches
from agent.app.specialists.origin_selection import (
    alert_event_from_entry,
    onset_allows_explanation,
    select_origin,
)
from agent.app.specialists.synthesizer import SynthesizerAgent
from topology.topology_graph import get_loader

from tests.test_group_origin_selection import (  # noqa: F401  (fixtures)
    ONSET,
    ORDERS_PREV_REV,
    S2_INVENTORY_1,
    S2_ORDERS,
    _all_action_text,
    _change,
    _entry,
    _event,
    _ticket_report_for_group,
    agent_cfg,
    clock,
    telemetry,
)

ORDERS_BAD_REV = "ata-orders-api-00009-bad"
# Deployed 3 minutes before the orders alert onset (06:01:52Z).
ORDERS_BAD_DEPLOY = _change("ata-orders-api", ORDERS_BAD_REV, ORDERS_PREV_REV, "2026-10-27T05:58:52Z")


def _select(alerts, tolerance=None):
    topo = get_loader()
    return select_origin(
        alerts, canonicalize=topo.canonicalize, upstream_of=topo.upstream, onset_tolerance_seconds=tolerance
    )


def _with_db_auth_only_upstream(monkeypatch, upstream: str):
    """Wrap the stubbed CloudRunSpecialist: ``upstream`` is anomalous ONLY via has_db_auth_error."""
    stubbed = CloudRunSpecialist.investigate

    async def investigate(self, alert, config, target_service=None):
        target = target_service or alert.resource_name
        if target == upstream:
            return EvidenceDossier(
                specialist_name="CloudRunSpecialist",
                target_resource=target,
                findings=[f"Application error logs on `{target}` indicate database authentication failure."],
                metrics={
                    "has_db_auth_error": True,
                    "observed_symptom": "database authentication errors from application logs",
                    "error_rate_5xx": "N/A (not captured)",
                    "latency_p95_ms": "N/A (not captured)",
                },
                anomaly_detected=True,
            )
        return await stubbed(self, alert, config, target_service=target_service)

    monkeypatch.setattr(CloudRunSpecialist, "investigate", investigate)


# ---------------------------------------------------------------------------
# R1: unalerted upstream with log-only anomaly does not neutralize a downstream deploy
# ---------------------------------------------------------------------------

def test_unalerted_db_auth_only_upstream_does_not_neutralize_downstream_bad_deploy(
    agent_cfg, clock, monkeypatch, telemetry
):
    orders = _entry("0.r1-orders", "ata-orders-5xx", "ata-orders-api", "5xx")
    telemetry["degraded"] = {"ata-orders-api": "5xx"}
    telemetry["changes"] = [ORDERS_BAD_DEPLOY]
    _with_db_auth_only_upstream(monkeypatch, "ata-inventory-api")

    report, _, _ = _ticket_report_for_group([orders], agent_cfg, clock, monkeypatch)
    first = report.get_normalized_actions()[0]

    assert report.origin == "ata-orders-api"
    assert ORDERS_BAD_REV in report.probable_root_cause.text
    assert first.command == (
        f"gcloud run services update-traffic ata-orders-api --to-revisions={ORDERS_PREV_REV}=100 --region=us-west1"
    )
    assert f"recent revision {ORDERS_BAD_REV} not implicated" not in report.observations
    assert "explained by upstream ata-inventory-api" not in report.observations


def test_assess_change_ignores_unalerted_upstream_without_measured_breach(caplog):
    caplog.set_level(logging.INFO)
    alert = _event(_entry("0.r1-unit", "ata-orders-5xx", "ata-orders-api", "5xx"))
    dossiers = {
        "ChangeSpecialist": EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-orders-api",
            metrics={"change_service": "ata-orders-api", "recent_revision": ORDERS_BAD_REV},
            anomaly_detected=True,
        ),
        "ata-inventory-api": EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource="ata-inventory-api",
            metrics={"has_db_auth_error": True, "error_rate_5xx": "N/A (not captured)"},
            anomaly_detected=True,
        ),
    }

    verdict = SynthesizerAgent()._assess_change(alert, dossiers, ["ata-inventory-api"], None, onset_tolerance_seconds=300)

    assert verdict["implicated"] is True
    assert any("no measured 5xx/latency breach" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize(
    "metrics",
    [
        {"latency_p95_ms": 6500.0, "metric_onset": ONSET},
        {"error_rate_5xx": "80.0%", "metric_onset": ONSET},
    ],
    ids=["latency", "5xx"],
)
def test_assess_change_unalerted_upstream_with_measured_breach_still_explains(metrics):
    alert = _event(_entry("0.r1-measured", "ata-orders-5xx", "ata-orders-api", "5xx"))
    dossiers = {
        "ChangeSpecialist": EvidenceDossier(
            specialist_name="ChangeSpecialist",
            target_resource="ata-orders-api",
            metrics={"change_service": "ata-orders-api", "recent_revision": ORDERS_BAD_REV},
            anomaly_detected=True,
        ),
        "ata-inventory-api": EvidenceDossier(
            specialist_name="CloudRunSpecialist", target_resource="ata-inventory-api",
            metrics=metrics, anomaly_detected=True,
        ),
    }

    verdict = SynthesizerAgent()._assess_change(alert, dossiers, ["ata-inventory-api"], None, onset_tolerance_seconds=300)

    assert verdict["implicated"] is False
    assert "explained by upstream ata-inventory-api" in verdict["reason"]


@pytest.mark.parametrize(
    "metrics, expected",
    [
        ({"error_rate_5xx": "80.0%"}, 1),
        ({"error_rate_5xx": "5.0%"}, 1),
        ({"error_rate_5xx": "4.9%"}, 0),
        ({"latency_p95_ms": 2000.0}, 1),
        ({"latency_p95_ms": 1999.0}, 0),
        ({"latency_p95_ms": 6500.0, "error_rate_5xx": "12.0%"}, 2),
        ({"error_rate_5xx": "N/A (not captured)", "latency_p95_ms": "N/A (not captured)"}, 0),
        ({"has_db_auth_error": True}, 0),
        ({}, 0),
        (None, 0),
    ],
)
def test_measured_breaches_counts_only_telemetry_over_threshold(metrics, expected):
    assert len(measured_breaches(metrics)) == expected


# ---------------------------------------------------------------------------
# Onset ordering
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "upstream, downstream, tolerance, allowed",
    [
        ("2026-10-27T06:02:30Z", "2026-10-27T06:01:52Z", 300, True),   # S4: DB 38 s later
        ("2026-10-27T06:06:52Z", "2026-10-27T06:01:52Z", 300, True),   # exactly at tolerance
        ("2026-10-27T06:06:53Z", "2026-10-27T06:01:52Z", 300, False),  # 1 s beyond
        ("2026-10-27T06:01:52Z", "2026-10-27T05:59:00Z", 120, False),  # review repro, tight tolerance
        ("2026-10-27T06:01:52Z", "2026-10-27T05:59:00Z", 300, True),   # review repro, default tolerance
        ("2026-10-27T05:00:00Z", "2026-10-27T06:01:52Z", 300, True),   # upstream first
        (None, "2026-10-27T06:01:52Z", 300, True),                     # unknown onset: no guard
        ("garbage", "2026-10-27T06:01:52Z", 300, True),
        ("2026-10-27T09:00:00Z", "2026-10-27T06:01:52Z", None, True),  # guard disabled
        ("2026-10-27T06:10:00+00:00", "2026-10-27T06:01:52Z", 300, False),
    ],
)
def test_onset_allows_explanation(upstream, downstream, tolerance, allowed):
    assert onset_allows_explanation(upstream, downstream, tolerance) is allowed


def test_select_origin_downstream_that_degraded_first_is_not_explained_by_upstream():
    orders = _entry("0.on-ord", "ata-orders-5xx", "ata-orders-api", "5xx", "2026-10-27T05:50:00Z")
    inventory = _entry("0.on-inv", "ata-inventory-latency", "ata-inventory-api", "latency", ONSET)

    selection = _select([inventory, orders], tolerance=300)
    cand = selection.candidate("ata-orders-api")

    assert cand.explained_by == []
    assert cand.preceded == ["ata-inventory-api"]
    assert selection.origin == "ata-orders-api"  # both unexplained: earliest onset
    assert any("before upstream ata-inventory-api" in r for r in selection.reasons)


def test_select_origin_within_tolerance_keeps_topology_explanation():
    db = _entry("0.db", "ata-cloudsql-connections-critical", "ata-inventory-db", "connections", "2026-10-27T06:02:30Z")
    selection = _select([S2_ORDERS, S2_INVENTORY_1, db], tolerance=300)

    assert selection.origin == "ata-inventory-db"
    assert selection.candidate("ata-orders-api").explained_by
    assert selection.candidate("ata-inventory-api").explained_by == ["ata-inventory-db"]


def test_deploy_on_service_that_degraded_well_before_upstream_stays_implicated(
    agent_cfg, clock, monkeypatch, telemetry
):
    orders = _entry("0.on-ord-e2e", "ata-orders-5xx", "ata-orders-api", "5xx", "2026-10-27T05:50:00Z")
    inventory = _entry("0.on-inv-e2e", "ata-inventory-latency", "ata-inventory-api", "latency", ONSET)
    telemetry["degraded"] = {"ata-orders-api": "5xx", "ata-inventory-api": "latency"}
    telemetry["changes"] = [_change("ata-orders-api", ORDERS_BAD_REV, ORDERS_PREV_REV, "2026-10-27T05:48:00Z")]

    report, _, _ = _ticket_report_for_group([orders, inventory], agent_cfg, clock, monkeypatch)

    assert report.origin == "ata-orders-api"
    assert ORDERS_BAD_REV in report.probable_root_cause.text
    assert f"--to-revisions={ORDERS_PREV_REV}=100" in report.get_normalized_actions()[0].command
    assert f"recent revision {ORDERS_BAD_REV} not implicated" not in report.observations


def test_onset_tolerance_comes_from_config(agent_cfg, telemetry):
    """The review's 2m52s repro is explained at the default tolerance and caught at 120 s."""
    orders = _entry("0.tol-ord", "ata-orders-5xx", "ata-orders-api", "5xx", "2026-10-27T05:59:00Z")
    inventory = _entry("0.tol-inv", "ata-inventory-latency", "ata-inventory-api", "latency", ONSET)
    telemetry["degraded"] = {"ata-orders-api": "5xx", "ata-inventory-api": "latency"}
    telemetry["changes"] = [_change("ata-orders-api", ORDERS_BAD_REV, ORDERS_PREV_REV, "2026-10-27T05:58:00Z")]

    def run(tolerance):
        cfg = agent_cfg.model_copy(update={"origin_onset_tolerance_seconds": tolerance})
        return asyncio.run(SynthesizerAgent().execute_investigation(
            _event(orders), "grp-tol", "incident-grp-tol.md", cfg, group_alerts=[orders, inventory]
        ))

    assert run(300.0).origin == "ata-inventory-api"
    tight = run(120.0)
    assert tight.origin == "ata-orders-api"
    assert ORDERS_BAD_REV in tight.probable_root_cause.text


def test_onset_tolerance_config_default_and_env(monkeypatch):
    assert AgentConfig().origin_onset_tolerance_seconds == 300.0
    monkeypatch.setenv("ORIGIN_ONSET_TOLERANCE_SECONDS", "90")
    assert AgentConfig().origin_onset_tolerance_seconds == 90.0


# ---------------------------------------------------------------------------
# Nits
# ---------------------------------------------------------------------------

def test_alert_event_from_entry_does_not_inherit_another_services_resource_type():
    trigger = _event(S2_ORDERS)  # resource_type cloud_run_revision
    proxy = _entry("0.proxy", "ata-apigee-orders-5xx", "ata-orders-v1", "5xx")

    event = alert_event_from_entry(proxy, trigger)

    assert event.resource_name == "ata-orders-v1"
    assert event.resource_type is None
    assert alert_event_from_entry(dict(proxy, resource_type="apigee_proxy"), trigger).resource_type == "apigee_proxy"
    assert alert_event_from_entry(S2_ORDERS, trigger) is trigger


def test_proxy_origin_alert_gets_apigee_console_link(agent_cfg):
    alert = AlertEvent(incident_id="0.px", policy_name="ata-apigee-orders-5xx", resource_name="ata-orders-v1")

    entry = SynthesizerAgent()._build_alerts(alert, agent_cfg)[0]

    assert "apigee" in entry["resource_url"]
    assert "run/detail" not in entry["resource_url"]


def test_main_entry_helper_uses_shared_builder_with_group_fallbacks():
    from agent.app.main import _alert_event_from_entry, _extract_primary_alert

    doc = OKFDocument(group_id="g1", policy_name="ata-orders-5xx", resource_name="ata-orders-api",
                      created_at="2026-10-27T06:00:00Z")
    sparse = {"incident_id": "0.sparse"}

    event = _alert_event_from_entry(sparse, doc)

    assert event.incident_id == "0.sparse"
    assert event.policy_name == "ata-orders-5xx"
    assert event.resource_name == "ata-orders-api"
    assert event.started_at == "2026-10-27T06:00:00Z"
    assert event.state == "open"
    assert _extract_primary_alert(doc).incident_id == "0.unknown"
    full = _alert_event_from_entry(S2_ORDERS, doc)
    assert (full.policy_name, full.resource_name, full.started_at) == ("ata-orders-latency", "ata-orders-api", ONSET)
    assert full.policy_user_labels["signal"] == "latency"


def test_single_service_not_implicated_change_is_labelled_change_assessment(agent_cfg, clock, monkeypatch, telemetry):
    """S3 group 2 shape: one service, a recent change, no degradation of its own."""
    rate_limit = _entry("0.s3-429", "ata-apigee-orders-429", "ata-orders-v1", "rate-limit")
    telemetry["changes"] = [_change("ata-orders-api", ORDERS_BAD_REV, ORDERS_PREV_REV, "2026-10-27T05:58:52Z")]

    report, store, group_id = _ticket_report_for_group([rate_limit], agent_cfg, clock, monkeypatch)
    notes = report.root_cause_analysis.get("origin_selection")

    assert notes is not None, "the not-implicated change must be explained"
    assert notes["label"] == "Change assessment"
    assert "Change assessment:" in report.observations
    assert "Origin selection:" not in report.observations
    assert "## Change assessment" in store.read(group_id).body


def test_multi_service_notes_keep_origin_selection_label(agent_cfg, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}

    report = asyncio.run(SynthesizerAgent().execute_investigation(
        _event(S2_ORDERS), "grp-lbl", "incident-grp-lbl.md", agent_cfg, group_alerts=[S2_ORDERS, S2_INVENTORY_1]
    ))

    assert report.root_cause_analysis["origin_selection"]["label"] == "Origin selection"
    assert "Origin selection:" in report.observations


def test_assess_change_runs_once_per_investigation(agent_cfg, telemetry, monkeypatch):
    telemetry["degraded"] = {"ata-orders-api": "5xx"}
    telemetry["changes"] = [ORDERS_BAD_DEPLOY]
    calls = []
    original = SynthesizerAgent._assess_change

    def counting(self, *args, **kwargs):
        calls.append(1)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(SynthesizerAgent, "_assess_change", counting)
    asyncio.run(SynthesizerAgent().execute_investigation(
        _event(_entry("0.once", "ata-orders-5xx", "ata-orders-api", "5xx")), "grp-once", "incident-grp-once.md", agent_cfg
    ))

    assert len(calls) == 1
