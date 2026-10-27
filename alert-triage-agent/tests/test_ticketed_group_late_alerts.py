"""Alerts that join an already-ticketed group update the ServiceNow ticket, never silently.

QA round 2, S1 (INC0010328): a stray latency group (latency/p2, 1 alert) was ticketed as
'[P2] ata-inventory-api: Elevated latency (1 alert)'. S1's two 5xx/p1 alerts then joined it
as late alerts. The ticket kept the stale title, count (group had 3 alerts) and priority (P2
instead of P1); only "Correlated Alert Ingested" work notes were posted.

Behaviour under test:
1. When alerts join a ticketed group, the agent sends the bridge an update-fields instruction:
   the short description is rebuilt from the group (count == group alerts), the priority is
   raised when a new alert is more severe (never lowered) and a work note names the new alerts,
   the old and new title and the escalation.
2. Explicit time-distance rule: a NEW signal type whose onset is more than
   LATE_NEW_SIGNAL_MAX_ONSET_GAP_SECONDS after a ticketed group's onset opens a new group.

The group data below is the live S1 incident file
(archive/incident-ata-inventory-latency-ata-inventory-api-20261027t115439z.md).
"""

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from starlette.testclient import TestClient

from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import ActionStatusResponse, UpdateTicketFieldsRequest
from agent.app.contracts.report import InvestigationReport
from agent.app.grouping.late_alerts import (
    alert_priority,
    exclude_late_new_signal_groups,
    plan_ticket_refresh,
)
from agent.app.grouping.manager import GroupingManager
from agent.app.grouping.models import OKFDocument, format_okf_alert_entry
from agent.app.grouping.store import OKFStore
from agent.app.main import _note_alerts_missed_by_ticket
from agent.app.timers.scheduler import set_timer_scheduler_hook

S1_GROUP_ID = "ata-inventory-latency-ata-inventory-api-20261027t115439z"
S1_SYS_ID = "cd36deb793c453503f8ef29ddd03d6d2"
S1_TICKET_TITLE = "[P2] ata-inventory-api: Elevated latency (1 alert)"
# 2026-10-27T11:58:50Z, when both S1 5xx alerts opened (epoch seconds).
S1_5XX_EPOCH = 1793102330.0


def _labels(component: str, signal: str, severity: str) -> dict[str, str]:
    return {"app": "alert-triage-agent", "component": component, "severity": severity, "signal": signal, "tier": "service"}


def _stray_latency_entry() -> dict[str, Any]:
    return format_okf_alert_entry({
        "incident_id": "0.oebaamhy5evo",
        "policy_name": "ata-inventory-latency",
        "resource_name": "ata-inventory-api",
        "state": "closed",
        "started_at": "2026-10-27T11:54:39Z",
        "labels": _labels("ata-inventory-api", "latency", "p2"),
    })


def _alert(incident_id: str, policy: str, resource: str, signal: str, severity: str,
           started_at: str = "2026-10-27T11:58:50Z") -> AlertEvent:
    return AlertEvent(
        incident_id=incident_id,
        policy_name=policy,
        resource_name=resource,
        resource_type="cloud_run_revision",
        state="open",
        started_at=started_at,
        policy_user_labels=_labels(resource, signal, severity),
    )


def _orders_5xx(**kw) -> AlertEvent:
    return _alert("0.oebae2b4yuqz", "ata-orders-5xx", "ata-orders-api", "5xx", "p1", **kw)


def _inventory_5xx(**kw) -> AlertEvent:
    return _alert("0.oebae2b4yuqu", "ata-inventory-5xx", "ata-inventory-api", "5xx", "p1", **kw)


def _s1_group(status: str = "cooling_down", with_ticket_fields: bool = False) -> OKFDocument:
    """The stray latency group as stored when S1's 5xx alerts arrived (ticketed, alert closed)."""
    doc = OKFDocument(
        group_id=S1_GROUP_ID,
        status=status,
        project="cloud-assist-fde-2",
        policy_name="ata-inventory-latency",
        resource_name="ata-inventory-api",
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"],
        priority="P2",
        servicenow_sys_id=S1_SYS_ID,
        servicenow_number="INC0010328",
        created_at="2026-10-27T11:54:39Z",
        updated_at="2026-10-27T11:56:55Z",
        timestamp="2026-10-27T11:54:39Z",
        cooling_deadline="2026-10-27T11:59:55Z" if status == "cooling_down" else None,
        alerts=[_stray_latency_entry()],
    )
    if with_ticket_fields:
        doc.ticket_short_description = S1_TICKET_TITLE
        doc.ticket_priority = "P2"
        doc.ticket_title_problem = "Elevated latency"
        doc.ticket_origin = "ata-inventory-api"
    return doc


def _with_alerts(doc: OKFDocument, *alerts: AlertEvent) -> OKFDocument:
    for a in alerts:
        doc.add_alert(format_okf_alert_entry(a))
    return doc


class FakeBridge:
    """Bridge client double recording instructions (system boundary)."""

    def __init__(self, fail_update: bool = False) -> None:
        self.work_notes: list[Any] = []
        self.updates: list[UpdateTicketFieldsRequest] = []
        self.fail_update = fail_update

    def append_work_note(self, bridge_url, request, config=None, http_client=None):
        self.work_notes.append(request)
        return ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id)

    def update_ticket_fields(self, bridge_url, request, config=None, http_client=None):
        if self.fail_update:
            raise httpx.ConnectError("bridge unreachable")
        self.updates.append(request)
        return ActionStatusResponse(status="updated", sys_id=request.sys_id, group_id=request.group_id)


@pytest.fixture
def agent_cfg(tmp_path: Path) -> AgentConfig:
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True)
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        bridge_url="http://mock-bridge:8080",
        agent_url="http://mock-agent:8000",
        is_local=True,
    )


@pytest.fixture(autouse=True)
def _no_cloud_tasks():
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield
    set_timer_scheduler_hook(None)


def _manager(cfg: AgentConfig, doc: OKFDocument, bridge: FakeBridge) -> tuple[GroupingManager, OKFStore]:
    store = OKFStore(config=cfg)
    store.write(doc.group_id, doc)
    return GroupingManager(config=cfg, store=store, bridge_client=bridge), store


# ---------------------------------------------------------------------------
# 1. Planning the ticket refresh (pure)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("with_ticket_fields", [False, True], ids=["legacy-group-file", "ticket-fields-recorded"])
def test_s1_first_5xx_alert_escalates_ticket_to_p1_and_counts_two_alerts(with_ticket_fields):
    doc = _with_alerts(_s1_group(with_ticket_fields=with_ticket_fields), _orders_5xx())

    plan = plan_ticket_refresh(doc, ["0.oebae2b4yuqz"])

    assert plan.previous_short_description == S1_TICKET_TITLE
    assert plan.short_description == "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)"
    assert plan.previous_priority == "P2"
    assert plan.priority == "P1"
    assert plan.escalated is True


def test_s1_second_5xx_alert_counts_three_and_does_not_escalate_again():
    doc = _with_alerts(_s1_group(with_ticket_fields=True), _orders_5xx(), _inventory_5xx())
    doc.ticket_short_description = "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)"
    doc.ticket_priority = "P1"
    doc.ticket_title_problem = "Elevated HTTP 5xx error rate"

    plan = plan_ticket_refresh(doc, ["0.oebae2b4yuqu"])

    assert plan.short_description == "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (3 alerts)"
    assert plan.priority == "P1"
    assert plan.escalated is False


def test_both_s1_alerts_in_one_refresh_give_title_count_equal_to_group_alerts():
    doc = _with_alerts(_s1_group(), _orders_5xx(), _inventory_5xx())

    plan = plan_ticket_refresh(doc, ["0.oebae2b4yuqz", "0.oebae2b4yuqu"])

    assert plan.short_description.endswith(f"({len(doc.alerts)} alerts)")
    assert len(doc.alerts) == 3


def test_less_severe_late_alert_never_lowers_priority():
    doc = _s1_group(with_ticket_fields=True)
    doc.ticket_priority = "P1"
    doc.priority = "P1"
    doc.ticket_short_description = "[P1] ata-inventory-api: Elevated latency (1 alert)"
    _with_alerts(doc, _alert("0.later-latency", "ata-orders-latency", "ata-orders-api", "latency", "p2"))

    plan = plan_ticket_refresh(doc, ["0.later-latency"])

    assert plan.priority == "P1"
    assert plan.escalated is False
    assert plan.short_description == "[P1] ata-inventory-api: Elevated latency (2 alerts)"
    assert "never lowered" in plan.work_note


def test_same_severity_late_alert_keeps_title_problem_and_priority():
    doc = _with_alerts(
        _s1_group(with_ticket_fields=True),
        _alert("0.orders-lat", "ata-orders-latency", "ata-orders-api", "latency", "p2"),
    )

    plan = plan_ticket_refresh(doc, ["0.orders-lat"])

    assert plan.short_description == "[P2] ata-inventory-api: Elevated latency (2 alerts)"
    assert plan.escalated is False


def test_work_note_names_new_alerts_title_change_escalation_and_unchanged_rca():
    doc = _with_alerts(_s1_group(), _orders_5xx())

    note = plan_ticket_refresh(doc, ["0.oebae2b4yuqz"]).work_note

    assert "0.oebae2b4yuqz" in note and "ata-orders-5xx" in note
    assert S1_TICKET_TITLE in note
    assert "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)" in note
    assert "P2 -&gt; P1" in note or "P2 -> P1" in note
    assert "not re-run" in note


def test_no_refresh_planned_for_group_without_ticket():
    doc = _with_alerts(_s1_group(), _orders_5xx())
    doc.servicenow_sys_id = None

    assert plan_ticket_refresh(doc, ["0.oebae2b4yuqz"]) is None


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"labels": {"severity": "p1"}}, "P1"),
        ({"policy_user_labels": {"severity": "P2"}}, "P2"),
        ({"severity": "p3"}, "P3"),
        ({"labels": {"severity": "high"}}, None),
        ({}, None),
    ],
)
def test_alert_priority_reads_policy_severity_label(entry, expected):
    assert alert_priority(entry) == expected


# ---------------------------------------------------------------------------
# 2. GroupingManager: S1 replay against the ticketed (cooling-down) stray group
# ---------------------------------------------------------------------------

def test_s1_replay_updates_ticket_title_count_and_priority(agent_cfg):
    bridge = FakeBridge()
    manager, store = _manager(agent_cfg, _s1_group(), bridge)

    r1 = manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)
    r2 = manager.ingest_alert(_inventory_5xx(), current_time=S1_5XX_EPOCH + 5)

    assert r1.group_id == r2.group_id == S1_GROUP_ID
    assert [u.short_description for u in bridge.updates] == [
        "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)",
        "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (3 alerts)",
    ]
    # Priority is sent only with the escalation; the second update does not repeat it.
    assert [u.priority for u in bridge.updates] == ["P1", None]
    assert all(u.sys_id == S1_SYS_ID and u.group_id == S1_GROUP_ID for u in bridge.updates)

    stored = store.read(S1_GROUP_ID)
    assert len(stored.alerts) == 3
    assert stored.ticket_short_description.endswith("(3 alerts)")
    assert stored.ticket_priority == "P1"
    assert stored.priority == "P1"


def test_s1_replay_keeps_the_correlated_alert_work_notes(agent_cfg):
    bridge = FakeBridge()
    manager, _ = _manager(agent_cfg, _s1_group(), bridge)

    manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)

    assert len(bridge.work_notes) == 1
    assert "Correlated Alert Ingested" in bridge.work_notes[0].work_note


def test_duplicate_delivery_of_late_alert_sends_no_second_update(agent_cfg):
    bridge = FakeBridge()
    manager, _ = _manager(agent_cfg, _s1_group(), bridge)

    manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)
    manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH + 1)

    assert len(bridge.updates) == 1


def test_failed_ticket_update_is_logged_and_not_recorded_as_applied(agent_cfg, caplog):
    bridge = FakeBridge(fail_update=True)
    manager, store = _manager(agent_cfg, _s1_group(), bridge)

    result = manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)

    assert result.group_id == S1_GROUP_ID
    stored = store.read(S1_GROUP_ID)
    assert getattr(stored, "ticket_priority", None) is None
    assert any("INC0010328" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


def test_failed_escalation_is_applied_by_the_next_late_alert(agent_cfg):
    """predeploy3 R1: the P1 escalation failed (bridge error / rollback 404); the next, less
    severe late alert must still escalate the ticket and title from the whole group."""
    bridge = FakeBridge(fail_update=True)
    manager, store = _manager(agent_cfg, _s1_group(), bridge)
    manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)
    assert bridge.updates == [] and store.read(S1_GROUP_ID).ticket_priority is None

    bridge.fail_update = False
    later = _alert("0.later-latency", "ata-orders-latency", "ata-orders-api", "latency", "p2",
                   started_at="2026-10-27T11:59:30Z")
    manager.ingest_alert(later, current_time=S1_5XX_EPOCH + 40)

    assert len(bridge.updates) == 1
    update = bridge.updates[0]
    assert update.priority == "P1"
    assert update.short_description == "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (3 alerts)"
    assert "Priority unchanged" not in update.work_note
    assert "P2 -&gt; P1" in update.work_note
    assert "0.oebae2b4yuqz</code>, already in this group, has severity p1" in update.work_note
    stored = store.read(S1_GROUP_ID)
    assert stored.ticket_priority == "P1"
    assert stored.priority == "P1"


def test_plan_escalates_from_group_alert_when_ticket_priority_was_never_applied():
    doc = _with_alerts(
        _s1_group(with_ticket_fields=True),
        _orders_5xx(),
        _alert("0.later-latency", "ata-orders-latency", "ata-orders-api", "latency", "p2"),
    )
    # The 5xx escalation was never recorded: ticket fields still say P2 / latency.
    plan = plan_ticket_refresh(doc, ["0.later-latency"])
    assert plan.escalated is True
    assert plan.priority == "P1"
    assert plan.escalating_alert == "0.oebae2b4yuqz"
    assert plan.escalating_alert_is_new is False
    assert plan.title_problem == "Elevated HTTP 5xx error rate"
    assert plan.short_description == "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (3 alerts)"


def test_plan_names_new_alert_on_severity_tie():
    doc = _with_alerts(_s1_group(with_ticket_fields=True), _orders_5xx(), _inventory_5xx())
    plan = plan_ticket_refresh(doc, ["0.oebae2b4yuqu"])
    assert plan.escalating_alert == "0.oebae2b4yuqu" and plan.escalating_alert_is_new is True
    assert "new alert <code>0.oebae2b4yuqu</code>" in plan.work_note


def test_alert_attaching_to_unticketed_open_group_sends_no_ticket_update(agent_cfg):
    bridge = FakeBridge()
    doc = _s1_group(status="open")
    doc.servicenow_sys_id = None
    doc.servicenow_number = None
    doc.alerts[0]["state"] = "open"
    manager, _ = _manager(agent_cfg, doc, bridge)

    manager.ingest_alert(_inventory_5xx(started_at="2026-10-27T11:55:30Z"), current_time=S1_5XX_EPOCH - 200)

    assert bridge.updates == []


def test_alerts_joining_during_ticket_creation_also_refresh_the_ticket(agent_cfg):
    # The ticket was created from a report listing only the latency alert; 0.oebae2b4yuqz
    # joined while the ticket was being created (main._note_alerts_missed_by_ticket).
    bridge = FakeBridge()
    store = OKFStore(config=agent_cfg)
    doc = _with_alerts(_s1_group(status="ticketed"), _orders_5xx())
    store.write(doc.group_id, doc)
    report = InvestigationReport(
        group_id=S1_GROUP_ID, summary="Elevated latency", observations="", origin="ata-inventory-api",
        priority="P2", alerts=[_stray_latency_entry()],
    )

    _note_alerts_missed_by_ticket(report, doc, bridge, agent_cfg, store=store)

    assert len(bridge.updates) == 1
    assert bridge.updates[0].short_description == "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)"


# ---------------------------------------------------------------------------
# 3. Explicit time-distance rule for new signal types on ticketed groups
# ---------------------------------------------------------------------------

def test_new_signal_far_after_ticketed_group_onset_opens_new_group(agent_cfg):
    bridge = FakeBridge()
    manager, _ = _manager(agent_cfg, _s1_group(status="ticketed"), bridge)
    # 11:54:39Z + 15 min: no temporal evidence left (the scoring horizon is 10 min).
    late = _inventory_5xx(started_at="2026-10-27T12:09:40Z")

    result = manager.ingest_alert(late, current_time=S1_5XX_EPOCH + 650)

    assert result.action == "new_group"
    assert result.group_id != S1_GROUP_ID
    assert bridge.updates == []
    assert result.correlation["excluded_groups"][0]["group_id"] == S1_GROUP_ID


def test_same_signal_far_after_ticketed_group_onset_still_attaches(agent_cfg):
    bridge = FakeBridge()
    manager, _ = _manager(agent_cfg, _s1_group(status="ticketed"), bridge)
    recurrence = _alert("0.latency-again", "ata-inventory-latency", "ata-inventory-api", "latency", "p2",
                        started_at="2026-10-27T12:09:40Z")

    result = manager.ingest_alert(recurrence, current_time=S1_5XX_EPOCH + 650)

    assert result.group_id == S1_GROUP_ID


def test_s1_gap_is_within_the_default_window_so_s1_attaches_and_updates_ticket(agent_cfg):
    # S1: 11:54:39Z -> 11:58:50Z is 251 s; S5 (one genuine incident) had latency -> 5xx 413 s apart.
    assert agent_cfg.late_new_signal_max_onset_gap_seconds >= 413

    bridge = FakeBridge()
    manager, _ = _manager(agent_cfg, _s1_group(), bridge)
    result = manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)

    assert result.group_id == S1_GROUP_ID
    assert len(bridge.updates) == 1


def test_window_is_configurable_and_a_short_window_splits_s1(agent_cfg):
    cfg = agent_cfg.model_copy(update={"late_new_signal_max_onset_gap_seconds": 120.0})
    bridge = FakeBridge()
    manager, _ = _manager(cfg, _s1_group(), bridge)

    result = manager.ingest_alert(_orders_5xx(), current_time=S1_5XX_EPOCH)

    assert result.action == "new_group"
    assert bridge.updates == []


def test_window_reads_environment_variable(monkeypatch):
    monkeypatch.setenv("LATE_NEW_SIGNAL_MAX_ONSET_GAP_SECONDS", "240")

    assert AgentConfig().late_new_signal_max_onset_gap_seconds == 240.0


def test_rule_does_not_apply_to_groups_still_being_investigated():
    doc = _s1_group(status="open")
    doc.servicenow_sys_id = None

    kept, excluded = exclude_late_new_signal_groups(
        _inventory_5xx(started_at="2026-10-27T12:30:00Z"), [doc], window_seconds=60.0,
    )

    assert kept == [doc] and excluded == []


def test_rule_keeps_group_when_alert_onset_is_unknown():
    doc = _s1_group(status="ticketed")
    alert = _inventory_5xx()
    alert.started_at = None

    kept, excluded = exclude_late_new_signal_groups(alert, [doc], window_seconds=60.0)

    assert kept == [doc] and excluded == []


# ---------------------------------------------------------------------------
# 4. Bridge: /instructions/ticket/update-fields
# ---------------------------------------------------------------------------

@pytest.fixture
def bridge_client_and_patches():
    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig, reset_settings
    from bridge.app.main import app, set_servicenow_adapter

    config = BridgeConfig(
        project_id="cloud-assist-fde-2", name_prefix="ata", region="us-west1",
        servicenow_instance_url="https://mock.service-now.com",
        servicenow_username="admin", servicenow_password="password", is_local=True,
    )
    reset_settings(config)
    patches: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json
        if request.method == "PATCH" and "/api/now/table/incident/" in str(request.url):
            patches.append(json.loads(request.read()))
            return httpx.Response(200, json={"result": {}})
        return httpx.Response(404)

    adapter = ServiceNowAdapter(config=config, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    set_servicenow_adapter(adapter)
    with TestClient(app) as client:
        yield client, patches
    set_servicenow_adapter(None)
    reset_settings(None)


def test_bridge_update_fields_patches_title_priority_and_work_note(bridge_client_and_patches):
    client, patches = bridge_client_and_patches
    req = UpdateTicketFieldsRequest(
        sys_id=S1_SYS_ID, group_id=S1_GROUP_ID,
        short_description="[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)",
        priority="P1", work_note="[code]Ticket updated[/code]",
    )

    resp = client.post("/instructions/ticket/update-fields", headers={"Authorization": "Bearer valid-agent-token"},
                       json=req.model_dump(mode="json"))

    assert resp.status_code == 200
    assert resp.json()["status"] == "updated"
    assert patches == [{
        "short_description": "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)",
        "work_notes": "[code]Ticket updated[/code]",
        "urgency": "1",
        "impact": "1",
    }]


def test_bridge_update_fields_without_priority_leaves_urgency_and_impact(bridge_client_and_patches):
    client, patches = bridge_client_and_patches
    req = UpdateTicketFieldsRequest(
        sys_id=S1_SYS_ID, group_id=S1_GROUP_ID,
        short_description="[P1] ata-inventory-api: Elevated HTTP 5xx error rate (3 alerts)",
        work_note="[code]Ticket updated[/code]",
    )

    client.post("/instructions/ticket/update-fields", headers={"Authorization": "Bearer valid-agent-token"},
                json=req.model_dump(mode="json"))

    assert "urgency" not in patches[0] and "impact" not in patches[0]


def test_bridge_update_fields_requires_agent_auth(bridge_client_and_patches):
    client, _ = bridge_client_and_patches
    req = UpdateTicketFieldsRequest(sys_id=S1_SYS_ID, group_id=S1_GROUP_ID, short_description="x", work_note="y")

    assert client.post("/instructions/ticket/update-fields", json=req.model_dump(mode="json")).status_code == 401


def test_new_group_correlation_note_names_the_excluded_ticketed_group(agent_cfg):
    from agent.app.specialists.synthesizer import correlation_observation

    manager, _ = _manager(agent_cfg, _s1_group(status="ticketed"), FakeBridge())
    result = manager.ingest_alert(_inventory_5xx(started_at="2026-10-27T12:09:40Z"), current_time=S1_5XX_EPOCH + 650)

    note = correlation_observation(result.correlation)

    assert "no other open incident group" not in note
    assert S1_GROUP_ID in note
    assert "901s" in note and "600s window" in note
