"""Regression tests for INC0010319 (S1): wrong alert count and over-long ServiceNow title.

Live ticket title was:
  "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (76.3%) on ata-inventory-api impacting
   upstream consumers ata-inventory-v1, ata-orders-api, a... (1 alert)"
while the incident group held TWO alerts (ata-inventory-5xx, ata-orders-5xx). Expected:
  "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)"

Root causes covered here:
1. The investigation (and the fallback report) is built from the group's primary alert only,
   so report.alerts / observations held one alert; the ticket was created from that report.
2. The title used the synthesized problem sentence and truncated it mid-word.
3. The work note rendered Observations as one long ' | '-joined line.
"""

from pathlib import Path

import pytest
from starlette.testclient import TestClient

from agent.app import main as agent_main
from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import TicketResponse
from agent.app.contracts.report import (
    TITLE_PROBLEM_MAX_LENGTH,
    InvestigationReport as AgentReport,
)
from agent.app.contracts.timers import TimerWakeRequest
from agent.app.grouping.models import format_okf_alert_entry
from agent.app.grouping.store import OKFStore
from agent.app.main import app, set_clock_func
from agent.app.specialists.base import EvidenceDossier
from agent.app.specialists.synthesizer import SynthesizerAgent, apply_group_alerts
from agent.app.timers.scheduler import set_timer_scheduler_hook
from bridge.app.contracts.report import InvestigationReport as BridgeReport

S1_EXPECTED_TITLE = "[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)"

# The live synthesized problem sentence of INC0010319.
S1_SUMMARY = (
    "Elevated HTTP 5xx error rate (76.3%) on ata-inventory-api impacting upstream consumers "
    "(ata-inventory-v1, ata-orders-api, ata-orders-v1) since 2026-10-27T05:44:10Z"
)

# The live observations of INC0010319 (primary alert only).
S1_OBSERVATIONS = (
    "Cloud Monitoring policy 'ata-inventory-5xx' triggered on 'ata-inventory-api' at 2026-10-27T05:44:10Z (state: open)"
    " | HTTP 5xx error rate reached 76.3% (42 rpm) on ata-inventory-api; container restarts: N/A (not captured)"
    " | Database active connections normal (3 of 25) on ata-inventory-db"
    ' | Log excerpt (ata-inventory-api): [2026-10-27T05:44:42Z] ERROR - INFO: [REDACTED_IP]:35192 - "POST /items/reserve HTTP/1.1" 500 Internal Server Error'
    " | No configuration or revision deployments detected in the 2-hour window prior to alert onset."
)

S1_BLAST = ["ata-inventory-api", "ata-inventory-v1", "ata-orders-api", "ata-orders-v1"]


def _labels(component: str) -> dict:
    return {"app": "alert-triage-agent", "component": component, "severity": "p1", "signal": "5xx", "tier": "service"}


def _s1_alert_entry(incident_id: str, policy: str, component: str) -> dict:
    return {
        "incident_id": incident_id,
        "policy": policy,
        "policy_name": policy,
        "resource": component,
        "resource_name": component,
        "component": component,
        "signal": "5xx",
        "state": "open",
        "started_at": "2026-10-27T05:44:10Z",
        "labels": _labels(component),
        "policy_user_labels": _labels(component),
    }


INVENTORY_ALERT = _s1_alert_entry("0.oeb1u1lfyho2", "ata-inventory-5xx", "ata-inventory-api")
ORDERS_ALERT = _s1_alert_entry("0.oeb1u1lfyho7", "ata-orders-5xx", "ata-orders-api")


def _s1_report(cls=AgentReport, alerts=None, **overrides):
    fields = dict(
        group_id="ata-inventory-5xx-ata-inventory-api-20261027t054424z",
        summary=S1_SUMMARY,
        observations=S1_OBSERVATIONS,
        origin="ata-inventory-api",
        blast_radius=S1_BLAST,
        priority="P1",
        alerts=[dict(a) for a in (alerts if alerts is not None else [INVENTORY_ALERT, ORDERS_ALERT])],
    )
    fields.update(overrides)
    return cls(**fields)


# ---------------------------------------------------------------------------
# 1. Title format (report contract, agent and bridge copies)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_s1_title_uses_short_problem_and_group_alert_count(ReportCls):
    report = _s1_report(ReportCls)
    assert report.build_short_description(max_length=160) == S1_EXPECTED_TITLE


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_s1_title_with_synthesizer_title_problem(ReportCls):
    report = _s1_report(ReportCls, title_problem="Elevated HTTP 5xx error rate")
    assert report.build_short_description() == S1_EXPECTED_TITLE


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
@pytest.mark.parametrize("n,suffix", [(1, "(1 alert)"), (2, "(2 alerts)"), (7, "(7 alerts)")])
def test_title_count_pluralization(ReportCls, n, suffix):
    alerts = [dict(INVENTORY_ALERT, incident_id=f"0.a{i}") for i in range(n)]
    title = _s1_report(ReportCls, alerts=alerts).build_short_description()
    assert title.endswith(f" {suffix}")


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_title_counts_alerts_by_incident_id(ReportCls):
    """The same alert listed twice is one alert."""
    report = _s1_report(ReportCls, alerts=[INVENTORY_ALERT, INVENTORY_ALERT, ORDERS_ALERT])
    assert report.build_short_description().endswith("(2 alerts)")


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_title_never_contains_the_synthesized_sentence(ReportCls):
    title = _s1_report(ReportCls).build_short_description()
    assert "76.3%" not in title
    assert "upstream consumers" not in title
    assert "..." not in title


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
@pytest.mark.parametrize("max_len", [40, 50, 60, 80, 160])
def test_title_keeps_count_and_cuts_on_word_boundary(ReportCls, max_len):
    report = _s1_report(ReportCls, title_problem="Elevated upstream dependency latency on gateway")
    title = report.build_short_description(max_length=max_len)
    assert len(title) <= max_len
    assert title.endswith("(2 alerts)")
    assert title.startswith("[P1] ata-inventory-api")
    if ": " in title:
        problem = title.split(": ", 1)[1].rsplit(" (", 1)[0]
        assert "Elevated upstream dependency latency on gateway".startswith(problem)
        rest = "Elevated upstream dependency latency on gateway"[len(problem):]
        assert rest == "" or rest.startswith(" "), f"mid-word cut: {title!r}"


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_title_keeps_count_with_very_long_origin(ReportCls):
    report = _s1_report(ReportCls, origin="ata-" + "x" * 200)
    title = report.build_short_description(max_length=160)
    assert len(title) <= 160
    assert title.endswith(" (2 alerts)")


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_title_problem_over_limit_is_dropped_and_signal_used(ReportCls):
    report = _s1_report(ReportCls, title_problem="x " * TITLE_PROBLEM_MAX_LENGTH)
    assert report.title_problem is None
    assert report.build_short_description() == S1_EXPECTED_TITLE


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
@pytest.mark.parametrize("value", ["", "   ", "unknown", None])
def test_title_problem_empty_values_are_none(ReportCls, value):
    assert _s1_report(ReportCls, title_problem=value).title_problem is None


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_title_signal_from_policy_name_when_labels_missing(ReportCls):
    alerts = [{"incident_id": "0.a1", "policy_name": "ata-orders-latency", "resource_name": "ata-orders-api"}]
    report = _s1_report(ReportCls, origin="ata-orders-api", priority="P2", alerts=alerts)
    assert report.build_short_description() == "[P2] ata-orders-api: Elevated latency (1 alert)"


# ---------------------------------------------------------------------------
# 2. Synthesizer: short title phrase and group alerts in the report
# ---------------------------------------------------------------------------

def _inventory_alert_event() -> AlertEvent:
    return AlertEvent(
        incident_id="0.oeb1u1lfyho2",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-27T05:44:10Z",
        policy_user_labels=_labels("ata-inventory-api"),
        labels=_labels("ata-inventory-api"),
    )


def test_synthesizer_title_problem_for_5xx_alert():
    dossiers = [EvidenceDossier(specialist_name="CloudRunSpecialist", target_resource="ata-inventory-api")]
    phrase = SynthesizerAgent()._synthesize_title_problem(_inventory_alert_event(), dossiers, "ata-inventory-api")
    assert phrase == "Elevated HTTP 5xx error rate"
    assert len(phrase) <= TITLE_PROBLEM_MAX_LENGTH


def test_apply_group_alerts_adds_correlated_alert_to_report():
    report = _s1_report(alerts=[INVENTORY_ALERT])
    apply_group_alerts(report, [INVENTORY_ALERT, ORDERS_ALERT])

    assert [a["incident_id"] for a in report.alerts] == ["0.oeb1u1lfyho2", "0.oeb1u1lfyho7"]
    assert "Correlated alert: Cloud Monitoring policy 'ata-orders-5xx' triggered on 'ata-orders-api'" in report.observations
    # Directly after the primary alert's trigger line.
    assert report.observations.split(" | ")[1].startswith("Correlated alert: Cloud Monitoring policy 'ata-orders-5xx'")
    assert report.build_short_description() == S1_EXPECTED_TITLE
    assert "ata-orders-5xx | ata-orders-api | 0.oeb1u1lfyho7" in report.build_structured_description(project_id="test-project", incidents_bucket="test-project-ata-incidents")


def test_apply_group_alerts_is_idempotent():
    report = _s1_report(alerts=[INVENTORY_ALERT])
    apply_group_alerts(report, [INVENTORY_ALERT, ORDERS_ALERT])
    once = (list(report.alerts), report.observations)
    apply_group_alerts(report, [INVENTORY_ALERT, ORDERS_ALERT])
    assert (list(report.alerts), report.observations) == once
    assert report.observations.count("ata-orders-5xx") == 1


def test_apply_group_alerts_with_no_group_alerts_keeps_report():
    report = _s1_report(alerts=[INVENTORY_ALERT])
    apply_group_alerts(report, [])
    assert len(report.alerts) == 1
    assert report.observations == S1_OBSERVATIONS


# ---------------------------------------------------------------------------
# 3. Ticket time: /timer/wake sends a report covering every alert of the group
# ---------------------------------------------------------------------------

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


def _alert_payload(entry: dict) -> dict:
    return AlertEvent(
        incident_id=entry["incident_id"],
        policy_name=entry["policy_name"],
        resource_name=entry["resource_name"],
        resource_type="cloud_run_revision",
        state="open",
        started_at=entry["started_at"],
        policy_user_labels=entry["labels"],
        labels=entry["labels"],
    ).model_dump(mode="json")


async def _primary_only_investigation(primary_alert, group_id, filename, settings, group_alerts=None):
    """Shaped like the real investigation: built from the primary alert only."""
    return _s1_report(
        alerts=[format_okf_alert_entry(primary_alert, valid_ts=primary_alert.started_at)],
        group_id=group_id,
        filename=filename,
    )


@pytest.mark.parametrize("deadline_type", ["settle", "max_wait"])
def test_s1_ticket_report_covers_all_group_alerts(agent_cfg, monkeypatch, clock, deadline_type):
    sent = []

    def _create_ticket(self, bridge_url, request, config=None, http_client=None):
        sent.append(request)
        return TicketResponse(status="created", sys_id="sys_1", number="INC0010319", url="https://sn/x", group_id=request.group_id)

    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    monkeypatch.setattr(BridgeClient, "create_ticket", _create_ticket)
    monkeypatch.setattr(agent_main, "execute_multi_agent_investigation", _primary_only_investigation)
    client = TestClient(app, raise_server_exceptions=False)
    store = OKFStore(config=agent_cfg)

    first = client.post("/triage", json=_alert_payload(INVENTORY_ALERT))
    assert first.status_code == 200
    group_id = first.json()["group_id"]
    clock["now"] += 1  # orders alert arrives ~1s later, as in S1
    second = client.post("/triage", json=_alert_payload(ORDERS_ALERT))
    assert second.json()["group_id"] == group_id
    doc = store.read(group_id)
    assert len(doc.alerts) == 2

    deadline = doc.settle_deadline_epoch() if deadline_type == "settle" else doc.max_wait_deadline_epoch()
    clock["now"] = deadline
    res = client.post(
        "/timer/wake",
        json=TimerWakeRequest(group_id=group_id, deadline_type=deadline_type, scheduled_epoch=int(deadline)).model_dump(mode="json"),
    )
    assert res.json()["status"] == "executed"

    assert len(sent) == 1
    report = sent[0].report
    assert {a["incident_id"] for a in report.alerts} == {"0.oeb1u1lfyho2", "0.oeb1u1lfyho7"}
    assert "ata-orders-5xx" in report.observations
    # The bridge builds the title from the report it receives.
    bridge_report = BridgeReport.model_validate(report.model_dump(mode="json"))
    assert bridge_report.build_short_description(max_length=160) == S1_EXPECTED_TITLE
    assert "ata-orders-5xx" in bridge_report.build_executive_summary_html()


def test_synthesizer_okf_update_merges_group_alerts_into_report(agent_cfg, monkeypatch, clock):
    """The OKF report section (and the report it returns) lists the correlated alert too."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app, raise_server_exceptions=False)
    store = OKFStore(config=agent_cfg)
    group_id = client.post("/triage", json=_alert_payload(INVENTORY_ALERT)).json()["group_id"]
    clock["now"] += 1
    client.post("/triage", json=_alert_payload(ORDERS_ALERT))
    filename = f"incident-{group_id}.md"

    report = _s1_report(alerts=[dict(INVENTORY_ALERT)], group_id=group_id, filename=filename)
    SynthesizerAgent()._update_okf_file(report, _inventory_alert_event(), agent_cfg, store=store)

    assert {a["incident_id"] for a in report.alerts} == {"0.oeb1u1lfyho2", "0.oeb1u1lfyho7"}
    body = store.read(group_id).body
    assert "Correlated alert: Cloud Monitoring policy 'ata-orders-5xx'" in body


# ---------------------------------------------------------------------------
# 4. Work note: Observations and Systems as sub-bullets
# ---------------------------------------------------------------------------

def _observations_block(note: str) -> str:
    return note.split("<b>Observations:</b>", 1)[1].split("<b>Recommended Actions:</b>", 1)[0]


def _sub_items(block: str) -> list[str]:
    return [ln.strip() for ln in block.splitlines() if ln.strip().startswith("<li>") and ln.strip().endswith("</li>")]


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_work_note_observations_are_sub_bullets(ReportCls):
    report = _s1_report(ReportCls, alerts=[INVENTORY_ALERT])
    apply_group_alerts(report, [INVENTORY_ALERT, ORDERS_ALERT])
    note = report.build_executive_summary_html()
    block = _observations_block(note)

    assert " | " not in block
    items = _sub_items(block)
    assert len(items) == 6  # 5 live observations + the correlated ata-orders-5xx alert
    assert items[0] == (
        "<li>Cloud Monitoring policy 'ata-inventory-5xx' triggered on 'ata-inventory-api' "
        "at 2026-10-27T05:44:10Z (state: open)</li>"
    )
    assert items[1].startswith("<li>Correlated alert: Cloud Monitoring policy 'ata-orders-5xx'")
    assert any("No configuration or revision deployments" in i for i in items)
    assert "<ul>" in block and "</ul>" in block


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_work_note_observations_capped_with_more_marker(ReportCls):
    obs = " | ".join(f"Observation number {i} on ata-inventory-api" for i in range(1, 10))
    note = _s1_report(ReportCls, observations=obs).build_executive_summary_html()
    items = _sub_items(_observations_block(note))
    assert len(items) == 7
    assert items[5] == "<li>Observation number 6 on ata-inventory-api</li>"
    assert items[6] == "<li>...and 3 more (see attached report)</li>"


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_work_note_systems_are_sub_bullets(ReportCls):
    note = _s1_report(ReportCls).build_executive_summary_html()
    block = note.split("<b>Systems:</b>", 1)[1].split("<b>Observations:</b>", 1)[0]
    assert " | " not in block
    assert "<li>Origin: <code>ata-inventory-api</code>" in block
    assert "<li>Component: <code>ata-inventory-api</code>, Signal: <code>5xx</code></li>" in block
    assert f"<li>Blast Radius: {', '.join(S1_BLAST)}</li>" in block


@pytest.mark.parametrize("ReportCls", [AgentReport, BridgeReport])
def test_work_note_escapes_html_in_observations(ReportCls):
    note = _s1_report(ReportCls, observations="Log excerpt: <script>x</script> & more").build_executive_summary_html()
    assert "&lt;script&gt;" in note
    assert "<script>" not in note
