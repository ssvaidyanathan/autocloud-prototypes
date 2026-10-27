"""Cool-down expiry must not auto-resolve ServiceNow tickets unless AUTO_RESOLVE_ON_RECOVERY=true.

Monitoring recovery is not resolution: in production low traffic can mask a live fault.
With the flag off (the default) a cooling wake on a ticketed group:
1. does not call ticket/resolve,
2. posts exactly one "please verify and resolve" work note,
3. leaves the ServiceNow state alone (no resolve instruction is sent at all),
4. moves the group to the terminal RECOVERED state,
5. so a new alert for the same resource opens a fresh group,
6. and a retried cooling wake posts no second note.
With the flag on, today's resolve behaviour is unchanged.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import ActionStatusResponse, TicketResponse
from agent.app.contracts.report import InvestigationReport
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.models import COOLING_WINDOW_SECONDS, IncidentGroupStatus
from agent.app.grouping.store import OKFStore
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook

SYS_ID = "sys_recovery_1"


@pytest.fixture
def base_cfg(tmp_path: Path) -> AgentConfig:
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(store_dir),
        fallback_store_path=str(store_dir),
        bridge_url="http://mock-bridge:8080",
        agent_url="http://mock-agent:8000",
        timer_queue_name="ata-timer-queue",
        is_local=True,
    )


@pytest.fixture
def clock():
    state = {"now": 1000.0}
    set_clock_func(lambda: state["now"])
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield state
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.pop(get_agent_settings, None)


class FakeBridge:
    """Records every instruction the agent sends to the bridge."""

    def __init__(self, fail_work_notes: int = 0) -> None:
        self.fail_work_notes = fail_work_notes
        self.work_note_attempts = 0
        self.work_notes: list = []
        self.resolves: list = []

    def create_ticket(self, bridge_url, request, config=None, http_client=None):
        return TicketResponse(
            status="created",
            sys_id=SYS_ID,
            number="INC0000001",
            url=f"https://mock.service-now.com/nav_to.do?uri=incident.do?sys_id={SYS_ID}",
            group_id=request.group_id,
        )

    def append_work_note(self, bridge_url, request, config=None, http_client=None):
        self.work_note_attempts += 1
        if self.work_note_attempts <= self.fail_work_notes:
            raise RuntimeError("bridge unavailable")
        self.work_notes.append(request)
        return ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id)

    def resolve_ticket(self, bridge_url, request, config=None, http_client=None):
        self.resolves.append(request)
        return ActionStatusResponse(status="resolved", sys_id=request.sys_id, group_id=request.group_id)


async def _stub_investigation(primary_alert, group_id, filename, settings):
    return InvestigationReport(group_id=group_id, summary="stub", observations="stub")


def _alert(incident_id: str, state: str) -> dict:
    return AlertEvent(
        incident_id=incident_id,
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-5xx",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state=state,
        started_at="2026-10-24T00:16:40Z",
    ).model_dump(mode="json")


def _wake(client: TestClient, group_id: str, deadline_type: str, epoch: float):
    req = TimerWakeRequest(group_id=group_id, deadline_type=deadline_type, scheduled_epoch=int(epoch))
    return client.post("/timer/wake", json=req.model_dump(mode="json"))


def _setup(cfg: AgentConfig, monkeypatch, bridge: FakeBridge):
    app.dependency_overrides[get_agent_settings] = lambda: cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", bridge.create_ticket)
    monkeypatch.setattr(BridgeClient, "append_work_note", bridge.append_work_note)
    monkeypatch.setattr(BridgeClient, "resolve_ticket", bridge.resolve_ticket)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _stub_investigation)
    return TestClient(app, raise_server_exceptions=False), OKFStore(config=cfg)


def _drive_to_cooling(client: TestClient, store: OKFStore, clock) -> tuple[str, float]:
    """Open alert -> settle wake tickets the group -> alert closes -> group cooling_down."""
    res = client.post("/triage", json=_alert("0.recovery-alert-1", "open"))
    assert res.status_code == 200
    group_id = res.json()["group_id"]

    clock["now"] = store.read(group_id).settle_deadline_epoch()
    assert _wake(client, group_id, "settle", clock["now"]).json()["status"] == "executed"
    assert store.read(group_id).servicenow_sys_id == SYS_ID

    clock["now"] += 30.0
    closed_at = clock["now"]
    assert client.post("/triage", json=_alert("0.recovery-alert-1", "closed")).status_code == 200
    doc = store.read(group_id)
    assert doc.status == IncidentGroupStatus.COOLING_DOWN.value
    assert doc.cooling_deadline_epoch() == pytest.approx(closed_at + COOLING_WINDOW_SECONDS)
    return group_id, closed_at


def test_flag_defaults_to_false(monkeypatch):
    monkeypatch.delenv("AUTO_RESOLVE_ON_RECOVERY", raising=False)
    assert AgentConfig().auto_resolve_on_recovery is False
    monkeypatch.setenv("AUTO_RESOLVE_ON_RECOVERY", "true")
    assert AgentConfig().auto_resolve_on_recovery is True


def test_cooling_expiry_posts_note_and_does_not_resolve(base_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, closed_at = _drive_to_cooling(client, store, clock)
    notes_before = len(bridge.work_notes)

    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    with caplog.at_level("INFO", logger="agent.app.main"):
        resp = _wake(client, group_id, "cooling", clock["now"])
    assert resp.status_code == 200
    assert resp.json()["status"] == "executed"
    assert resp.json()["reason"] == "recovered_not_resolved"

    assert bridge.resolves == []
    new_notes = bridge.work_notes[notes_before:]
    assert len(new_notes) == 1
    assert new_notes[0].sys_id == SYS_ID
    recovered_iso = store.read(group_id).recovered_at
    assert new_notes[0].work_note == (
        f"Monitoring recovered at {recovered_iso}: all 1 alerts closed and the 180s cool-down passed "
        "with no recurrence. No remediation was recorded by the agent. "
        "Please verify the fix and resolve this incident."
    )

    doc = store.read(group_id)
    assert doc.status == IncidentGroupStatus.RECOVERED.value
    assert doc.servicenow_sys_id == SYS_ID
    assert doc.recovery_note_posted_at is not None
    assert doc.recovered_at == agent_main.datetime.fromtimestamp(
        closed_at, tz=agent_main.timezone.utc
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    assert any(
        "auto_resolve_on_recovery=false" in r.getMessage() and r.levelname == "INFO" for r in caplog.records
    )


def test_retried_cooling_wake_posts_no_second_note(base_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    clock["now"] = store.read(group_id).cooling_deadline_epoch()

    assert _wake(client, group_id, "cooling", clock["now"]).json()["status"] == "executed"
    notes_after_first = len(bridge.work_notes)

    clock["now"] += 60.0
    retry = _wake(client, group_id, "cooling", clock["now"])
    assert retry.status_code == 200
    assert retry.json()["status"] == "ignored"
    assert retry.json()["reason"] == "group_status_recovered"
    assert len(bridge.work_notes) == notes_after_first
    assert bridge.resolves == []
    assert store.read(group_id).status == IncidentGroupStatus.RECOVERED.value


def test_failed_note_is_retried_not_dropped(base_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    bridge.fail_work_notes = bridge.work_note_attempts + 1
    notes_before = len(bridge.work_notes)

    failed = _wake(client, group_id, "cooling", clock["now"])
    assert failed.status_code == 500
    doc = store.read(group_id)
    assert doc.status == IncidentGroupStatus.COOLING_DOWN.value
    assert doc.recovered_at is None
    assert doc.recovery_note_posted_at is None

    retried = _wake(client, group_id, "cooling", clock["now"] + 10.0)
    assert retried.json()["status"] == "executed"
    assert len(bridge.work_notes) == notes_before + 1
    assert bridge.resolves == []
    assert store.read(group_id).status == IncidentGroupStatus.RECOVERED.value


def test_new_alert_after_recovery_opens_new_group(base_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    assert _wake(client, group_id, "cooling", clock["now"]).json()["status"] == "executed"

    clock["now"] += 30.0
    res = client.post("/triage", json=_alert("0.recovery-alert-2", "open"))
    assert res.status_code == 200
    new_group_id = res.json()["group_id"]
    assert new_group_id != group_id

    old = store.read(group_id)
    assert old.status == IncidentGroupStatus.RECOVERED.value
    assert not old.has_alert("0.recovery-alert-2")
    new = store.read(new_group_id)
    assert new.status == IncidentGroupStatus.OPEN.value
    assert new.servicenow_sys_id is None


def test_flag_true_keeps_auto_resolve(base_cfg, monkeypatch, clock):
    cfg = base_cfg.model_copy(update={"auto_resolve_on_recovery": True})
    bridge = FakeBridge()
    client, store = _setup(cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    notes_before = len(bridge.work_notes)

    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    resp = _wake(client, group_id, "cooling", clock["now"])
    assert resp.json()["status"] == "executed"

    assert len(bridge.resolves) == 1
    assert bridge.resolves[0].sys_id == SYS_ID
    assert bridge.resolves[0].close_notes == (
        "Incident resolved. All monitoring conditions recovered and cool-down expired."
    )
    assert len(bridge.work_notes) == notes_before
    doc = store.read(group_id)
    assert doc.status == IncidentGroupStatus.RESOLVED.value
    assert doc.recovered_at is None


def test_concurrent_cooling_wakes_post_exactly_one_note(base_cfg, monkeypatch, clock):
    """Two wakes that both passed the cooling_down pre-check with the same stale doc."""
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    stale_doc = store.read(group_id)
    notes_before = len(bridge.work_notes)

    first = agent_main._mark_recovered_without_resolving(store, stale_doc, clock["now"], base_cfg)
    second = agent_main._mark_recovered_without_resolving(store, stale_doc, clock["now"], base_cfg)

    assert (first.status, first.reason) == ("executed", "recovered_not_resolved")
    assert (second.status, second.reason) == ("ignored", "group_status_recovered")
    assert len(bridge.work_notes) == notes_before + 1
    assert bridge.resolves == []
    assert store.read(group_id).status == IncidentGroupStatus.RECOVERED.value


def test_failed_note_and_failed_release_logs_stuck_group(base_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(base_cfg, monkeypatch, bridge)
    group_id, _ = _drive_to_cooling(client, store, clock)
    clock["now"] = store.read(group_id).cooling_deadline_epoch()
    doc = store.read(group_id)
    bridge.fail_work_notes = bridge.work_note_attempts + 1

    real_update = OKFStore.update_atomically
    calls = {"n": 0}

    def flaky_update(self, gid, fn):
        calls["n"] += 1
        if calls["n"] == 2:  # the release after the failed note
            raise RuntimeError("precondition retries exhausted")
        return real_update(self, gid, fn)

    monkeypatch.setattr(OKFStore, "update_atomically", flaky_update)
    with caplog.at_level("ERROR", logger="agent.app.main"):
        with pytest.raises(RuntimeError, match="bridge unavailable"):
            agent_main._mark_recovered_without_resolving(store, doc, clock["now"], base_cfg)

    stuck = [r for r in caplog.records if "claim NOT released" in r.getMessage()]
    assert len(stuck) == 1
    assert stuck[0].levelname == "ERROR"
    assert group_id in stuck[0].getMessage()
    assert "precondition retries exhausted" in stuck[0].getMessage()
    assert store.read(group_id).status == IncidentGroupStatus.RECOVERED.value
    assert store.read(group_id).recovery_note_posted_at is None
