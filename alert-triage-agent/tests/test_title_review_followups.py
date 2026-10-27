"""Follow-ups from the ata-title review (title-review.md).

R1: a rejected title_problem and every fallback branch of resolve_title_problem are logged
    (WARNING) with the rejected value / branch, so a generic title is explainable from logs.
O1: an alert that attaches after the ticket-time re-read but before servicenow_sys_id is
    stored gets a late-alert work note once the ticket exists.
O2: the work note's Systems values and Problem text are HTML-escaped.
O3: a literal [code] / [/code] inside work-note text cannot end ServiceNow's [code] block.
"""

import logging
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.report import DEFAULT_TITLE_PROBLEM, InvestigationReport as AgentReport
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.manager import GroupingManager
from agent.app.grouping.models import format_okf_alert_entry
from agent.app.grouping.store import OKFStore
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import set_timer_scheduler_hook
from bridge.app.contracts.report import InvestigationReport as BridgeReport

REPORT_CLASSES = [AgentReport, BridgeReport]
CONTRACT_LOGGERS = {AgentReport: "agent.app.contracts.report", BridgeReport: "bridge.app.contracts.report"}


def _labels(component: str, signal: str = "5xx") -> dict:
    return {"app": "alert-triage-agent", "component": component, "severity": "p1", "signal": signal, "tier": "service"}


def _alert(incident_id: str, policy: str, component: str, signal: str = "5xx") -> dict:
    return {
        "incident_id": incident_id,
        "policy_name": policy,
        "resource_name": component,
        "component": component,
        "signal": signal,
        "state": "open",
        "started_at": "2026-10-27T05:44:10Z",
        "labels": _labels(component, signal),
        "policy_user_labels": _labels(component, signal),
    }


def _report(cls, **overrides):
    fields = dict(
        group_id="grp-1",
        summary="Elevated HTTP 5xx error rate on ata-inventory-api",
        observations="obs one | obs two",
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
        alerts=[_alert("0.a", "ata-inventory-5xx", "ata-inventory-api")],
    )
    fields.update(overrides)
    return cls(**fields)


def _warnings(caplog, logger_name):
    return [r for r in caplog.records if r.name == logger_name and r.levelno == logging.WARNING]


# ---------------------------------------------------------------------------
# R1: rejected title_problem values and fallback branches are logged
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
@pytest.mark.parametrize(
    "value,reason",
    [
        ("x" * 61, "len=61 > 60"),
        ("Supercalifragilisticexpialidocious-" + "x" * 27, "len=62 > 60"),
        ("unknown", "placeholder value"),
    ],
)
def test_rejected_title_problem_logs_warning_with_value_and_reason(ReportCls, value, reason, caplog):
    caplog.set_level(logging.DEBUG)
    report = _report(ReportCls, title_problem=value)

    assert report.title_problem is None
    warnings = _warnings(caplog, CONTRACT_LOGGERS[ReportCls])
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert "title_problem rejected" in message
    assert reason in message
    assert repr(value[:120]) in message


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_rejected_title_problem_log_truncates_value_to_120_chars(ReportCls, caplog):
    caplog.set_level(logging.WARNING)
    value = "y" * 500
    _report(ReportCls, title_problem=value)
    message = _warnings(caplog, CONTRACT_LOGGERS[ReportCls])[0].getMessage()
    assert repr("y" * 120) in message
    assert "y" * 121 not in message


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
@pytest.mark.parametrize("value", [None, "", "   "])
def test_missing_title_problem_is_not_logged_as_rejected(ReportCls, value, caplog):
    caplog.set_level(logging.DEBUG)
    _report(ReportCls, title_problem=value)
    assert not [r for r in _warnings(caplog, CONTRACT_LOGGERS[ReportCls]) if "rejected" in r.getMessage()]


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_valid_title_problem_logs_nothing(ReportCls, caplog):
    caplog.set_level(logging.DEBUG)
    report = _report(ReportCls, title_problem="Elevated latency")
    assert report.resolve_title_problem() == "Elevated latency"
    assert not [r for r in caplog.records if r.name == CONTRACT_LOGGERS[ReportCls]]


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_signal_derived_title_problem_logs_info_not_warning(ReportCls, caplog):
    caplog.set_level(logging.DEBUG)
    report = _report(ReportCls)
    assert report.resolve_title_problem() == "Elevated HTTP 5xx error rate"
    assert not _warnings(caplog, CONTRACT_LOGGERS[ReportCls])
    infos = [r for r in caplog.records if r.name == CONTRACT_LOGGERS[ReportCls] and r.levelno == logging.INFO]
    assert infos and "grp-1" in infos[0].getMessage()


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_default_title_problem_fallback_logs_warning_with_group_and_branch(ReportCls, caplog):
    caplog.set_level(logging.DEBUG)
    no_signal = {"incident_id": "0.z", "policy_name": "ata-custom-thing", "resource_name": "svc"}
    report = _report(
        ReportCls,
        alerts=[no_signal],
        summary="A long synthesized sentence about something odd happening that is well over sixty characters",
    )
    assert report.resolve_title_problem() == DEFAULT_TITLE_PROBLEM
    messages = [r.getMessage() for r in _warnings(caplog, CONTRACT_LOGGERS[ReportCls])]
    assert any("grp-1" in m and "default phrase" in m for m in messages)


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_issue_type_title_fallback_logs_warning_with_branch(ReportCls, caplog):
    caplog.set_level(logging.DEBUG)
    report = _report(ReportCls, alerts=[{"incident_id": "0.z", "policy_name": "p", "resource_name": "svc"}], issue_type="latency_spike")
    assert report.resolve_title_problem() == "Elevated latency"
    messages = [r.getMessage() for r in _warnings(caplog, CONTRACT_LOGGERS[ReportCls])]
    assert any("'issue_type'" in m and "grp-1" in m for m in messages)


def test_contract_copies_stay_byte_identical():
    root = Path(__file__).resolve().parents[1]
    agent = (root / "agent" / "app" / "contracts" / "report.py").read_bytes()
    bridge = (root / "bridge" / "app" / "contracts" / "report.py").read_bytes()
    assert agent == bridge


# ---------------------------------------------------------------------------
# O2 / O3: work-note escaping and [code] breakout hardening
# ---------------------------------------------------------------------------

def _systems_block(note: str) -> str:
    return note.split("<b>Systems:</b>", 1)[1].split("<b>Observations:</b>", 1)[0]


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_work_note_escapes_component_and_signal_values(ReportCls):
    alert = _alert("0.a", "ata-inventory-5xx", "<b>c</b>", signal="<i>s</i>")
    note = _report(ReportCls, alerts=[alert]).build_executive_summary_html()
    block = _systems_block(note)
    assert "Component: <code>&lt;b&gt;c&lt;/b&gt;</code>" in block
    assert "Signal: <code>&lt;i&gt;s&lt;/i&gt;</code>" in block
    assert "<b>c</b>" not in note


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_work_note_escapes_origin_and_blast_radius(ReportCls):
    note = _report(ReportCls, origin="<img src=x onerror=1>", blast_radius=["a&b", "<svc>"]).build_executive_summary_html()
    block = _systems_block(note)
    assert "<code>&lt;img src=x onerror=1&gt;</code>" in block
    assert "Blast Radius: a&amp;b, &lt;svc&gt;" in block
    assert "<img" not in note


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_work_note_escapes_problem_text(ReportCls):
    note = _report(ReportCls, summary="Errors <script>alert(1)</script> on svc").build_executive_summary_html()
    assert "&lt;script&gt;" in note
    assert "<script>" not in note


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
@pytest.mark.parametrize(
    "field,value",
    [
        ("observations", "log line [/code] then text | second [code]"),
        ("summary", "Problem [/code] breakout"),
        ("origin", "svc[/CODE]"),
        ("recommended_actions", ["Restart [/code] now"]),
    ],
)
def test_work_note_text_cannot_close_the_code_block(ReportCls, field, value):
    note = _report(ReportCls, **{field: value}).build_executive_summary_html()
    assert note.startswith("[code]\n")
    assert note.lower().count("[/code]") == 1
    assert note.lower().count("[code]") == 1
    assert note.endswith("[/code]")
    assert "&#91;/" in note


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_work_note_command_cannot_close_the_code_block(ReportCls):
    note = _report(ReportCls, recommended_actions=["gcloud run services describe x [/code]"]).build_executive_summary_html()
    assert note.lower().count("[/code]") == 1


@pytest.mark.parametrize("ReportCls", REPORT_CLASSES)
def test_work_note_plain_values_unchanged(ReportCls):
    """Escaping does not alter ordinary service names (S1 rendering stays the same)."""
    block = _systems_block(_report(ReportCls).build_executive_summary_html())
    assert "<li>Origin: <code>ata-inventory-api</code>" in block
    assert "<li>Component: <code>ata-inventory-api</code>, Signal: <code>5xx</code></li>" in block
    assert "<li>Blast Radius: ata-inventory-api, ata-orders-api</li>" in block


# ---------------------------------------------------------------------------
# O1: alert attached while the ticket is being created
# ---------------------------------------------------------------------------

INVENTORY = _alert("0.inv", "ata-inventory-5xx", "ata-inventory-api")
ORDERS = _alert("0.ord", "ata-orders-5xx", "ata-orders-api")
LATE = _alert("0.late", "ata-orders-latency", "ata-orders-api", signal="latency")


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
    state = {"now": 1793079864.0}  # 2026-10-27T05:44:24Z
    set_clock_func(lambda: state["now"])
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield state
    set_clock_func(None)
    set_timer_scheduler_hook(None)
    app.dependency_overrides.pop(get_agent_settings, None)


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


async def _primary_only_investigation(primary_alert, group_id, filename, settings):
    return _report(
        AgentReport,
        group_id=group_id,
        filename=filename,
        alerts=[format_okf_alert_entry(primary_alert, valid_ts=primary_alert.started_at)],
    )


def _wake(client, store, group_id, clock):
    doc = store.read(group_id)
    deadline = doc.settle_deadline_epoch()
    clock["now"] = deadline
    return client.post(
        "/timer/wake",
        json=TimerWakeRequest(group_id=group_id, deadline_type="settle", scheduled_epoch=int(deadline)).model_dump(mode="json"),
    )


def test_alert_attached_during_ticket_create_gets_late_alert_work_note(agent_cfg, monkeypatch, clock):
    notes = []
    created = []

    def _create_ticket(self, bridge_url, request, config=None, http_client=None):
        created.append(request)
        # The late alert attaches while the bridge create call is in flight (no sys_id yet).
        GroupingManager(config=agent_cfg).ingest_alert(alert=_event(LATE), current_time=clock["now"])
        return TicketResponse(status="created", sys_id="sys_9", number="INC0010999", url="https://sn/x", group_id=request.group_id)

    def _append_work_note(self, bridge_url, request, config=None, http_client=None):
        notes.append(request)

    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", _create_ticket)
    monkeypatch.setattr(BridgeClient, "append_work_note", _append_work_note)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _primary_only_investigation)
    client = TestClient(app, raise_server_exceptions=False)
    store = OKFStore(config=agent_cfg)

    group_id = client.post("/triage", json=_event(INVENTORY).model_dump(mode="json")).json()["group_id"]
    clock["now"] += 1
    assert client.post("/triage", json=_event(ORDERS).model_dump(mode="json")).json()["group_id"] == group_id

    assert _wake(client, store, group_id, clock).json()["status"] == "executed"

    assert {a["incident_id"] for a in created[0].report.alerts} == {"0.inv", "0.ord"}
    assert "0.late" in {a["incident_id"] for a in store.read(group_id).alerts}
    late_notes = [n for n in notes if "ata-orders-latency" in n.work_note]
    assert len(late_notes) == 1
    assert late_notes[0].sys_id == "sys_9"
    # Alerts already listed in the ticket get no extra note.
    assert not [n for n in notes if "ata-orders-5xx" in n.work_note]


def test_no_late_alert_work_note_when_ticket_lists_every_alert(agent_cfg, monkeypatch, clock):
    notes = []

    def _create_ticket(self, bridge_url, request, config=None, http_client=None):
        return TicketResponse(status="created", sys_id="sys_9", number="INC0010999", url="https://sn/x", group_id=request.group_id)

    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", _create_ticket)
    monkeypatch.setattr(BridgeClient, "append_work_note", lambda self, bridge_url, request, **kw: notes.append(request))
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _primary_only_investigation)
    client = TestClient(app, raise_server_exceptions=False)
    store = OKFStore(config=agent_cfg)

    group_id = client.post("/triage", json=_event(INVENTORY).model_dump(mode="json")).json()["group_id"]
    clock["now"] += 1
    client.post("/triage", json=_event(ORDERS).model_dump(mode="json"))

    assert _wake(client, store, group_id, clock).json()["status"] == "executed"
    assert notes == []
