import time

import httpx
import pytest

from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig
from bridge.app.contracts.alert import AlertEvent
from bridge.app.contracts.report import InvestigationReport
from bridge.app.core.dispatcher import TaskDispatcher
from bridge.app.core.windowing import IncidentGroup, IncidentGroupStatus, WindowingStateMachine


@pytest.fixture
def test_config(tmp_path):
    store = tmp_path / "knowledge/incident-manager"
    store.mkdir(parents=True, exist_ok=True)
    return BridgeConfig(
        project_id="test-project",
        name_prefix="ata",
        servicenow_instance_url="https://fake.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        incident_store_path=str(store),
        is_local=True,
    )


@pytest.fixture
def mock_adapter(test_config):
    posted_notes = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "/api/now/table/incident" in str(request.url):
            if request.method == "POST":
                return httpx.Response(201, json={"result": {"sys_id": "sn-grp-123", "number": "INC009988"}})
            elif request.method == "PATCH":
                posted_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-grp-123"}})
        if "/api/now/attachment/file" in str(request.url):
            return httpx.Response(201, json={"result": {"sys_id": "att-123"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    adapter._posted_notes = posted_notes  # type: ignore[attr-defined]
    return adapter


def test_windowing_120s_settle_reset(test_config, mock_adapter):
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
        settle_duration=120.0,
        max_wait_duration=300.0,
    )

    t0 = 1000.0
    alert1 = AlertEvent(
        incident_id="alert-1",
        policy_name="ata-backend-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-22T21:00:00Z",
    )

    group, is_dup = wm.ingest_alert(alert1, current_time=t0)
    assert not is_dup
    assert group is not None
    assert group.created_at == t0
    assert group.settle_deadline == t0 + 120.0  # 1120.0
    assert group.max_wait_deadline == t0 + 300.0  # 1300.0
    assert group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS

    # 50 seconds later, another alert joins the group
    t1 = t0 + 50.0  # 1050.0
    alert2 = AlertEvent(
        incident_id="alert-2",
        policy_name="ata-backend-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-22T21:00:50Z",
    )

    group2, is_dup2 = wm.ingest_alert(alert2, current_time=t1)
    assert not is_dup2
    assert group2.group_id == group.group_id
    # Settle deadline must be reset to t1 + 120 = 1170.0
    assert group.settle_deadline == 1170.0
    # Max wait deadline remains t0 + 300 = 1300.0
    assert group.max_wait_deadline == 1300.0

    # At t = 1160 (110s after alert2), still within settle window
    stabilized = wm.check_deadlines(current_time=1160.0)
    assert len(stabilized) == 0
    assert group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS

    # At t = 1170.0 (120s quiet), settle period reached -> transitions to STABLE then INVESTIGATING
    stabilized = wm.check_deadlines(current_time=1170.0)
    assert len(stabilized) == 1
    assert stabilized[0].group_id == group.group_id
    assert group.status == IncidentGroupStatus.INVESTIGATING


def test_windowing_300s_max_wait_timeout(test_config, mock_adapter):
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
        settle_duration=120.0,
        max_wait_duration=300.0,
    )

    t0 = 2000.0
    alert1 = AlertEvent(
        incident_id="alert-flood-1",
        policy_name="ata-db-contention",
        resource_name="ata-inventory-db",
        started_at="2026-10-22T21:00:00Z",
    )
    group, _ = wm.ingest_alert(alert1, current_time=t0)

    # Alerts keep arriving every 70 seconds: t0+70, t0+140, t0+210, t0+280
    for i, offset in enumerate([70.0, 140.0, 210.0, 280.0], start=2):
        alert_i = AlertEvent(
            incident_id=f"alert-flood-{i}",
            policy_name="ata-db-contention",
            resource_name="ata-inventory-db",
            started_at="2026-10-22T21:00:00Z",
        )
        wm.ingest_alert(alert_i, current_time=t0 + offset)

    # At t = t0 + 295, settle deadline was pushed to (t0 + 280 + 120 = t0 + 400)
    assert group.settle_deadline == t0 + 400.0
    # But max wait deadline is strictly t0 + 300
    assert group.max_wait_deadline == t0 + 300.0

    # At t0 + 299, still not timed out
    assert len(wm.check_deadlines(current_time=t0 + 299.0)) == 0

    # At t0 + 300, max wait deadline enforces transition to STABLE -> INVESTIGATING
    stabilized = wm.check_deadlines(current_time=t0 + 300.0)
    assert len(stabilized) == 1
    assert group.status == IncidentGroupStatus.INVESTIGATING


def test_windowing_status_lifecycle_and_ticketed_late_alerts(test_config, mock_adapter):
    dispatcher = TaskDispatcher(config=test_config)
    wm = WindowingStateMachine(
        config=test_config,
        dispatcher=dispatcher,
        adapter=mock_adapter,
    )

    t0 = 5000.0
    alert = AlertEvent(
        incident_id="alert-lifecycle-1",
        policy_name="ata-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-22T21:00:00Z",
    )
    group, _ = wm.ingest_alert(alert, current_time=t0)
    assert group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS

    # Stabilize
    wm.check_deadlines(current_time=t0 + 125.0)
    assert group.status == IncidentGroupStatus.INVESTIGATING

    # Agent returns investigation findings -> transition to TICKETED
    report = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Inventory API high 5xx error rate",
        observations="Connection pools saturated",
        recommended_actions=["Restart connection pool"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
        priority="P1",
    )
    ref = wm.handle_investigation_findings(report)
    assert ref.sys_id == "sn-grp-123"
    assert group.status == IncidentGroupStatus.TICKETED
    assert group.servicenow_sys_id == "sn-grp-123"
    mock_adapter._posted_notes.clear()

    # Late correlated alert arrives while TICKETED -> updates blast radius and appends work note
    late_alert = AlertEvent(
        incident_id="alert-lifecycle-2",
        policy_name="ata-5xx",
        resource_name="ata-orders-api",
        started_at="2026-10-22T21:05:00Z",
    )
    wm.ingest_alert(late_alert, current_time=t0 + 200.0)
    assert "ata-orders-api" in group.blast_radius
    assert len(mock_adapter._posted_notes) == 1  # Late alert work note PATCH

    # Close alert 1
    close1 = AlertEvent(
        incident_id="alert-lifecycle-1",
        policy_name="ata-5xx",
        resource_name="ata-inventory-api",
        state="closed",
        started_at="2026-10-22T21:00:00Z",
    )
    wm.ingest_alert(close1, current_time=t0 + 300.0)
    assert group.status == IncidentGroupStatus.TICKETED  # Still 1 alert open

    # Close alert 2 -> all closed -> transitions to RESOLVED and is archived
    close2 = AlertEvent(
        incident_id="alert-lifecycle-2",
        policy_name="ata-5xx",
        resource_name="ata-orders-api",
        state="closed",
        started_at="2026-10-22T21:05:00Z",
    )
    wm.ingest_alert(close2, current_time=t0 + 310.0)
    assert group.status == IncidentGroupStatus.COOLING_DOWN

    # Cool-down expires after 180s linger
    wm.check_deadlines(current_time=t0 + 310.0 + 185.0)
    assert group.status == IncidentGroupStatus.RESOLVED
    assert group.archived is True
    assert group.archived_at == t0 + 310.0 + 185.0
    assert group.group_id in wm.archived_groups
    assert group.group_id not in wm.groups
    assert len(wm.list_groups()) == 0
    assert len(wm.list_archived_groups()) == 1
    assert len(mock_adapter._posted_notes) == 2  # Resolution note PATCH

    # New alert arriving after resolution initializes a fresh new group
    alert3 = AlertEvent(
        incident_id="alert-lifecycle-3",
        policy_name="ata-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-22T21:10:00Z",
    )
    new_group, is_dup3 = wm.ingest_alert(alert3, current_time=t0 + 400.0)
    assert not is_dup3
    assert new_group is not None
    assert new_group.group_id != group.group_id
    assert new_group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert not new_group.archived
    assert len(wm.list_groups()) == 1



def test_windowing_idempotency_duplicate_handling(test_config, mock_adapter):
    wm = WindowingStateMachine(config=test_config, adapter=mock_adapter)

    alert = AlertEvent(
        incident_id="alert-dedup-1",
        policy_name="ata-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-22T21:00:00Z",
    )

    # First ingestion
    group1, is_dup1 = wm.ingest_alert(alert, current_time=100.0)
    assert not is_dup1

    # Identical ingestion
    group2, is_dup2 = wm.ingest_alert(alert, current_time=105.0)
    assert is_dup2
    assert group1.group_id == group2.group_id


def test_n_callbacks_single_incident(test_config):
    """Assert N callbacks for one group create exactly 1 ServiceNow incident and later callbacks update."""
    created_incidents = []
    updated_work_notes = []
    attached_reports = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/now/table/sys_user" in url_str:
            return httpx.Response(200, json={"result": [{"sys_id": "usr-1", "user_name": "admin"}]})
        if "/api/now/table/incident" in url_str:
            if request.method == "POST":
                created_incidents.append(request)
                return httpx.Response(201, json={"result": {"sys_id": "sn-single-100", "number": "INC0010999", "state": "1"}})
            elif request.method == "PATCH":
                updated_work_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-single-100", "state": "2"}})
            elif request.method == "GET":
                return httpx.Response(200, json={"result": {"sys_id": "sn-single-100", "state": "2"}})
        if "/api/now/attachment/file" in url_str:
            attached_reports.append(request)
            return httpx.Response(201, json={"result": {"sys_id": "att-100"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    wm = WindowingStateMachine(config=test_config, adapter=adapter)

    # Setup group
    alert = AlertEvent(
        incident_id="alert-s1-1",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        started_at="2026-10-23T04:30:00Z",
    )
    group, _ = wm.ingest_alert(alert, current_time=1000.0)

    # 1. Preliminary findings callback
    r1 = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Preliminary: 5xx spike observed",
        observations="Error rate 85%",
        recommended_actions=["Check pool"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
    )
    ref1 = wm.handle_investigation_findings(r1)
    assert ref1 is None
    assert group.latest_report == r1
    assert group.status == IncidentGroupStatus.MITIGATION_IN_PROGRESS
    assert len(created_incidents) == 0

    # 2. Alert joining callback (orders alert joins group)
    r2 = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Updated: orders-api impacted by inventory 5xx",
        observations="Cascading 5xx errors",
        recommended_actions=["Restart inventory pool"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
    )
    ref2 = wm.handle_investigation_findings(r2)
    assert ref2 is None
    assert group.latest_report == r2
    assert len(created_incidents) == 0

    # 3. Settle tick callback: group stabilizes at quiet settle deadline
    stabilized = wm.check_deadlines(current_time=1130.0)
    assert len(stabilized) == 1
    assert group.status == IncidentGroupStatus.INVESTIGATING

    r3 = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Settle evaluation: stable at 2 alerts",
        observations="No new alerts during quiet window",
        recommended_actions=["Rollback revision"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
    )
    ref3 = wm.handle_investigation_findings(r3)
    assert ref3 is not None
    assert ref3.sys_id == "sn-single-100"
    assert ref3.number == "INC0010999"
    assert len(created_incidents) == 1  # Ticket created at settle stabilization

    # 4. Final findings callback
    r4 = InvestigationReport(
        group_id=group.group_id,
        filename=group.filename,
        summary="Final Report: root cause connection exhaustion",
        observations="Pool saturated",
        recommended_actions=["Terminate idle sessions"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
    )
    ref4 = wm.handle_investigation_findings(r4)
    assert ref4 is not None
    assert ref4.sys_id == "sn-single-100"
    assert ref4.number == "INC0010999"
    assert len(created_incidents) == 1  # STILL EXACTLY 1 INCIDENT CREATED

    # Ticket refs match
    assert ref3.sys_id == ref4.sys_id
    assert ref3.number == ref4.number


def test_concurrent_callbacks_race_safe(test_config):
    """Assert concurrent findings callbacks for the same group are race-safe and create 1 incident."""
    import concurrent.futures
    import time

    created_incidents = []
    updated_work_notes = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/now/table/sys_user" in url_str:
            return httpx.Response(200, json={"result": [{"sys_id": "usr-1", "user_name": "admin"}]})
        if "/api/now/table/incident" in url_str:
            if request.method == "POST":
                # Artificial delay to widen the race window if locks are missing
                time.sleep(0.04)
                created_incidents.append(request)
                return httpx.Response(201, json={"result": {"sys_id": "sn-race-200", "number": "INC0010888", "state": "1"}})
            elif request.method == "PATCH":
                updated_work_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-race-200", "state": "2"}})
            elif request.method == "GET":
                return httpx.Response(200, json={"result": {"sys_id": "sn-race-200", "state": "2"}})
        if "/api/now/attachment/file" in url_str:
            return httpx.Response(201, json={"result": {"sys_id": "att-200"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    wm = WindowingStateMachine(config=test_config, adapter=adapter)

    group_id = "grp_race_test_20261023"
    wm.groups[group_id] = IncidentGroup(
        group_id=group_id,
        filename=f"incident-{group_id}.md",
        status=IncidentGroupStatus.INVESTIGATING,
        created_at=time.time(),
        origin="ata-orders-api",
        blast_radius=["ata-orders-api"],
    )
    results = []

    def worker(i: int):
        report = InvestigationReport(
            group_id=group_id,
            filename=f"incident-{group_id}.md",
            summary=f"Concurrent findings worker {i}",
            observations="Testing race safety",
            recommended_actions=["Verify single ticket"],
            origin="ata-orders-api",
            blast_radius=["ata-orders-api"],
        )
        return wm.handle_investigation_findings(report)

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(worker, i) for i in range(12)]
        for f in concurrent.futures.as_completed(futures):
            results.append(f.result())

    # CRITICAL: Exactly 1 incident created across all concurrent threads
    assert len(created_incidents) == 1
    assert len(results) == 12
    for r in results:
        assert r.sys_id == "sn-race-200"
        assert r.number == "INC0010888"


def test_restart_mid_group_does_not_duplicate(test_config, tmp_path):
    """Assert a bridge restart mid-group recovers ticket details and does NOT duplicate."""
    import yaml
    from pathlib import Path

    created_incidents = []
    updated_work_notes = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/now/table/sys_user" in url_str:
            return httpx.Response(200, json={"result": [{"sys_id": "usr-1", "user_name": "admin"}]})
        if "/api/now/table/incident" in url_str:
            if request.method == "POST":
                created_incidents.append(request)
                return httpx.Response(201, json={"result": {"sys_id": "sn-restart-300", "number": "INC0010777", "state": "1"}})
            elif request.method == "PATCH":
                updated_work_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-restart-300", "state": "2"}})
            elif request.method == "GET":
                return httpx.Response(200, json={"result": {"sys_id": "sn-restart-300", "state": "2"}})
        if "/api/now/attachment/file" in url_str:
            return httpx.Response(201, json={"result": {"sys_id": "att-300"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)

    # Set up OKF local directory
    okf_dir = Path(test_config.incident_store_path or "/tmp/knowledge/incident-manager")
    okf_dir.mkdir(parents=True, exist_ok=True)
    filename = "incident-restart-mid-group.md"
    group_id = "grp_restart_mid_group_001"

    # 1. State machine 1 creates initial ticket
    wm1 = WindowingStateMachine(config=test_config, adapter=adapter)
    wm1.groups[group_id] = IncidentGroup(
        group_id=group_id,
        filename=filename,
        status=IncidentGroupStatus.INVESTIGATING,
        created_at=time.time(),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
    )
    r1 = InvestigationReport(
        group_id=group_id,
        filename=filename,
        summary="Initial preliminary findings before restart",
        observations="Connections rising",
        recommended_actions=["Monitor"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
    )
    ref1 = wm1.handle_investigation_findings(r1)
    assert ref1.sys_id == "sn-restart-300"
    assert ref1.number == "INC0010777"
    assert len(created_incidents) == 1

    # Verify OKF was written with ticket info
    local_okf = okf_dir / filename
    assert local_okf.exists()
    content = local_okf.read_text(encoding="utf-8")
    meta = yaml.safe_load(content.split("---")[1])
    assert meta["servicenow_sys_id"] == "sn-restart-300"
    assert meta["servicenow_number"] == "INC0010777"

    # 2. SIMULATE RESTART: wm1 is dropped; wm2 is instantiated fresh (empty in-memory state)
    wm2 = WindowingStateMachine(config=test_config, adapter=adapter)
    assert group_id not in wm2.groups

    # Second callback arrives at wm2 (e.g. alert 2 joined or settle tick after bridge restart)
    r2 = InvestigationReport(
        group_id=group_id,
        filename=filename,
        summary="Second callback post-restart: orders joined",
        observations="Pool exhaustion confirmed",
        recommended_actions=["Restart pool"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
    )
    ref2 = wm2.handle_investigation_findings(r2)

    # CRITICAL: NO new incident created; recovered existing ticket from OKF
    assert len(created_incidents) == 1
    assert ref2.sys_id == "sn-restart-300"
    assert ref2.number == "INC0010777"
    assert wm2.groups[group_id].servicenow_sys_id == "sn-restart-300"


def test_query_before_create_deterministic_correlation_id(test_config):
    """Assert query-before-create correlation_id recovers existing ServiceNow incident when OKF is absent."""
    created_incidents = []
    updated_work_notes = []
    query_calls = []

    corr_filename = "incident-external-corr-999.md"

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "/api/now/table/incident" in url_str:
            if request.method == "GET" and corr_filename in url_str:
                query_calls.append(request)
                return httpx.Response(200, json={"result": [{"sys_id": "sn-corr-existing-400", "number": "INC0010666"}]})
            elif request.method == "POST":
                created_incidents.append(request)
                return httpx.Response(201, json={"result": {"sys_id": "sn-SHOULD-NOT-BE-CREATED", "number": "INC0099999"}})
            elif request.method == "PATCH":
                updated_work_notes.append(request)
                return httpx.Response(200, json={"result": {"sys_id": "sn-corr-existing-400"}})
        if "/api/now/attachment/file" in url_str:
            return httpx.Response(201, json={"result": {"sys_id": "att-400"}})
        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    wm = WindowingStateMachine(config=test_config, adapter=adapter)

    # Make sure local OKF doesn't exist for this file
    from pathlib import Path
    for p in [Path(f"/knowledge/incident-manager/{corr_filename}"), Path(f"/tmp/knowledge/incident-manager/{corr_filename}")]:
        if p.exists():
            p.unlink()

    report = InvestigationReport(
        group_id="grp_corr_test_999",
        filename=corr_filename,
        summary="Findings for pre-existing correlation record",
        observations="Correlation test",
        recommended_actions=["Verify existing ticket reused"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
    )

    ref = wm.handle_investigation_findings(report)

    # CRITICAL: Reused existing ticket from correlation_id query; zero POST /incident
    assert len(created_incidents) == 0
    assert len(query_calls) >= 1
    assert ref.sys_id == "sn-corr-existing-400"
    assert ref.number == "INC0010666"
    assert wm.groups["grp_corr_test_999"].servicenow_sys_id == "sn-corr-existing-400"
