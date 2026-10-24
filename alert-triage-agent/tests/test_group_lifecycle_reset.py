"""Tests covering group resolution, archiving, stale group prevention, and reset endpoints.

Addresses F2: STALE GROUP REUSE (lifecycle & reset cleanups).
Ensures:
1. When underlying Monitoring incidents close, the group moves to RESOLVED and is archived.
2. An alert arriving after group resolution initializes a fresh new group.
3. Reset (via state machine or /windowing/reset endpoint) resolves and archives all active groups,
   leaving zero active groups.
4. S5 scenario alerts on the same resource/policy do not reuse S4's stale group after reset.
"""

import httpx
import pytest
from starlette.testclient import TestClient

from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.contracts.alert import AlertEvent
from bridge.app.contracts.report import InvestigationReport
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.core.windowing import IncidentGroup, IncidentGroupStatus, WindowingStateMachine
from bridge.app.main import app as bridge_app, set_dispatcher, set_servicenow_adapter


@pytest.fixture
def test_config():
    config = BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        webhook_token="test-secret-token",
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        is_local=True,
    )
    reset_settings(config)
    yield config
    reset_settings(None)


@pytest.fixture
def mock_adapter(test_config):
    posted_notes = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/now/table/incident" in url_str:
            if request.method == "POST":
                return httpx.Response(201, json={"result": {"sys_id": "sn-test-sys-1", "number": "INC001999"}})
            elif request.method == "PATCH":
                posted_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-test-sys-1"}})
        if "/api/now/attachment/file" in url_str:
            return httpx.Response(201, json={"result": {"sys_id": "att-test-1"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    adapter._posted_notes = posted_notes  # type: ignore[attr-defined]
    return adapter


def test_group_resolution_and_archiving_when_incidents_close(test_config, mock_adapter):
    """When all underlying Monitoring incidents close, group moves to RESOLVED and is archived."""
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
    )

    t0 = 1000.0
    alert1 = AlertEvent(
        incident_id="inc-ord-1",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-23T05:00:00Z",
    )
    alert2 = AlertEvent(
        incident_id="inc-ord-2",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-23T05:01:00Z",
    )

    group, is_dup = wm.ingest_alert(alert1, current_time=t0)
    assert not is_dup
    assert group is not None
    assert group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert not group.archived

    wm.ingest_alert(alert2, current_time=t0 + 10.0)
    assert len(group.alerts) == 2

    # Simulate ticketing at settle stabilization
    group.status = IncidentGroupStatus.INVESTIGATING
    report = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Orders 5xx spike",
        observations="Pods crashing",
        recommended_actions=["Rollback"],
        origin="ata-orders-api",
        blast_radius=["ata-orders-api"],
        priority="P1",
    )
    wm.handle_investigation_findings(report)
    assert group.status == IncidentGroupStatus.TICKETED
    assert group.servicenow_sys_id == "sn-test-sys-1"

    # Close alert 1: group still has alert 2 open, so not yet resolved
    close_alert1 = AlertEvent(
        incident_id="inc-ord-1",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        state="closed",
    )
    wm.ingest_alert(close_alert1, current_time=t0 + 50.0)
    assert group.status == IncidentGroupStatus.TICKETED
    assert not group.archived
    assert len(wm.list_groups()) == 1

    # Close alert 2: all alerts now closed -> group enters COOLING_DOWN linger window
    close_alert2 = AlertEvent(
        incident_id="inc-ord-2",
        policy_name="ata-orders-5xx",
        resource_name="ata-orders-api",
        state="closed",
    )
    wm.ingest_alert(close_alert2, current_time=t0 + 60.0)
    assert group.status == IncidentGroupStatus.COOLING_DOWN

    # Advance past 180s cool-down deadline to resolve and archive
    wm.check_deadlines(current_time=t0 + 60.0 + 185.0)
    assert group.status == IncidentGroupStatus.RESOLVED
    assert group.archived is True
    assert group.archived_at == t0 + 60.0 + 185.0
    assert group.resolved_at == t0 + 60.0 + 185.0

    # Group must be in archived_groups and NOT in active groups
    assert group.group_id in wm.archived_groups
    assert group.group_id not in wm.groups
    assert len(wm.list_groups()) == 0
    assert len(wm.list_archived_groups()) == 1


def test_post_resolution_alert_initializes_fresh_new_group(test_config, mock_adapter):
    """An alert arriving after resolution must start a fresh new group, never joining the resolved group."""
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
    )

    t0 = 2000.0
    alert_initial = AlertEvent(
        incident_id="inc-init-1",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:16:00Z",
    )
    group1, _ = wm.ingest_alert(alert_initial, current_time=t0)
    assert group1 is not None

    # Resolve alert -> enter COOLING_DOWN
    alert_close = AlertEvent(
        incident_id="inc-init-1",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        state="closed",
    )
    wm.ingest_alert(alert_close, current_time=t0 + 100.0)
    assert group1.status == IncidentGroupStatus.COOLING_DOWN

    # Advance past 180s cool-down deadline to resolve and archive
    wm.check_deadlines(current_time=t0 + 100.0 + 185.0)
    assert group1.status == IncidentGroupStatus.RESOLVED
    assert group1.archived is True
    assert len(wm.list_groups()) == 0

    # New alert arrives on the EXACT same policy and resource after resolution
    t1 = t0 + 300.0
    alert_new = AlertEvent(
        incident_id="inc-new-2",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:25:00Z",
    )
    group2, is_dup = wm.ingest_alert(alert_new, current_time=t1)

    assert not is_dup
    assert group2 is not None
    # Must NOT reuse group1
    assert group2.group_id != group1.group_id
    assert group2.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert not group2.archived
    assert len(wm.list_groups()) == 1
    assert len(wm.list_archived_groups()) == 1


def test_reset_resolves_and_archives_all_active_groups(test_config, mock_adapter):
    """Calling reset() resolves and archives all active groups and clears deduplication."""
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
    )

    # Ingest 2 distinct groups
    alert1 = AlertEvent(
        incident_id="inc-active-1",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T05:10:00Z",
    )
    alert2 = AlertEvent(
        incident_id="inc-active-2",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:12:00Z",
    )

    g1, _ = wm.ingest_alert(alert1, current_time=100.0)
    g2, _ = wm.ingest_alert(alert2, current_time=110.0)

    # Ticket g2 upon stabilization
    g2.status = IncidentGroupStatus.INVESTIGATING
    report2 = InvestigationReport(
        group_id=g2.group_id,
        filename=g2.filename,
        summary="Apigee 429 spike",
        observations="Rate limit reached",
        recommended_actions=["Increase rate"],
        origin="ata-orders-v1",
        blast_radius=["ata-orders-v1"],
        priority="P2",
    )
    wm.handle_investigation_findings(report2)

    assert len(wm.list_groups()) == 2
    assert g1.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert g2.status == IncidentGroupStatus.TICKETED

    # Execute reset
    reset_res = wm.reset(current_time=200.0)
    assert reset_res["status"] == "ok"
    assert reset_res["resolved_count"] == 2
    assert reset_res["active_groups"] == 0

    # Invariants after reset
    assert len(wm.list_groups()) == 0
    assert len(wm.list_archived_groups()) == 2
    assert g1.status == IncidentGroupStatus.RESOLVED
    assert g1.archived is True
    assert g2.status == IncidentGroupStatus.RESOLVED
    assert g2.archived is True
    assert len(wm.seen_alerts) == 0

    # Ensure resolution work note was appended to the ticketed group
    assert any("resolved" in str(req.content).lower() for req in mock_adapter._posted_notes)


def test_stale_group_reuse_prevention_simulation(test_config, mock_adapter):
    """Simulate alert -> reset -> subsequent alert lifecycle.

    First alert fires on ata-orders-v1.
    Group is ticketed.
    Scenario reset is performed.
    Subsequent alert fires on ata-orders-v1.
    Must create a brand new group with a new group_id and fresh lifecycle.
    """
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
    )

    # --- Stage 1: S4 Scenario ---
    s4_time = 1729660571.0  # 2026-10-23T05:16:11Z
    s4_alert = AlertEvent(
        incident_id="0.oe5hn9p0otpe",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:16:11Z",
    )
    s4_group, is_dup = wm.ingest_alert(s4_alert, current_time=s4_time)
    assert not is_dup
    assert s4_group is not None
    s4_group_id = s4_group.group_id

    # S4 finishes investigation & ticketing upon stabilization
    s4_group.status = IncidentGroupStatus.INVESTIGATING
    s4_report = InvestigationReport(
        group_id=s4_group_id,
        filename=s4_group.filename,
        summary="Apigee SpikeArrest 1pm throttled orders",
        observations="1pm policy enforced",
        recommended_actions=["Deploy rate 300pm"],
        origin="ata-orders-v1",
        blast_radius=["ata-orders-v1"],
        priority="P2",
    )
    wm.handle_investigation_findings(s4_report)
    assert s4_group.status == IncidentGroupStatus.TICKETED
    assert s4_group.servicenow_sys_id == "sn-test-sys-1"

    # --- Stage 2: Scenario Reset (scripts/reset-scenarios.sh) ---
    reset_summary = wm.reset(current_time=s4_time + 100.0)
    assert reset_summary["resolved_count"] == 1
    assert reset_summary["active_groups"] == 0
    assert len(wm.list_groups()) == 0
    assert s4_group.status == IncidentGroupStatus.RESOLVED
    assert s4_group.archived is True

    # --- Stage 3: S5 Scenario (Concurrent Disjoint) ---
    s5_time = 1729661234.0  # 2026-10-23T05:27:14Z
    # S5 Group 2 alert on the same resource ata-orders-v1
    s5_alert = AlertEvent(
        incident_id="0.oe5hwcqe3x3w",
        policy_name="ata-apigee-orders-429",
        resource_name="ata-orders-v1",
        started_at="2026-10-23T05:27:14Z",
    )
    s5_group, is_s5_dup = wm.ingest_alert(s5_alert, current_time=s5_time)

    assert not is_s5_dup
    assert s5_group is not None
    # CRITICAL: S5 must NOT reuse S4's group ID or ticket!
    assert s5_group.group_id != s4_group_id
    assert s5_group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert s5_group.servicenow_sys_id is None  # Fresh group has no stale ticket
    assert not s5_group.archived
    assert len(wm.list_groups()) == 1


def test_bridge_reset_and_archive_endpoints(test_config, mock_adapter):
    """Test that deleted bridge windowing and reset HTTP endpoints return 404 Not Found."""
    client = TestClient(bridge_app)
    auth_headers = {"Authorization": "Bearer valid-agent-token"}

    # Verify that deleted open and windowing routes return 404
    assert client.get("/windowing/groups").status_code == 404
    assert client.post("/windowing/groups/grp-test-1/archive").status_code == 404
    assert client.post("/windowing/groups/grp-test-1/archive", headers=auth_headers).status_code == 404
    assert client.post("/windowing/groups/grp-test-1/resolve").status_code == 404
    assert client.post("/windowing/groups/grp-test-1/resolve", headers=auth_headers).status_code == 404
    assert client.post("/windowing/reset").status_code == 404
    assert client.post("/windowing/reset", headers=auth_headers).status_code == 404
    assert client.post("/reset").status_code == 404
    assert client.post("/reset", headers=auth_headers).status_code == 404
