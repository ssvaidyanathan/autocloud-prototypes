"""F6: ServiceNow urgency/impact so the default priority matrix shows the title's priority."""

import pytest

from agent.app.contracts import instructions as agent_instr
from bridge.app.contracts import instructions as bridge_instr

# ServiceNow default dl_u_priority matrix, as read (read-only) from the PDI on 2026-10-28.
DEFAULT_MATRIX = {
    ("1", "1"): 1, ("1", "2"): 2, ("1", "3"): 3,
    ("2", "1"): 2, ("2", "2"): 3, ("2", "3"): 4,
    ("3", "1"): 3, ("3", "2"): 4, ("3", "3"): 4,
}


@pytest.mark.parametrize("mod", [agent_instr, bridge_instr])
@pytest.mark.parametrize("prio,expected", [("P1", 1), ("P2", 2), ("P3", 3), ("P4", 4), ("p2", 2)])
def test_title_priority_equals_servicenow_priority(mod, prio, expected):
    assert DEFAULT_MATRIX[mod.servicenow_urgency_impact(prio)] == expected


def test_p2_is_urgency_1_impact_2():
    assert agent_instr.servicenow_urgency_impact("P2") == ("1", "2")


@pytest.mark.parametrize("mod", [agent_instr, bridge_instr])
@pytest.mark.parametrize("prio,expected", [
    ("P0", ("1", "1")),   # more severe than P1: clamped up to P1, never de-escalated
    ("p0", ("1", "1")),
    ("P5", ("2", "3")),   # below P4: clamped to P4
    ("P99", ("2", "3")),
    ("high", ("1", "2")),  # not 'P<n>': the report default P2
    ("", ("1", "2")),
    (None, ("1", "2")),
])
def test_priority_outside_mapping_is_clamped_with_warning(mod, prio, expected, caplog):
    import logging
    with caplog.at_level(logging.WARNING):
        assert mod.servicenow_urgency_impact(prio) == expected
    assert any(r.levelno == logging.WARNING and repr(prio) in r.getMessage() for r in caplog.records)


def test_p0_is_never_below_p1():
    assert agent_instr.servicenow_urgency_impact("P0") == agent_instr.servicenow_urgency_impact("P1")


def test_mapping_identical_in_agent_and_bridge():
    assert agent_instr.SERVICENOW_URGENCY_IMPACT == bridge_instr.SERVICENOW_URGENCY_IMPACT


@pytest.mark.parametrize("prio,urgency,impact", [
    ("P1", "1", "1"), ("P2", "1", "2"),
    ("P0", "1", "1"), ("P5", "2", "3"), ("garbage", "1", "2"),  # R1: the ticket is still created
])
def test_create_incident_sends_the_mapping(prio, urgency, impact):
    import json

    import httpx

    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig
    from bridge.app.contracts.report import InvestigationReport, ProbableRootCause

    created = {}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET" and "/api/now/table/sys_user" in url:
            return httpx.Response(200, json={"result": [{"sys_id": "u1"}]})
        if request.method == "POST" and url.endswith("/api/now/table/incident"):
            created.update(json.loads(request.read()))
            return httpx.Response(201, json={"result": {"sys_id": "s1", "number": "INC1", "state": "1"}})
        if request.method == "POST" and "/api/now/attachment/file" in url:
            return httpx.Response(201, json={"result": {"sys_id": "a1"}})
        return httpx.Response(200, json={"result": {"sys_id": "s1", "state": "2"}})

    report = InvestigationReport(
        group_id="g", summary="Rate limit exceeded", observations="o",
        probable_root_cause=ProbableRootCause(text="x", confidence=0.8),
        origin="ata-orders-v1", blast_radius=["ata-orders-v1"], priority=prio,
    )
    cfg = BridgeConfig(project_id="p", name_prefix="ata", servicenow_instance_url="https://fake.service-now.com",
                       servicenow_username="u", servicenow_password="pw", incidents_bucket="b", is_local=True)
    ServiceNowAdapter(config=cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler))).create_incident(report)
    assert (created["urgency"], created["impact"]) == (urgency, impact)


def test_p0_escalation_work_note_does_not_raise():
    """R1: a late 'p0' alert escalates the ticket; the agent's work note states urgency 1 / impact 1."""
    from agent.app.grouping.late_alerts import TicketRefresh, _work_note

    plan = TicketRefresh(
        sys_id="s1", group_id="g", ticket_number="INC1", new_alert_ids=["0.p0"],
        previous_short_description="[P2] x", previous_title_reconstructed=False, short_description="[P0] x",
        previous_priority="P2", priority="P0", escalated=True, title_problem="x", origin="o", alert_count=2,
        escalating_alert="0.p0",
    )
    note = _work_note(plan, [{"incident_id": "0.p0", "severity": "p0", "policy_name": "pol", "resource_name": "r"}])
    assert "urgency 1, impact 1 set" in note


def test_p0_escalation_patch_sends_p1_values():
    """R1: the bridge escalation PATCH for P0 sends urgency 1 / impact 1 instead of failing."""
    import json

    import httpx

    from bridge.app.adapters.servicenow import ServiceNowAdapter
    from bridge.app.config import BridgeConfig

    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.read()))
        return httpx.Response(200, json={"result": {"sys_id": "s1"}})

    cfg = BridgeConfig(project_id="p", name_prefix="ata", servicenow_instance_url="https://fake.service-now.com",
                       servicenow_username="u", servicenow_password="pw", incidents_bucket="b", is_local=True)
    adapter = ServiceNowAdapter(config=cfg, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    adapter.update_ticket_fields("s1", "[P0] x", "note", priority="P0",
                                   client=httpx.Client(transport=httpx.MockTransport(handler)))
    assert (sent["urgency"], sent["impact"]) == ("1", "1")
