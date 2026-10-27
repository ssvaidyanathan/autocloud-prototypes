"""Timer wakes must be safe to retry under the timer queue retry_config.

ata-timer-queue retries a failed /timer/wake for up to timer_queue_max_retry_duration,
so a wake can arrive long after its deadline, and the same wake can arrive again
after a failed or lost earlier attempt. These tests cover:
1. A late settle wake (an hour after the deadline) still tickets the group.
2. A late max-wait wake still tickets the group.
3. A settle wake retried after ticket creation failed tickets on the retry.
4. A wake retried after it already succeeded is ignored (no second ticket).
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.report import InvestigationReport
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.store import OKFStore
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook

LATE_SECONDS = 3600.0


@pytest.fixture
def agent_cfg(tmp_path: Path) -> AgentConfig:
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
    """Bridge create_ticket stand-in with the bridge's group_id idempotency."""

    def __init__(self, fail_first: int = 0) -> None:
        self.fail_first = fail_first
        self.calls = 0
        self.tickets: dict[str, TicketResponse] = {}

    def create_ticket(self, bridge_url, request, config=None, http_client=None):
        self.calls += 1
        if self.calls <= self.fail_first:
            raise RuntimeError("bridge unavailable")
        if request.group_id in self.tickets:
            return self.tickets[request.group_id].model_copy(update={"status": "already_exists"})
        n = len(self.tickets) + 1
        ticket = TicketResponse(
            status="created",
            sys_id=f"sys_{n}",
            number=f"INC{n:07d}",
            url=f"https://mock.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_{n}",
            group_id=request.group_id,
        )
        self.tickets[request.group_id] = ticket
        return ticket


def _open_group(client: TestClient) -> str:
    alert = AlertEvent(
        incident_id="0.retry-alert-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-5xx",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:16:40Z",
    )
    res = client.post("/triage", json=alert.model_dump(mode="json"))
    assert res.status_code == 200
    return res.json()["group_id"]


def _wake(client: TestClient, group_id: str, deadline_type: str, epoch: float):
    req = TimerWakeRequest(group_id=group_id, deadline_type=deadline_type, scheduled_epoch=int(epoch))
    return client.post("/timer/wake", json=req.model_dump(mode="json"))


async def _stub_investigation(primary_alert, group_id, filename, settings):
    # The investigation itself is out of scope here; these tests cover the wake state machine.
    return InvestigationReport(group_id=group_id, summary="stub", observations="stub")


def _setup(agent_cfg, monkeypatch, bridge: FakeBridge):
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", bridge.create_ticket)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _stub_investigation)
    return TestClient(app, raise_server_exceptions=False), OKFStore(config=agent_cfg)


@pytest.mark.parametrize("deadline_type", ["settle", "max_wait"])
def test_late_wake_still_tickets(agent_cfg, monkeypatch, clock, deadline_type):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _open_group(client)
    doc = store.read(group_id)
    deadline = doc.settle_deadline_epoch() if deadline_type == "settle" else doc.max_wait_deadline_epoch()

    clock["now"] = deadline + LATE_SECONDS
    res = _wake(client, group_id, deadline_type, deadline)

    assert res.status_code == 200
    assert res.json()["status"] == "executed"
    assert store.read(group_id).servicenow_number == "INC0000001"
    assert len(bridge.tickets) == 1


def test_settle_wake_retried_after_ticket_failure_tickets_once(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge(fail_first=1)
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _open_group(client)
    deadline = store.read(group_id).settle_deadline_epoch()

    # First attempt fails after the group moved to investigating; Cloud Tasks sees a 5xx and retries.
    clock["now"] = deadline
    first = _wake(client, group_id, "settle", deadline)
    assert first.status_code >= 500
    assert store.read(group_id).status == "investigating"
    assert store.read(group_id).servicenow_sys_id is None

    clock["now"] = deadline + 60.0
    retry = _wake(client, group_id, "settle", deadline)

    assert retry.status_code == 200
    assert retry.json()["status"] == "executed"
    assert store.read(group_id).servicenow_sys_id == "sys_1"
    assert len(bridge.tickets) == 1


@pytest.mark.parametrize("deadline_type", ["settle", "max_wait"])
def test_wake_retried_after_success_is_ignored(agent_cfg, monkeypatch, clock, deadline_type):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _open_group(client)
    doc = store.read(group_id)
    deadline = doc.settle_deadline_epoch() if deadline_type == "settle" else doc.max_wait_deadline_epoch()

    clock["now"] = deadline
    assert _wake(client, group_id, deadline_type, deadline).json()["status"] == "executed"

    # Same task delivered again (for example the first response was lost).
    clock["now"] = deadline + 60.0
    again = _wake(client, group_id, deadline_type, deadline)

    assert again.status_code == 200
    assert again.json()["status"] == "ignored"
    assert bridge.calls == 1
    assert store.read(group_id).servicenow_sys_id == "sys_1"
