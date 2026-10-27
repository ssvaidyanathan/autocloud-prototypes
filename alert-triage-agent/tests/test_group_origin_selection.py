"""Group-wide, topology-aware origin selection (S2 misdiagnosis, INC0010320).

Live S2 (6.5 s latency injected into ata-inventory-api): the group held ata-orders-latency
(first, 06:01:52Z) plus two ata-inventory-latency alerts. /timer/wake investigated alerts[0]
only, the change specialist found ata-orders-api-00004-rgg (an unrelated redeploy at 04:45Z),
and the ticket blamed 'Deployment regression on ata-orders-api' (81%) and recommended rolling
orders-api back. Expected: origin ata-inventory-api, blast radius orders-api plus both Apigee
proxies, and the orders revision stated as not implicated.

The specialists' telemetry sources are stubbed (no live GCP); the ChangeSpecialist runs for
real on a stubbed change timeline, the topology is the real TOPOLOGY.md.
"""

from datetime import datetime, timezone
import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

import tools.app.server as tools_server
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.store import OKFStore
from agent.app.main import app, set_clock_func
from agent.app.specialists.apigee import ApigeeSpecialist
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.cloud_run import CloudRunSpecialist
from agent.app.specialists.cloud_sql import CloudSQLSpecialist
from agent.app.specialists.origin_selection import select_origin
from agent.app.specialists.synthesizer import SynthesizerAgent
from agent.app.timers.scheduler import set_timer_scheduler_hook
from bridge.app.contracts.report import InvestigationReport as BridgeReport
from topology.topology_graph import get_loader

ONSET = "2026-10-27T06:01:52Z"
ONSET_EPOCH = datetime(2026, 10, 27, 6, 1, 52, tzinfo=timezone.utc).timestamp()

ORDERS_REV = "ata-orders-api-00004-rgg"
ORDERS_PREV_REV = "ata-orders-api-00003-njk"
INVENTORY_BAD_REV = "ata-inventory-api-00062-bad"
INVENTORY_GOOD_REV = "ata-inventory-api-00061-good"


def _labels(component: str, signal: str, severity: str = "p2") -> dict:
    return {"app": "alert-triage-agent", "component": component, "severity": severity, "signal": signal, "tier": "service"}


def _entry(incident_id: str, policy: str, component: str, signal: str, started_at: str = ONSET) -> dict:
    return {
        "incident_id": incident_id,
        "policy_name": policy,
        "resource_name": component,
        "component": component,
        "signal": signal,
        "state": "open",
        "started_at": started_at,
        "labels": _labels(component, signal),
        "policy_user_labels": _labels(component, signal),
    }


# The live S2 group, in arrival order.
S2_ORDERS = _entry("0.oeb28lg5d27j", "ata-orders-latency", "ata-orders-api", "latency")
S2_INVENTORY_1 = _entry("0.oeb28lg5d27e", "ata-inventory-latency", "ata-inventory-api", "latency")
S2_INVENTORY_2 = _entry("0.oeb28lg5d279", "ata-inventory-latency", "ata-inventory-api", "latency")
S2_GROUP = [S2_ORDERS, S2_INVENTORY_1, S2_INVENTORY_2]


def _event(entry: dict) -> AlertEvent:
    return AlertEvent(
        incident_id=entry["incident_id"],
        policy_name=entry["policy_name"],
        resource_name=entry["resource_name"],
        resource_type="cloud_run_revision",
        state="open",
        started_at=entry["started_at"],
        policy_user_labels=entry["labels"],
        labels=entry["labels"],
    )


def _change(service: str, revision: str, previous: str, deployed_at: str) -> dict:
    return {
        "service_name": service,
        "revision_name": revision,
        "previous_revision": previous,
        "timestamp": deployed_at,
        "differs_from_previous": True,
        "is_serving": True,
        "source": "cloud_run_revision",
    }


# The live unrelated orders redeploy (04:45Z, 76 min before onset).
ORDERS_REDEPLOY = _change("ata-orders-api", ORDERS_REV, ORDERS_PREV_REV, "2026-10-27T04:45:14Z")


def _select(alerts):
    topo = get_loader()
    return select_origin(alerts, canonicalize=topo.canonicalize, upstream_of=topo.upstream)


# ---------------------------------------------------------------------------
# Fixtures: stubbed telemetry sources
# ---------------------------------------------------------------------------

@pytest.fixture
def telemetry(monkeypatch):
    """Configure which services show degraded Cloud Run telemetry and which changes exist."""
    state = {"degraded": {}, "changes": []}

    async def cloud_run(self, alert, config, target_service=None):
        target = target_service or alert.resource_name
        signal = state["degraded"].get(target)
        if signal == "latency":
            return EvidenceDossier(
                specialist_name="CloudRunSpecialist",
                target_resource=target,
                findings=[f"Elevated request latency detected on `{target}`: 6500.0ms (threshold: 2000ms)."],
                metrics={"latency_p95_ms": 6500.0, "error_rate_5xx": "0.0%", "metric_onset": ONSET},
                anomaly_detected=True,
            )
        if signal == "5xx":
            return EvidenceDossier(
                specialist_name="CloudRunSpecialist",
                target_resource=target,
                findings=[f"Elevated HTTP 5xx error rate detected on `{target}`: 80.0% of traffic breached threshold."],
                metrics={"error_rate_5xx": "80.0%", "metric_onset": ONSET},
                anomaly_detected=True,
            )
        return EvidenceDossier(
            specialist_name="CloudRunSpecialist",
            target_resource=target,
            findings=[f"Cloud Run service `{target}` operational telemetry evaluated."],
            metrics={"error_rate_5xx": "0.0%"},
            anomaly_detected=False,
        )

    async def apigee(self, alert, config):
        return EvidenceDossier(
            specialist_name="ApigeeSpecialist",
            target_resource=alert.resource_name,
            findings=["Apigee proxy traffic nominal."],
            metrics={"status_429_rate": "0.0%"},
            queries_executed={"cloud_logging": ["ata-apigee-access"]},
        )

    async def cloud_sql(self, alert, config):
        return EvidenceDossier(
            specialist_name="CloudSQLSpecialist",
            target_resource="ata-inventory-db",
            findings=["PostgreSQL metrics are nominal."],
            metrics={"active_connections": 4, "max_connections": 25, "active_lock_waits": 0, "cpu_utilization_pct": 5.0},
            queries_executed={"cloud_monitoring": ["cloudsql.googleapis.com/database/postgresql/num_backends"]},
        )

    def change_timeline(window_hours=2, project_id=None, region=None, **kwargs):
        return {"changes": list(state["changes"])}

    monkeypatch.setattr(CloudRunSpecialist, "investigate", cloud_run)
    monkeypatch.setattr(ApigeeSpecialist, "investigate", apigee)
    monkeypatch.setattr(CloudSQLSpecialist, "investigate", cloud_sql)
    monkeypatch.setattr(tools_server, "get_change_timeline", change_timeline)
    return state


@pytest.fixture
def agent_cfg(tmp_path: Path) -> AgentConfig:
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        bridge_url="http://mock-bridge:8080",
        agent_url="http://mock-agent:8000",
        timer_queue_name="ata-timer-queue",
        is_local=True,
    )


@pytest.fixture
def clock():
    state = {"now": ONSET_EPOCH}
    set_clock_func(lambda: state["now"])
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield state
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.pop(get_agent_settings, None)


def _ticket_report_for_group(entries, agent_cfg, clock, monkeypatch):
    """POST each alert to /triage (in order), fire the settle wake, return (report, store, group_id)."""
    sent = []

    def _create_ticket(self, bridge_url, request, config=None, http_client=None):
        sent.append(request)
        return TicketResponse(status="created", sys_id="sys_s2", number="INC0010320", url="https://sn/x", group_id=request.group_id)

    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", _create_ticket)
    monkeypatch.setattr(BridgeClient, "append_work_note", lambda self, bridge_url, request, **kw: None)
    client = TestClient(app, raise_server_exceptions=False)
    store = OKFStore(config=agent_cfg)

    group_id = None
    for entry in entries:
        res = client.post("/triage", json=_event(entry).model_dump(mode="json"))
        assert res.status_code == 200, res.text
        group_id = group_id or res.json()["group_id"]
        assert res.json()["group_id"] == group_id, "alerts must share one incident group"
        clock["now"] += 1

    doc = store.read(group_id)
    clock["now"] = doc.settle_deadline_epoch()
    res = client.post(
        "/timer/wake",
        json=TimerWakeRequest(group_id=group_id, deadline_type="settle", scheduled_epoch=int(clock["now"])).model_dump(mode="json"),
    )
    assert res.json()["status"] == "executed", res.text
    assert len(sent) == 1
    return sent[0].report, store, group_id


def _all_action_text(report) -> str:
    parts = []
    for a in report.get_normalized_actions():
        parts.extend([a.title or "", a.command or "", a.rollback or "", a.verification or ""])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 1. S2 regression (end to end through /timer/wake)
# ---------------------------------------------------------------------------

def test_s2_origin_is_upstream_inventory_not_first_alert_orders(agent_cfg, clock, monkeypatch, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report, store, group_id = _ticket_report_for_group(S2_GROUP, agent_cfg, clock, monkeypatch)

    assert report.origin == "ata-inventory-api"
    assert set(report.blast_radius) >= {"ata-inventory-api", "ata-orders-api", "ata-orders-v1", "ata-inventory-v1"}
    assert store.read(group_id).origin == "ata-inventory-api"


def test_s2_does_not_recommend_rolling_back_unimplicated_orders_api(agent_cfg, clock, monkeypatch, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report, _, _ = _ticket_report_for_group(S2_GROUP, agent_cfg, clock, monkeypatch)
    actions = _all_action_text(report)

    assert "update-traffic ata-orders-api" not in actions
    assert ORDERS_PREV_REV not in actions
    assert "--to-latest" not in actions
    assert "Deployment regression" not in report.summary
    assert ORDERS_REV not in report.probable_root_cause.text


def test_s2_report_explains_origin_selection(agent_cfg, clock, monkeypatch, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report, store, group_id = _ticket_report_for_group(S2_GROUP, agent_cfg, clock, monkeypatch)
    expected = (
        "ata-orders-api latency explained by upstream ata-inventory-api latency;"
        f" recent revision {ORDERS_REV} not implicated"
    )

    assert "Origin selection:" in report.observations
    assert expected in report.observations
    notes = report.root_cause_analysis["origin_selection"]
    assert notes["origin"] == "ata-inventory-api"
    assert [c["service"] for c in notes["candidates"]] == ["ata-inventory-api", "ata-orders-api"]
    assert any(ORDERS_REV in alt["alternative"] for alt in report.root_cause_analysis["alternatives_ruled_out"])
    # Rendered where people read it: the ServiceNow work note and the OKF report section.
    bridge_report = BridgeReport.model_validate(report.model_dump(mode="json"))
    assert "Origin selection:" in bridge_report.build_executive_summary_html()
    body = store.read(group_id).body
    assert "## Origin selection" in body
    assert expected in body


def test_s2_ticket_title_names_inventory_latency(agent_cfg, clock, monkeypatch, telemetry):
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report, _, _ = _ticket_report_for_group(S2_GROUP, agent_cfg, clock, monkeypatch)
    bridge_report = BridgeReport.model_validate(report.model_dump(mode="json"))

    assert bridge_report.build_short_description(max_length=160) == "[P2] ata-inventory-api: Elevated latency (3 alerts)"


def test_s2_origin_decision_logged_at_info_with_candidates_and_scores(agent_cfg, clock, monkeypatch, telemetry, caplog):
    caplog.set_level(logging.INFO)
    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    _ticket_report_for_group(S2_GROUP, agent_cfg, clock, monkeypatch)
    decisions = [
        r.getMessage() for r in caplog.records
        if r.name == "agent.app.specialists.origin_selection" and r.levelno == logging.INFO
    ]

    assert decisions
    assert "origin=ata-inventory-api" in decisions[-1]
    assert "first alert was on ata-orders-api" in decisions[-1]
    assert "'score'" in decisions[-1] and "ata-orders-api" in decisions[-1]


# ---------------------------------------------------------------------------
# 2. True upstream deploy: the deploy on the degraded origin IS implicated
# ---------------------------------------------------------------------------

def test_recent_bad_revision_on_degraded_upstream_is_implicated_and_rolled_back(agent_cfg, clock, monkeypatch, telemetry):
    orders = _entry("0.up-orders", "ata-orders-latency", "ata-orders-api", "latency")
    inventory = _entry("0.up-inventory", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx", "ata-orders-api": "latency"}
    telemetry["changes"] = [
        _change("ata-inventory-api", INVENTORY_BAD_REV, INVENTORY_GOOD_REV, "2026-10-27T05:55:00Z"),
        ORDERS_REDEPLOY,
    ]

    report, _, _ = _ticket_report_for_group([orders, inventory], agent_cfg, clock, monkeypatch)
    rollback = report.get_normalized_actions()[0]

    assert report.origin == "ata-inventory-api"
    assert INVENTORY_BAD_REV in report.probable_root_cause.text
    assert rollback.command == (
        f"gcloud run services update-traffic ata-inventory-api --to-revisions={INVENTORY_GOOD_REV}=100 --region=us-west1"
    )
    assert INVENTORY_BAD_REV in rollback.rollback  # undo names the explicit revision
    actions = _all_action_text(report)
    assert "--to-latest" not in actions
    assert "update-traffic ata-orders-api" not in actions
    assert f"recent revision {ORDERS_REV} not implicated" in report.observations


# ---------------------------------------------------------------------------
# 3. Single alert: unchanged
# ---------------------------------------------------------------------------

def test_single_alert_origin_and_report_unchanged(agent_cfg, clock, monkeypatch, telemetry):
    inventory = _entry("0.single", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}

    report, store, group_id = _ticket_report_for_group([inventory], agent_cfg, clock, monkeypatch)

    assert report.origin == "ata-inventory-api"
    assert "Origin selection" not in report.observations
    assert "origin_selection" not in report.root_cause_analysis
    assert "## Origin selection" not in store.read(group_id).body
    assert "--to-latest" not in _all_action_text(report)


def test_single_alert_bad_deploy_still_attributed_to_the_deploy(agent_cfg, clock, monkeypatch, telemetry):
    """S5 shape: one alert on the service that was just redeployed."""
    inventory = _entry("0.s5", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    telemetry["degraded"] = {"ata-inventory-api": "5xx"}
    telemetry["changes"] = [_change("ata-inventory-api", INVENTORY_BAD_REV, INVENTORY_GOOD_REV, "2026-10-27T05:55:00Z")]

    report, _, _ = _ticket_report_for_group([inventory], agent_cfg, clock, monkeypatch)

    assert report.origin == "ata-inventory-api"
    assert INVENTORY_BAD_REV in report.probable_root_cause.text
    assert f"--to-revisions={INVENTORY_GOOD_REV}=100" in report.get_normalized_actions()[0].command
    assert "--to-latest" not in _all_action_text(report)


def test_single_downstream_alert_with_degraded_upstream_does_not_blame_its_deploy(agent_cfg, telemetry):
    """Improved single-alert path: the orders deploy is explained away by inventory's own anomaly."""
    import asyncio

    telemetry["degraded"] = {"ata-inventory-api": "latency", "ata-orders-api": "latency"}
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report = asyncio.run(
        SynthesizerAgent().execute_investigation(_event(S2_ORDERS), "grp-single", "incident-grp-single.md", agent_cfg)
    )

    assert report.origin == "ata-inventory-api"
    assert "update-traffic ata-orders-api" not in _all_action_text(report)
    assert f"recent revision {ORDERS_REV} not implicated" in report.observations


# ---------------------------------------------------------------------------
# 4. Disjoint services: no topology edge, no cross-blame
# ---------------------------------------------------------------------------

def test_disjoint_services_are_not_cross_attributed(agent_cfg, clock, monkeypatch, telemetry):
    """ata-inventory-v1 and ata-orders-v1 share no dependency path; a deploy upstream of one is not blamed on the other."""
    inv_proxy = _entry("0.dj-inv", "ata-apigee-inventory-5xx", "ata-inventory-v1", "5xx")
    orders_proxy = _entry("0.dj-ord", "ata-apigee-orders-5xx", "ata-orders-v1", "5xx")
    telemetry["changes"] = [ORDERS_REDEPLOY]

    report = __import__("asyncio").run(
        SynthesizerAgent().execute_investigation(
            _event(inv_proxy), "grp-dj", "incident-grp-dj.md", agent_cfg, group_alerts=[inv_proxy, orders_proxy]
        )
    )

    assert report.origin == "ata-inventory-v1"
    assert "ata-orders-api" not in report.probable_root_cause.text
    assert "update-traffic ata-orders-api" not in _all_action_text(report)
    assert "no topology relationship" in report.observations


# ---------------------------------------------------------------------------
# 5. select_origin unit rules
# ---------------------------------------------------------------------------

def test_select_origin_s2_ranks_upstream_first_regardless_of_arrival():
    selection = _select(S2_GROUP)

    assert selection.origin == "ata-inventory-api"
    assert selection.primary_alert["incident_id"] == S2_INVENTORY_1["incident_id"]
    orders = selection.candidate("ata-orders-api")
    assert orders.explained_by == ["ata-inventory-api"]
    assert selection.candidate("ata-inventory-api").explains == ["ata-orders-api"]
    assert any("arrival order was not used" in r for r in selection.reasons)


def test_select_origin_single_service_follows_the_alert():
    selection = _select([S2_INVENTORY_1, S2_INVENTORY_2])
    assert selection.origin == "ata-inventory-api"
    assert not selection.is_multi_service


def test_select_origin_resolves_alias_labels_via_topology():
    aliased = dict(S2_INVENTORY_1, component="inventory-api", resource_name="inventory-api")
    aliased["labels"] = aliased["policy_user_labels"] = _labels("inventory-api", "latency")
    selection = _select([S2_ORDERS, aliased])
    assert selection.origin == "ata-inventory-api"


def test_select_origin_db_explains_whole_chain():
    db = _entry("0.db", "ata-inventory-db-connections", "ata-inventory-db", "connections", "2026-10-27T06:02:30Z")
    selection = _select([S2_ORDERS, S2_INVENTORY_1, db])
    assert selection.origin == "ata-inventory-db"
    assert [c.service for c in selection.candidates] == ["ata-inventory-db", "ata-inventory-api", "ata-orders-api"]


def test_select_origin_unrelated_services_use_onset_then_arrival():
    late = _entry("0.a", "ata-apigee-orders-5xx", "ata-orders-v1", "5xx", "2026-10-27T06:05:00Z")
    early = _entry("0.b", "ata-apigee-inventory-5xx", "ata-inventory-v1", "5xx", "2026-10-27T06:01:00Z")
    assert _select([late, early]).origin == "ata-inventory-v1"
    same = dict(early, started_at=late["started_at"])
    assert _select([late, same]).origin == "ata-orders-v1"


def test_select_origin_gateway_rate_limit_is_not_explained_by_backend():
    rate_limit = _entry("0.rl", "ata-apigee-orders-429", "ata-orders-v1", "rate-limit")
    backend = _entry("0.be", "ata-inventory-5xx", "ata-inventory-api", "5xx")
    selection = _select([rate_limit, backend])
    assert selection.candidate("ata-orders-v1").explained_by == []
    assert selection.origin == "ata-orders-v1"  # equal rank: arrival order


def test_select_origin_empty_group_returns_none():
    assert _select([]) is None
    assert _select(None) is None


# ---------------------------------------------------------------------------
# 6. Recommended actions: explicit revisions only, config-derived region
# ---------------------------------------------------------------------------

def _change_dossier(service: str, revision: str, previous: str) -> EvidenceDossier:
    return EvidenceDossier(
        specialist_name="ChangeSpecialist",
        target_resource=service,
        findings=[f"Recent revision deployment detected on `{service}` (5m ago): `{revision}`."],
        metrics={"recent_revision": revision, "previous_revision": previous, "change_service": service, "minutes_since_deploy": 5},
        anomaly_detected=True,
    )


def test_no_rollback_recommended_for_change_on_a_service_other_than_origin():
    alert = _event(S2_INVENTORY_1)
    actions = SynthesizerAgent()._synthesize_recommendations(
        [_change_dossier("ata-orders-api", ORDERS_REV, ORDERS_PREV_REV)], "ata-inventory-api", alert,
        region="us-west1", project_id="cloud-assist-fde-2",
    )
    text = "\n".join(f"{a.title} {a.command} {a.rollback}" for a in actions)
    assert "ata-orders-api" not in text
    assert "--to-latest" not in text


def test_generic_recommendation_never_routes_to_latest_revision():
    actions = SynthesizerAgent()._synthesize_recommendations(
        [], "ata-inventory-api", _event(S2_INVENTORY_1), region="us-west1", project_id="cloud-assist-fde-2"
    )
    assert actions
    for a in actions:
        assert "--to-latest" not in (a.command or "")
        assert "--to-latest" not in (a.rollback or "")
        assert "update-traffic" not in (a.command or "")


def test_rollback_names_explicit_revisions_and_reason():
    actions = SynthesizerAgent()._synthesize_recommendations(
        [_change_dossier("ata-inventory-api", INVENTORY_BAD_REV, INVENTORY_GOOD_REV)], "ata-inventory-api",
        _event(S2_INVENTORY_1), region="us-west1", project_id="cloud-assist-fde-2",
    )
    rollback = actions[0]
    assert f"--to-revisions={INVENTORY_GOOD_REV}=100" in rollback.command
    assert f"before {INVENTORY_BAD_REV} was deployed" in rollback.title
    assert f"--to-revisions={INVENTORY_BAD_REV}=100" in rollback.rollback
    assert "--to-latest" not in rollback.rollback


def test_recommendation_commands_use_configured_region():
    actions = SynthesizerAgent()._synthesize_recommendations(
        [_change_dossier("ata-inventory-api", INVENTORY_BAD_REV, INVENTORY_GOOD_REV)], "ata-inventory-api",
        _event(S2_INVENTORY_1), region="europe-west4", project_id="p",
    )
    commands = " ".join(a.command or "" for a in actions)
    assert "--region=europe-west4" in commands
    assert "us-west1" not in commands
