"""Groups stuck in 'investigating' without a ticket are recovered loudly and boundedly.

A settle wake moves a group to investigating and then tickets it. If that wake keeps failing
until ata-timer-queue's retry window ends, Cloud Tasks deletes it silently. Before this fix the
group then stayed investigating forever, with no ticket, and new alerts attached to it.

Covered here (agent/app/grouping/stuck_investigation.py, manager.py, main.py):
1. Stuck group + new alert: ERROR log, recovery recorded, a settle wake is re-enqueued, the
   alert joins the group, and the ticket created by the recovery wake says so.
2. Normal path unchanged: an investigation still within INVESTIGATION_STALE_SECONDS of its
   last wake attempt is not recovered, no extra wake is scheduled, and tickets carry no note.
3. Watchdog: armed once on entering investigating, re-arms while the wake is still retrying,
   recovers when stuck, and is ignored after the group is ticketed.
4. Bounded: after INVESTIGATION_MAX_RECOVERIES the group is degraded (ERROR) and new alerts
   open a new group. A failed recovery scheduling call uses up one attempt; it does not degrade
   the group (R1). Attempts are at least INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS apart, so
   a burst of triggers uses one attempt. The next group for the same scope lists the degraded
   group's alerts (O1).
5. Idempotent: two triggers in one stale period recover once.
6. Config: env overrides, validation, and the stale default exceeds the timer queue backoff
   plus the request timeout (drift guard against the Terraform default).
"""

import json
import logging
from pathlib import Path
import re

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.report import InvestigationReport
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.manager import GroupingManager
from agent.app.grouping.store import OKFStore
from agent.app.grouping.stuck_investigation import (
    RECOVERY_OBSERVATION_PREFIX,
    WATCHDOG_DEADLINE_TYPE,
    apply_recovery_note,
    is_stuck_investigation,
    recovery_observation,
)
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import TimerScheduleError, set_timer_scheduler_hook

STALE = 3600.0
INTERVAL = 300.0  # INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS
FOUNDATION_VARIABLES = Path(__file__).resolve().parents[1] / "infra" / "terraform" / "foundation" / "variables.tf"
CLOUD_RUN_DEFAULT_REQUEST_TIMEOUT_SECONDS = 300.0


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
        INVESTIGATION_STALE_SECONDS=STALE,
        INVESTIGATION_MAX_RECOVERIES=2,
        INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS=INTERVAL,
    )


class TimerRecorder:
    """Scheduler hook recording (deadline_type, epoch); can fail selected deadline types."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []
        self.fail_types: set[str] = set()

    def __call__(self, **kwargs):
        dtype = kwargs["deadline_type"]
        epoch = int(kwargs["deadline_dt"].timestamp())
        if dtype in self.fail_types:
            raise TimerScheduleError(f"cloud tasks unavailable ({dtype})")
        self.calls.append((dtype, epoch))
        return f"mock-task-{dtype}-{epoch}"

    def of(self, dtype: str) -> list[int]:
        return [e for t, e in self.calls if t == dtype]


@pytest.fixture
def clock():
    state = {"now": 1_000_000.0}
    timers = TimerRecorder()
    set_clock_func(lambda: state["now"])
    set_timer_scheduler_hook(timers)
    state["timers"] = timers
    yield state
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.pop(get_agent_settings, None)


class FakeBridge:
    """Bridge stand-in with group_id idempotency; ``down`` makes create_ticket fail."""

    def __init__(self) -> None:
        self.down = False
        self.calls = 0
        self.tickets: dict[str, TicketResponse] = {}
        self.reports: dict[str, InvestigationReport] = {}

    def create_ticket(self, bridge_url, request, config=None, http_client=None):
        self.calls += 1
        if self.down:
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
        self.reports[request.group_id] = request.report
        return ticket

    def append_work_note(self, bridge_url, request, config=None, http_client=None):
        return None


async def _stub_investigation(primary_alert, group_id, filename, settings, group_alerts=None):
    return InvestigationReport(group_id=group_id, summary="stub", observations="stub observation")


def _alert(n: int) -> AlertEvent:
    return AlertEvent(
        incident_id=f"0.stuck-alert-{n}",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-5xx",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:16:40Z",
    )


def _setup(agent_cfg, monkeypatch, bridge: FakeBridge):
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", bridge.create_ticket)
    monkeypatch.setattr(BridgeClient, "append_work_note", bridge.append_work_note)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _stub_investigation)
    return TestClient(app, raise_server_exceptions=False), OKFStore(config=agent_cfg)


def _triage(client: TestClient, n: int):
    return client.post("/triage", json=_alert(n).model_dump(mode="json"))


def _wake(client: TestClient, group_id: str, deadline_type: str, epoch: float):
    req = TimerWakeRequest(group_id=group_id, deadline_type=deadline_type, scheduled_epoch=int(epoch))
    return client.post("/timer/wake", json=req.model_dump(mode="json"))


def _stuck_group(client, store, clock, bridge) -> tuple[str, float]:
    """Open a group, fail its settle wake (bridge down), return (group_id, last attempt epoch)."""
    res = _triage(client, 1)
    assert res.status_code == 200
    group_id = res.json()["group_id"]
    deadline = store.read(group_id).settle_deadline_epoch()
    bridge.down = True
    clock["now"] = deadline
    assert _wake(client, group_id, "settle", deadline).status_code >= 500
    doc = store.read(group_id)
    assert doc.status == "investigating" and doc.servicenow_sys_id is None
    return group_id, deadline


def _stuck_logs(caplog) -> list[dict]:
    out = []
    for r in caplog.records:
        if r.levelno == logging.ERROR and r.getMessage().startswith("STUCK_INVESTIGATION: "):
            out.append(json.loads(r.getMessage().split(": ", 1)[1]))
    return out


# 1. Stuck group + new alert ---------------------------------------------------------------


def test_new_alert_on_stuck_group_recovers_it_loudly_and_reenqueues_settle(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    timers = clock["timers"]
    settles_before = len(timers.of("settle"))

    clock["now"] = last_attempt + STALE + 10.0
    with caplog.at_level(logging.ERROR):
        res = _triage(client, 2)

    assert res.status_code == 200
    assert res.json()["group_id"] == group_id
    doc = store.read(group_id)
    assert doc.status == "investigating"
    assert doc.has_alert("0.stuck-alert-2")
    assert len(doc.investigation_recoveries) == 1
    rec = doc.investigation_recoveries[0]
    assert rec["trigger"] == "alert 0.stuck-alert-2 ingested"
    assert rec["recovery_number"] == 1
    assert timers.of("settle")[settles_before:] == [int(clock["now"])]
    assert int(clock["now"] + STALE) in timers.of(WATCHDOG_DEADLINE_TYPE)
    logs = _stuck_logs(caplog)
    assert [(e["group_id"], e["action"]) for e in logs] == [(group_id, "recovered")]


def test_recovery_settle_wake_tickets_the_group_with_the_late_alert_and_a_recovery_note(
    agent_cfg, monkeypatch, clock
):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    assert _triage(client, 2).status_code == 200
    recovery_epoch = clock["timers"].of("settle")[-1]

    bridge.down = False
    clock["now"] = recovery_epoch + 1.0
    res = _wake(client, group_id, "settle", recovery_epoch)

    assert res.status_code == 200 and res.json()["status"] == "executed"
    doc = store.read(group_id)
    assert doc.servicenow_sys_id == "sys_1"
    report = bridge.reports[group_id]
    assert {"0.stuck-alert-1", "0.stuck-alert-2"} <= {a["incident_id"] for a in report.alerts}
    notes = [o for o in report.observations.split(" | ") if o.startswith(RECOVERY_OBSERVATION_PREFIX)]
    assert len(notes) == 1
    assert "alert 0.stuck-alert-2 ingested" in notes[0]
    assert f"{STALE:g}s" in notes[0]


def test_legacy_investigating_group_without_attempt_stamp_is_treated_as_stuck(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    store.update_atomically(group_id, lambda d: setattr(d, "investigation_started_at", None) or d)

    clock["now"] = last_attempt + 5.0
    with caplog.at_level(logging.ERROR):
        assert _triage(client, 2).status_code == 200

    rec = store.read(group_id).investigation_recoveries
    assert len(rec) == 1 and rec[0]["last_wake_attempt_at"] is None
    assert _stuck_logs(caplog)


def _schedule_failure_logs(caplog) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.levelno == logging.ERROR and "Failed to schedule the recovery settle wake" in r.getMessage()
    ]


def test_failed_recovery_settle_scheduling_on_ingest_consumes_one_attempt_and_keeps_the_group_stuck(
    agent_cfg, monkeypatch, clock, caplog
):
    # R1 (fresh-rev-s12): a Cloud Tasks blip during recovery uses up one recovery attempt; it
    # does not degrade the group, and it does not fail ingest of the alert that triggered the sweep.
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    clock["timers"].fail_types = {"settle"}

    with caplog.at_level(logging.ERROR):
        res = _triage(client, 2)

    assert res.status_code == 200
    assert res.json()["group_id"] == group_id  # still attachable: the alert is not split off
    doc = store.read(group_id)
    assert doc.status == "investigating" and doc.degraded is False
    assert doc.has_alert("0.stuck-alert-2")
    assert len(doc.investigation_recoveries) == 1
    rec = doc.investigation_recoveries[0]
    assert rec["recovery_number"] == 1
    assert rec["settle_wake_scheduled"] is False
    assert "cloud tasks unavailable" in rec["settle_wake_error"]
    # Detectable as stuck again: the attempt stamp is back to the last real wake attempt.
    assert doc.investigation_started_at == rec["last_wake_attempt_at"]
    assert is_stuck_investigation(doc, clock["now"], STALE)
    assert _schedule_failure_logs(caplog)


def test_next_ingest_after_a_failed_recovery_scheduling_retries_the_recovery(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    clock["timers"].fail_types = {"settle"}
    assert _triage(client, 2).status_code == 200

    clock["timers"].fail_types = set()
    clock["now"] += INTERVAL + 1.0
    settles_before = len(clock["timers"].of("settle"))
    with caplog.at_level(logging.ERROR):
        res = _triage(client, 3)

    assert res.status_code == 200 and res.json()["group_id"] == group_id
    doc = store.read(group_id)
    assert doc.status == "investigating"
    assert [r["recovery_number"] for r in doc.investigation_recoveries] == [1, 2]
    assert doc.investigation_recoveries[1].get("settle_wake_scheduled", True) is True
    assert clock["timers"].of("settle")[settles_before:] == [int(clock["now"])]
    assert not is_stuck_investigation(doc, clock["now"], STALE)

    # The recovery wake tickets the group; the note states the failed attempt as a fact.
    bridge.down = False
    clock["now"] += 1.0
    assert _wake(client, group_id, "settle", clock["timers"].of("settle")[-1]).json()["status"] == "executed"
    note = next(o for o in bridge.reports[group_id].observations.split(" | ")
                if o.startswith(RECOVERY_OBSERVATION_PREFIX))
    assert "its settle wake could not be scheduled" in note
    assert "alert 0.stuck-alert-3 ingested" in note


def test_watchdog_reraises_when_the_recovery_settle_cannot_be_scheduled_and_retries_after_the_interval(
    agent_cfg, monkeypatch, clock, caplog
):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, _ = _stuck_group(client, store, clock, bridge)
    watchdog_epoch = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[0]
    clock["now"] = watchdog_epoch
    clock["timers"].fail_types = {"settle"}

    with caplog.at_level(logging.ERROR):
        first = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)

    assert first.status_code >= 500  # Cloud Tasks retries the watchdog wake
    doc = store.read(group_id)
    assert doc.status == "investigating" and len(doc.investigation_recoveries) == 1
    assert is_stuck_investigation(doc, clock["now"], STALE)
    assert _schedule_failure_logs(caplog)

    # The Cloud Tasks retry seconds later does not use an attempt: it re-arms the watchdog for
    # the moment the next attempt is allowed and returns 200, so the retries stop.
    clock["timers"].fail_types = set()
    clock["now"] += 2.0
    retry = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)
    assert retry.status_code == 200
    assert retry.json() == {"status": "ignored", "group_id": group_id, "reason": "recovery_throttled"}
    assert len(store.read(group_id).investigation_recoveries) == 1
    next_epoch = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[-1]
    assert next_epoch == int(watchdog_epoch + INTERVAL)

    clock["now"] = next_epoch
    later = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, next_epoch)
    assert later.json()["reason"] == "stuck_investigation_recovered"
    doc = store.read(group_id)
    assert doc.status == "investigating" and len(doc.investigation_recoveries) == 2


def test_recovery_scheduling_failures_degrade_the_group_only_after_max_recoveries(
    agent_cfg, monkeypatch, clock, caplog
):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    clock["timers"].fail_types = {"settle"}

    for n in (2, 3):  # INVESTIGATION_MAX_RECOVERIES=2: two failed attempts, group kept
        clock["now"] += INTERVAL + 1.0
        res = _triage(client, n)
        assert res.status_code == 200 and res.json()["group_id"] == group_id
        assert store.read(group_id).status == "investigating"
    assert len(store.read(group_id).investigation_recoveries) == 2

    # Within the interval after the last attempt the group is not degraded yet.
    clock["now"] += INTERVAL - 1.0
    assert _triage(client, 4).json()["group_id"] == group_id
    assert store.read(group_id).status == "investigating"
    assert len(store.read(group_id).investigation_recoveries) == 2

    clock["timers"].fail_types = set()
    clock["now"] += 2.0
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        res = _triage(client, 5)

    assert res.status_code == 200
    assert res.json()["group_id"] != group_id
    stuck = store.read(group_id)
    assert stuck.status == "degraded" and stuck.degraded is True
    assert "2 of 2 recoveries already used" in stuck.degraded_reason
    assert [e["action"] for e in _stuck_logs(caplog)] == ["degraded"]
    ats = [r["at"] for r in stuck.investigation_recoveries]
    assert ats[1] > ats[0]


def _throttle_warnings(caplog, group_id: str) -> list[logging.LogRecord]:
    return [
        r for r in caplog.records
        if r.levelno == logging.WARNING and "RECOVERY_THROTTLED" in r.getMessage() and group_id in r.getMessage()
    ]


def test_burst_of_ingests_within_the_interval_consumes_exactly_one_attempt(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    clock["timers"].fail_types = {"settle"}

    with caplog.at_level(logging.WARNING):
        for n in range(2, 8):  # 6 alerts within 60 s
            res = _triage(client, n)
            assert res.status_code == 200 and res.json()["group_id"] == group_id
            clock["now"] += 10.0

    doc = store.read(group_id)
    assert doc.status == "investigating"
    assert len(doc.investigation_recoveries) == 1
    assert all(doc.has_alert(f"0.stuck-alert-{n}") for n in range(2, 8))  # every alert ingested
    assert len(_throttle_warnings(caplog, group_id)) == 1  # once per group per interval
    assert len(clock["timers"].of("settle")) == 1  # only the group's original settle (no new attempts)

    # After the interval the next trigger uses the second attempt (scheduling works again).
    clock["timers"].fail_types = set()
    clock["now"] = last_attempt + STALE + 10.0 + INTERVAL
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        assert _triage(client, 8).json()["group_id"] == group_id
    doc = store.read(group_id)
    assert [r["recovery_number"] for r in doc.investigation_recoveries] == [1, 2]
    assert _throttle_warnings(caplog, group_id) == []


def test_throttle_warning_is_logged_once_in_each_interval(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    t0 = last_attempt + STALE + 10.0
    clock["timers"].fail_types = {"settle"}

    with caplog.at_level(logging.WARNING):
        for offset in (0.0, 10.0, 20.0, INTERVAL + 1.0, INTERVAL + 11.0, INTERVAL + 21.0):
            clock["now"] = t0 + offset
            assert _triage(client, 2 + int(offset)).status_code == 200

    # Attempts at t0 and t0+INTERVAL+1; one throttle WARNING in each interval after them.
    assert len(store.read(group_id).investigation_recoveries) == 2
    assert len(_throttle_warnings(caplog, group_id)) == 2


def test_burst_of_watchdog_wakes_within_the_interval_consumes_exactly_one_attempt(
    agent_cfg, monkeypatch, clock, caplog
):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, _ = _stuck_group(client, store, clock, bridge)
    watchdog_epoch = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[0]
    clock["now"] = watchdog_epoch
    clock["timers"].fail_types = {"settle"}
    assert _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch).status_code >= 500
    clock["timers"].fail_types = set()

    with caplog.at_level(logging.WARNING):
        for delay in (1.0, 3.0, 7.0, 15.0, 31.0):  # Cloud Tasks backoff
            clock["now"] = watchdog_epoch + delay
            res = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)
            assert res.status_code == 200 and res.json()["reason"] == "recovery_throttled"

    assert len(store.read(group_id).investigation_recoveries) == 1
    # Every throttled wake re-arms the same single watchdog (same task name), at the allowed time.
    assert set(clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[1:]) == {int(watchdog_epoch + INTERVAL)}
    assert len(_throttle_warnings(caplog, group_id)) == 5  # each watchdog wake logs: at most 1 per backoff step


def test_recovery_observation_states_an_attempt_whose_settle_wake_was_not_scheduled():
    line = recovery_observation([
        {"at": "2026-10-24T01:00:00Z", "trigger": "investigation watchdog",
         "last_wake_attempt_at": "2026-10-24T00:00:00Z", "stale_seconds": 3600.0,
         "settle_wake_scheduled": False, "settle_wake_error": "TimerScheduleError: 503"},
        {"at": "2026-10-24T01:00:05Z", "trigger": "investigation watchdog",
         "last_wake_attempt_at": "2026-10-24T00:00:00Z", "stale_seconds": 3600.0},
    ])
    assert ("restart attempted at 2026-10-24T01:00:00Z (investigation watchdog; last wake attempt "
            "2026-10-24T00:00:00Z) but its settle wake could not be scheduled") in line
    assert "restarted at 2026-10-24T01:00:05Z" in line
    assert "503" not in line  # the error text stays in the file and the log, not the ticket


# O1 (fresh-rev-s12): alerts of a group degraded after the recovery limit are not dropped ----


def _degrade_after_max_recoveries(client, store, clock, bridge) -> str:
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    t = last_attempt
    for n in (2, 3):  # INVESTIGATION_MAX_RECOVERIES=2
        t += STALE + 10.0
        clock["now"] = t
        assert _triage(client, n).json()["group_id"] == group_id
    clock["now"] = t + STALE + 10.0
    return group_id


def test_degrade_error_log_lists_the_groups_alerts_and_policies(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _degrade_after_max_recoveries(client, store, clock, bridge)
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        assert _triage(client, 4).status_code == 200

    (log,) = [e for e in _stuck_logs(caplog) if e["action"] == "degraded"]
    assert log["group_id"] == group_id
    assert log["alerts"] == [
        {"incident_id": f"0.stuck-alert-{n}", "policy_name": _alert(n).policy_name} for n in (1, 2, 3)
    ]
    assert store.read(group_id).degraded_cause == "stuck_investigation"


def test_next_group_for_the_same_scope_tickets_the_degraded_groups_alerts(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    degraded_id = _degrade_after_max_recoveries(client, store, clock, bridge)

    res = _triage(client, 4)
    new_id = res.json()["group_id"]
    assert new_id != degraded_id
    assert store.read(degraded_id).carried_to_group_id == new_id
    new_doc = store.read(new_id)
    assert [p["group_id"] for p in new_doc.degraded_predecessors] == [degraded_id]

    bridge.down = False
    settle = store.read(new_id).settle_deadline_epoch()
    clock["now"] = settle
    assert _wake(client, new_id, "settle", settle).json()["status"] == "executed"

    notes = [o for o in bridge.reports[new_id].observations.split(" | ") if degraded_id in o]
    assert len(notes) == 1
    note = notes[0]
    policy = _alert(1).policy_name
    for n in (1, 2, 3):
        assert f"0.stuck-alert-{n} ({policy})" in note
    assert "investigation stuck; 2 recovery attempts failed" in note
    assert "0.stuck-alert-4" not in note  # the new group's own alert is not listed as dropped


def test_degraded_groups_alerts_are_carried_into_one_new_group_only(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    degraded_id = _degrade_after_max_recoveries(client, store, clock, bridge)
    first_new = _triage(client, 4).json()["group_id"]
    store.update_atomically(first_new, lambda d: setattr(d, "status", "resolved") or d)

    clock["now"] += 10.0
    second_new = _triage(client, 5).json()["group_id"]

    assert second_new not in (first_new, degraded_id)
    assert store.read(second_new).degraded_predecessors == []
    assert store.read(degraded_id).carried_to_group_id == first_new


def test_degraded_group_is_not_carried_into_a_group_for_an_unrelated_resource(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    degraded_id = _degrade_after_max_recoveries(client, store, clock, bridge)
    # Degrade it (sweep) via an alert on an unrelated resource: that alert's new group must not carry it.
    unrelated = _alert(4).model_copy(update={"resource_name": "ata-s2fix-unrelated",
                                             "policy_name": "projects/p/alertPolicies/ata-s2fix-unrelated-latency"})
    res = client.post("/triage", json=unrelated.model_dump(mode="json"))

    assert res.status_code == 200
    assert store.read(degraded_id).status == "degraded"
    assert store.read(res.json()["group_id"]).degraded_predecessors == []
    assert store.read(degraded_id).carried_to_group_id is None

    # A later group for the degraded group's resource still carries it.
    clock["now"] += 10.0
    later = _triage(client, 5).json()["group_id"]
    assert [p["group_id"] for p in store.read(later).degraded_predecessors] == [degraded_id]


def test_degraded_predecessor_observation_text():
    from agent.app.grouping.stuck_investigation import degraded_predecessor_observation

    line = degraded_predecessor_observation([
        {"group_id": "g-old", "reason": "investigation stuck; 3 recovery attempts failed",
         "alerts": [{"incident_id": "0.a", "policy_name": "pol-a"}, {"incident_id": "0.b", "policy_name": None}]},
    ])
    assert line == (
        "Degraded incident group g-old (investigation stuck; 3 recovery attempts failed) was not ticketed; "
        "its alerts: 0.a (pol-a), 0.b (policy not recorded)."
    )
    assert degraded_predecessor_observation([]) is None


# 2. Normal path unchanged -----------------------------------------------------------------


def test_alert_on_active_investigation_attaches_without_recovery(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    calls_before = list(clock["timers"].calls)

    clock["now"] = last_attempt + STALE - 10.0
    with caplog.at_level(logging.ERROR):
        res = _triage(client, 2)

    assert res.status_code == 200 and res.json()["group_id"] == group_id
    doc = store.read(group_id)
    assert doc.has_alert("0.stuck-alert-2")
    assert doc.investigation_recoveries == []
    assert clock["timers"].calls == calls_before
    assert _stuck_logs(caplog) == []


def test_each_retry_of_a_failing_wake_restamps_the_attempt_so_it_is_not_stale(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)

    clock["now"] = last_attempt + STALE - 60.0
    assert _wake(client, group_id, "settle", last_attempt).status_code >= 500
    clock["now"] = last_attempt + STALE + 10.0

    assert not is_stuck_investigation(store.read(group_id), clock["now"], STALE)
    assert _triage(client, 2).status_code == 200
    assert store.read(group_id).investigation_recoveries == []


def test_normal_settle_ticket_has_no_recovery_note_and_arms_one_watchdog(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _triage(client, 1).json()["group_id"]
    deadline = store.read(group_id).settle_deadline_epoch()
    clock["now"] = deadline

    res = _wake(client, group_id, "settle", deadline)

    assert res.json()["status"] == "executed"
    doc = store.read(group_id)
    assert doc.status == "ticketed" and doc.investigation_recoveries == []
    assert RECOVERY_OBSERVATION_PREFIX not in (bridge.reports[group_id].observations or "")
    assert clock["timers"].of(WATCHDOG_DEADLINE_TYPE) == [int(deadline + STALE)]


def test_watchdog_scheduling_failure_does_not_block_ticketing(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _triage(client, 1).json()["group_id"]
    deadline = store.read(group_id).settle_deadline_epoch()
    clock["now"] = deadline
    clock["timers"].fail_types = {WATCHDOG_DEADLINE_TYPE}

    with caplog.at_level(logging.ERROR):
        res = _wake(client, group_id, "settle", deadline)

    assert res.status_code == 200 and res.json()["status"] == "executed"
    assert store.read(group_id).servicenow_sys_id == "sys_1"
    assert any("investigation watchdog" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)


# 3. Watchdog ------------------------------------------------------------------------------


def test_retried_settle_wake_does_not_arm_another_watchdog(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + 60.0

    _wake(client, group_id, "settle", last_attempt)

    assert clock["timers"].of(WATCHDOG_DEADLINE_TYPE) == [int(last_attempt + STALE)]


def test_watchdog_rearms_while_the_wake_is_still_retrying(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    retry_at = last_attempt + 1800.0
    clock["now"] = retry_at
    _wake(client, group_id, "settle", last_attempt)
    watchdog_epoch = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[0]

    clock["now"] = watchdog_epoch
    res = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)

    assert res.json() == {"status": "ignored", "group_id": group_id, "reason": "investigation_active"}
    assert clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[-1] == int(retry_at + STALE)
    assert store.read(group_id).investigation_recoveries == []


def test_rearmed_watchdog_is_always_later_than_the_current_one(agent_cfg, monkeypatch, clock):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    watchdog_epoch = int(last_attempt + STALE)

    clock["now"] = watchdog_epoch - 1.0  # delivered a moment early: not stale yet
    res = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)

    assert res.json()["reason"] == "investigation_active"
    assert clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[-1] > watchdog_epoch


def test_watchdog_recovers_stuck_group_without_any_new_alert(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    watchdog_epoch = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[0]

    clock["now"] = watchdog_epoch
    with caplog.at_level(logging.ERROR):
        res = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, watchdog_epoch)

    assert res.json()["status"] == "executed"
    assert res.json()["reason"] == "stuck_investigation_recovered"
    assert store.read(group_id).investigation_recoveries[0]["trigger"] == "investigation watchdog"
    assert clock["timers"].of("settle")[-1] == int(watchdog_epoch)
    assert _stuck_logs(caplog)[0]["action"] == "recovered"

    bridge.down = False
    clock["now"] = watchdog_epoch + 1.0
    assert _wake(client, group_id, "settle", watchdog_epoch).json()["status"] == "executed"
    assert len(bridge.tickets) == 1

    next_watchdog = clock["timers"].of(WATCHDOG_DEADLINE_TYPE)[-1]
    clock["now"] = next_watchdog
    after = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, next_watchdog)
    assert after.json()["status"] == "ignored"
    assert after.json()["reason"] == "group_status_ticketed"


@pytest.mark.parametrize("status", ["open", "ticketed", "cooling_down", "degraded"])
def test_watchdog_ignores_groups_not_investigating(agent_cfg, monkeypatch, clock, status):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id = _triage(client, 1).json()["group_id"]
    store.update_atomically(group_id, lambda d: setattr(d, "status", status) or d)
    calls_before = list(clock["timers"].calls)
    clock["now"] += 10 * STALE

    res = _wake(client, group_id, WATCHDOG_DEADLINE_TYPE, clock["now"])

    assert res.json() == {"status": "ignored", "group_id": group_id, "reason": f"group_status_{status}"}
    assert clock["timers"].calls == calls_before
    assert store.read(group_id).status == status


# 4. Bounded -------------------------------------------------------------------------------


def test_group_is_degraded_after_max_recoveries_and_new_alert_opens_new_group(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    t = last_attempt
    for n in (2, 3):  # INVESTIGATION_MAX_RECOVERIES=2
        t += STALE + 10.0
        clock["now"] = t
        assert _triage(client, n).json()["group_id"] == group_id
    assert len(store.read(group_id).investigation_recoveries) == 2
    settles_before = len(clock["timers"].of("settle"))

    clock["now"] = t + STALE + 10.0
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        res = _triage(client, 4)

    assert res.status_code == 200
    assert res.json()["group_id"] != group_id
    stuck = store.read(group_id)
    assert stuck.status == "degraded" and stuck.degraded is True
    assert "2 of 2 recoveries already used" in stuck.degraded_reason
    assert not stuck.has_alert("0.stuck-alert-4")
    assert len(stuck.investigation_recoveries) == 2
    assert clock["timers"].of("settle")[settles_before:] == [int(clock["now"]) + 120]  # the new group's settle only
    logs = _stuck_logs(caplog)
    assert [e["action"] for e in logs] == ["degraded"]
    assert logs[0]["recoveries_used"] == 2 and "recovery_number" not in logs[0]


# 5. Idempotent ----------------------------------------------------------------------------


def test_two_triggers_in_one_stale_period_recover_once(agent_cfg, monkeypatch, clock, caplog):
    bridge = FakeBridge()
    client, store = _setup(agent_cfg, monkeypatch, bridge)
    group_id, last_attempt = _stuck_group(client, store, clock, bridge)
    clock["now"] = last_attempt + STALE + 10.0
    stale_view = store.read(group_id)

    with caplog.at_level(logging.ERROR):
        assert _triage(client, 2).status_code == 200
        clock["now"] += 5.0
        # A trigger holding the pre-recovery read (e.g. the watchdog) re-checks the fresh document.
        res = agent_main._investigation_watchdog(store, stale_view, clock["now"], agent_cfg, int(clock["now"]))
        assert _triage(client, 3).status_code == 200

    assert res.reason == "stuck_investigation_not_stuck"
    assert len(store.read(group_id).investigation_recoveries) == 1
    assert len(_stuck_logs(caplog)) == 1


def test_manager_ingest_with_injected_time_recovers_stuck_group(agent_cfg, clock):
    store = OKFStore(config=agent_cfg)

    class _Bridge:
        def append_work_note(self, **kwargs):
            return None

    mgr = GroupingManager(config=agent_cfg, store=store, bridge_client=_Bridge())
    created = mgr.ingest_alert(_alert(1), current_time=1000.0)
    store.update_atomically(
        created.group_id,
        lambda d: (setattr(d, "status", "investigating"), setattr(d, "investigation_started_at", "1970-01-01T00:16:40Z"))
        and d,
    )

    res = mgr.ingest_alert(_alert(2), current_time=1000.0 + STALE + 1.0)

    assert res.group_id == created.group_id
    assert len(store.read(created.group_id).investigation_recoveries) == 1


# Observation text -------------------------------------------------------------------------


def test_recovery_observation_states_each_recovery():
    recs = [
        {"at": "2026-10-28T01:00:00Z", "trigger": "investigation watchdog", "last_wake_attempt_at": "2026-10-28T00:00:00Z", "stale_seconds": 3600.0},
        {"at": "2026-10-28T02:30:00Z", "trigger": "alert 0.x ingested", "last_wake_attempt_at": None, "stale_seconds": 3600.0},
    ]
    line = recovery_observation(recs)
    assert line.startswith(RECOVERY_OBSERVATION_PREFIX)
    assert "3600s" in line
    assert "restarted at 2026-10-28T01:00:00Z (investigation watchdog; last wake attempt 2026-10-28T00:00:00Z)" in line
    assert "restarted at 2026-10-28T02:30:00Z (alert 0.x ingested; last wake attempt not recorded)" in line
    assert " | " not in line
    assert recovery_observation([]) is None and recovery_observation(None) is None


def test_apply_recovery_note_is_idempotent():
    report = InvestigationReport(group_id="g", summary="s", observations="a | b")
    recs = [{"at": "t1", "trigger": "investigation watchdog", "last_wake_attempt_at": "t0", "stale_seconds": 60}]
    apply_recovery_note(report, recs)
    apply_recovery_note(report, recs)
    items = report.observations.split(" | ")
    assert items[:2] == ["a", "b"]
    assert sum(i.startswith(RECOVERY_OBSERVATION_PREFIX) for i in items) == 1


# 6. Config --------------------------------------------------------------------------------


def test_stuck_investigation_settings_read_from_env(monkeypatch):
    monkeypatch.setenv("INVESTIGATION_STALE_SECONDS", "7200")
    monkeypatch.setenv("INVESTIGATION_MAX_RECOVERIES", "5")
    monkeypatch.setenv("INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS", "900")
    cfg = AgentConfig()
    assert cfg.investigation_stale_seconds == 7200.0
    assert cfg.investigation_max_recoveries == 5
    assert cfg.investigation_recovery_min_interval_seconds == 900.0


def test_recovery_min_interval_default():
    assert AgentConfig().investigation_recovery_min_interval_seconds == 300.0


@pytest.mark.parametrize(
    "env,value",
    [
        ("INVESTIGATION_STALE_SECONDS", "0"),
        ("INVESTIGATION_STALE_SECONDS", "-1"),
        ("INVESTIGATION_STALE_SECONDS", "soon"),
        ("INVESTIGATION_MAX_RECOVERIES", "0"),
        ("INVESTIGATION_MAX_RECOVERIES", "1.5"),
        ("INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS", "0"),
        ("INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS", "-5"),
        ("INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS", "later"),
    ],
)
def test_invalid_stuck_investigation_settings_are_rejected(monkeypatch, env, value):
    monkeypatch.setenv(env, value)
    with pytest.raises(ValidationError):
        AgentConfig()


def _tf_default_seconds(variable: str) -> float:
    text = FOUNDATION_VARIABLES.read_text()
    m = re.search(r'variable "%s"\s*\{[^}]*?default\s*=\s*"([0-9.]+)s"' % re.escape(variable), text, re.S)
    assert m, f"{variable} default not found in {FOUNDATION_VARIABLES}"
    return float(m.group(1))


def test_default_stale_threshold_exceeds_timer_backoff_plus_request_timeout():
    # Every retry of a failing wake re-stamps the attempt. Retries are at most
    # timer_queue_max_backoff apart and an attempt can run up to the request timeout,
    # so a smaller threshold could recover a group whose wake is still retrying.
    max_backoff = _tf_default_seconds("timer_queue_max_backoff")
    default = AgentConfig.model_fields["investigation_stale_seconds"].default
    assert default > max_backoff + CLOUD_RUN_DEFAULT_REQUEST_TIMEOUT_SECONDS
