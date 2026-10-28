"""Comprehensive tests for Agent Group State, Timers, Suppression & Instruction Client.

Tests:
1. Stale-wake handling:
   - Stale settle wakes ignored when settle deadline was extended.
   - Valid settle wakes trigger investigation and ticket minting.
2. Flapping cool-down reactivation:
   - Group enters cooling_down on alert closure with 180s timer.
   - New alert arrival reactivates group to ticketed and appends work note.
   - Subsequent cooling timer wake is safely ignored.
3. Late alert note appending on ticketed groups:
   - Alert joins already-ticketed group.
   - Work note appended via BridgeClient.append_work_note with formatted HTML.
4. Max-wait deadline enforcement:
   - Cascading alerts push settle deadline out continuously.
   - Settle deadline is capped at max_wait_deadline (300s).
   - Max-wait wake forces investigation and ticket minting.
5. Atomic OKF store concurrency under simulated race conditions:
   - Multithreaded concurrent writers with GCS if_generation_match precondition mismatches.
   - Verifies zero lock clobbering, retry backoff, and all alerts preserved.
6. Safe upstream DAG origin promotion:
   - Downstream origin promoted upstream on upstream anomaly.
   - Downstream subsequent alerts do not demote origin.
7. Duplicate alert suppression:
   - Idempotent duplicate alert suppression across requests.
8. Timer scheduler deterministic IDs and AlreadyExists handling.
9. BridgeClient complete method suite and retry semantics.
"""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
from pathlib import Path
import threading
import time
from typing import Any, Optional
from google.api_core import exceptions
import httpx
import pytest
from starlette.testclient import TestClient

from agent.app.bridge_client import (
    BridgeClient,
    append_work_note,
    create_ticket,
    query_ticket,
    resolve_ticket,
    update_report,
)
from agent.app.config import AgentConfig, get_agent_settings
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import (
    ActionStatusResponse,
    CreateTicketRequest,
    ResolveTicketRequest,
    TicketResponse,
    UpdateReportRequest,
    WorkNoteRequest,
)
from agent.app.contracts.report import InvestigationReport, ProbableRootCause
from agent.app.contracts.timers import TimerWakeRequest, TimerWakeResponse
from agent.app.grouping.manager import GroupingManager, GroupingResult
from agent.app.grouping.models import IncidentGroupStatus, OKFDocument, parse_iso_timestamp
from agent.app.grouping.store import (
    ConcurrencyLockError,
    OKFStore,
    read_okf,
    update_okf_atomically,
    write_okf,
)
from agent.app.grouping.suppression import (
    check_duplicate_alert,
    format_late_alert_work_note,
    safe_promote_upstream_origin,
)
from agent.app.main import app, set_clock_func
from agent.app.timers.scheduler import (
    get_timer_scheduler_hook,
    schedule_delayed_timer,
    set_timer_scheduler_hook,
)


# ==============================================================================
# Fixtures
# ==============================================================================

@pytest.fixture
def temp_store(tmp_path: Path) -> Path:
    store_dir = tmp_path / "knowledge" / "incident-manager"
    store_dir.mkdir(parents=True, exist_ok=True)
    return store_dir


@pytest.fixture
def agent_cfg(temp_store: Path) -> AgentConfig:
    return AgentConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        incident_store_path=str(temp_store),
        fallback_store_path=str(temp_store),
        bridge_url="http://mock-bridge:8080",
        agent_url="http://mock-agent:8000",
        timer_queue_name="ata-timer-queue",
        is_local=True,
    )


@pytest.fixture(autouse=True)
def reset_globals():
    set_clock_func(None)
    set_timer_scheduler_hook(lambda **kwargs: f"mock-task-{kwargs.get('deadline_type')}")
    yield
    set_clock_func(None)
    set_timer_scheduler_hook(None)


# ==============================================================================
# 1. Stale-Wake Handling Tests
# ==============================================================================

def test_stale_settle_wake_ignored_and_valid_executed(agent_cfg: Path, monkeypatch):
    """Test that stale settle wakes are ignored when deadline is extended, and valid wakes execute."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app)

    simulated_time = 1000.0
    set_clock_func(lambda: simulated_time)

    # Ingest initial alert at t=1000.0 -> settle deadline is 1120.0
    alert1 = AlertEvent(
        incident_id="0.alert-001",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-latency",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:16:40Z",
    )
    res1 = client.post("/triage", json=alert1.model_dump(mode="json"))
    assert res1.status_code == 200
    group_id = res1.json()["group_id"]

    store = OKFStore(config=agent_cfg)
    doc1 = store.read(group_id)
    assert doc1 is not None
    assert doc1.status == "open"
    assert doc1.settle_deadline_epoch() == pytest.approx(1120.0)

    # Second alert arrives at t=1030.0 -> settle deadline extended to 1150.0
    simulated_time = 1030.0
    alert2 = AlertEvent(
        incident_id="0.alert-002",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-latency",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:17:10Z",
    )
    res2 = client.post("/triage", json=alert2.model_dump(mode="json"))
    assert res2.status_code == 200
    doc2 = store.read(group_id)
    assert doc2.settle_deadline_epoch() == pytest.approx(1150.0)

    # Cloud Tasks wake for the FIRST scheduled wake arrives at t=1120.0
    simulated_time = 1120.0
    wake_req_1 = TimerWakeRequest(
        group_id=group_id,
        deadline_type="settle",
        scheduled_epoch=1120,
    )
    wake_res_1 = client.post("/timer/wake", json=wake_req_1.model_dump(mode="json"))
    assert wake_res_1.status_code == 200
    w_data_1 = wake_res_1.json()
    assert w_data_1["status"] == "ignored"
    assert w_data_1["reason"] == "stale_wake"

    # OKF status must remain open (not ticketed)
    doc_after_stale = store.read(group_id)
    assert doc_after_stale.status == "open"
    assert doc_after_stale.servicenow_sys_id is None

    # Advance clock to t=1150.0 when second wake arrives
    simulated_time = 1150.0

    # Mock BridgeClient.create_ticket to simulate successful ticket creation
    ticket_created = False

    def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
        nonlocal ticket_created
        ticket_created = True
        return TicketResponse(
            status="created",
            sys_id="sys_mock_123",
            number="INC009988",
            url="https://mock.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_mock_123",
            group_id=request.group_id,
        )

    monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)

    wake_req_2 = TimerWakeRequest(
        group_id=group_id,
        deadline_type="settle",
        scheduled_epoch=1150,
    )
    wake_res_2 = client.post("/timer/wake", json=wake_req_2.model_dump(mode="json"))
    assert wake_res_2.status_code == 200
    w_data_2 = wake_res_2.json()
    assert w_data_2["status"] == "executed"
    assert ticket_created is True

    # OKF status must transition to ticketed with ticket reference
    doc_final = store.read(group_id)
    assert doc_final.status == "ticketed"
    assert doc_final.servicenow_sys_id == "sys_mock_123"
    assert doc_final.servicenow_number == "INC009988"


# ==============================================================================
# 2. Flapping Cool-Down Reactivation Tests
# ==============================================================================

def test_flapping_cooling_down_reactivates_to_ticketed(agent_cfg: Path, monkeypatch):
    """Test flapping alert: group enters cooling_down on alert closure, reactivates on new alert."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1000.0
    set_clock_func(lambda: simulated_time)

    # Create an initial group and simulate it being ticketed
    alert1 = AlertEvent(
        incident_id="0.flap-alert-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:16:40Z",
    )
    res1 = client.post("/triage", json=alert1.model_dump(mode="json"))
    group_id = res1.json()["group_id"]

    # Mark as ticketed with servicenow_sys_id
    store.update_atomically(
        group_id,
        lambda d: setattr(d, "status", "ticketed") or setattr(d, "servicenow_sys_id", "sys_flap_123") or d,
    )

    # Capture scheduled delayed timers
    scheduled_timers: list[dict[str, Any]] = []
    set_timer_scheduler_hook(
        lambda **kwargs:
            scheduled_timers.append(kwargs) or f"task-{kwargs.get('deadline_type')}"
    )

    # Alert closes at t=1060.0
    simulated_time = 1060.0
    alert_closed = AlertEvent(
        incident_id="0.flap-alert-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="closed",
        started_at="2026-10-24T00:16:40Z",
    )
    res_close = client.post("/triage", json=alert_closed.model_dump(mode="json"))
    assert res_close.status_code == 200

    # Group must be in cooling_down, with 180s cooling deadline
    doc_cooling = store.read(group_id)
    assert doc_cooling.status == "cooling_down"
    assert doc_cooling.cooling_deadline_epoch() == pytest.approx(1060.0 + 180.0)

    # Assert cooling timer was scheduled
    assert any(t["deadline_type"] == "cooling" for t in scheduled_timers)

    # Before cooling expires (e.g. at t=1120.0), flapping alert re-triggers!
    simulated_time = 1120.0
    flapping_alert = AlertEvent(
        incident_id="0.flap-alert-2",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:18:40Z",
    )

    notes_appended: list[WorkNoteRequest] = []

    def mock_append_work_note(self, bridge_url, request, config=None, http_client=None):
        notes_appended.append(request)
        return ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id)

    monkeypatch.setattr(BridgeClient, "append_work_note", mock_append_work_note)
    ticket_updates: list[Any] = []
    monkeypatch.setattr(BridgeClient, "update_ticket_fields", lambda self, bridge_url, request, **kw: ticket_updates.append(request))

    res_retrigger = client.post("/triage", json=flapping_alert.model_dump(mode="json"))
    assert res_retrigger.status_code == 200
    assert res_retrigger.json()["group_id"] == group_id

    # Group must reactivate to ticketed
    doc_reactivated = store.read(group_id)
    assert doc_reactivated.status == "ticketed"
    assert doc_reactivated.cooling_deadline is None

    # Work note must have been appended
    assert len(notes_appended) == 1
    assert notes_appended[0].sys_id == "sys_flap_123"
    assert "Correlated Alert Ingested" in notes_appended[0].work_note
    # Ticket title follows the group: 2 alerts now.
    assert len(ticket_updates) == 1 and ticket_updates[0].short_description.endswith("(2 alerts)")

    # Now simulate the original cooling timer wake arriving at t=1240.0 (1060 + 180)
    simulated_time = 1240.0
    wake_cooling = TimerWakeRequest(
        group_id=group_id,
        deadline_type="cooling",
        scheduled_epoch=1240,
    )
    wake_res = client.post("/timer/wake", json=wake_cooling.model_dump(mode="json"))
    assert wake_res.status_code == 200
    # Because status is "ticketed" and NOT "cooling_down", the wake must be ignored!
    assert wake_res.json()["status"] == "ignored"

    # Status remains ticketed
    assert store.read(group_id).status == "ticketed"


# ==============================================================================
# 3. Late Alert Note Appending Tests
# ==============================================================================

def test_late_alert_appends_work_note_on_ticketed_group(agent_cfg: Path, monkeypatch):
    """Test that late alerts arriving for an already ticketed group append a work note."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app)
    store = OKFStore(config=agent_cfg)

    # Initial group in ticketed state
    group_id = "test-late-alert-group"
    doc = OKFDocument(
        group_id=group_id,
        status="ticketed",
        policy_name="ata-orders-latency",
        resource_name="ata-orders-api",
        origin="ata-orders-api",
        blast_radius=["ata-orders-api"],
        servicenow_sys_id="sys_late_456",
        servicenow_number="INC001122",
        alerts=[{
            "incident_id": "0.initial-01",
            "policy_name": "ata-orders-latency",
            "resource_name": "ata-orders-api",
            "state": "open",
        }],
    )
    store.write(group_id, doc)

    notes_sent: list[WorkNoteRequest] = []

    def mock_append_note(self, bridge_url, request, config=None, http_client=None):
        notes_sent.append(request)
        return ActionStatusResponse(status="success", sys_id=request.sys_id, group_id=request.group_id)

    monkeypatch.setattr(BridgeClient, "append_work_note", mock_append_note)
    ticket_updates: list[Any] = []
    monkeypatch.setattr(BridgeClient, "update_ticket_fields", lambda self, bridge_url, request, **kw: ticket_updates.append(request))

    # Correlated late alert arrives
    late_alert = AlertEvent(
        incident_id="0.late-02",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-latency",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:20:00Z",
    )
    res = client.post("/triage", json=late_alert.model_dump(mode="json"))
    assert res.status_code == 200
    assert res.json()["group_id"] == group_id

    # Work note must have been appended
    assert len(notes_sent) == 1
    assert notes_sent[0].sys_id == "sys_late_456"
    assert "ata-inventory-latency" in notes_sent[0].work_note
    assert "0.late-02" in notes_sent[0].work_note
    assert len(ticket_updates) == 1 and ticket_updates[0].short_description.endswith("(2 alerts)")

    # OKF document must retain ticketed status and have both alerts
    updated = store.read(group_id)
    assert updated.status == "ticketed"
    assert len(updated.alerts) == 2
    assert any(a["incident_id"] == "0.late-02" for a in updated.alerts)


# ==============================================================================
# 4. Max-Wait Deadline Enforcement Tests
# ==============================================================================

def test_max_wait_deadline_enforcement(agent_cfg: Path, monkeypatch):
    """Test max-wait deadline: settle pushed out continuously, capped at 300s, max-wait wake forces ticket."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1000.0
    set_clock_func(lambda: simulated_time)

    # Alert 1 arrives at t=1000.0 -> created=1000, settle=1120, max_wait=1300
    alert1 = AlertEvent(
        incident_id="0.mw-alert-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-5xx",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:16:40Z",
    )
    res1 = client.post("/triage", json=alert1.model_dump(mode="json"))
    group_id = res1.json()["group_id"]

    doc1 = store.read(group_id)
    assert doc1.max_wait_deadline_epoch() == pytest.approx(1300.0)

    # Simulating cascading alerts arriving at t=1080, t=1160, t=1240
    for i, t_arr in enumerate([1080.0, 1160.0, 1240.0], start=2):
        simulated_time = t_arr
        alert_cascade = AlertEvent(
            incident_id=f"0.mw-alert-{i}",
            policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-5xx",
            resource_name="ata-inventory-api",
            resource_type="cloud_run_revision",
            state="open",
            started_at="2026-10-24T00:17:00Z",
        )
        res = client.post("/triage", json=alert_cascade.model_dump(mode="json"))
        assert res.status_code == 200

    # At t=1240, settle would normally be 1240 + 120 = 1360, but it MUST be capped at max_wait 1300!
    doc_capped = store.read(group_id)
    assert doc_capped.settle_deadline_epoch() == pytest.approx(1300.0)

    # Advance to t=1300.0
    simulated_time = 1300.0

    ticket_created = False

    def mock_create_ticket(self, bridge_url, request, config=None, http_client=None):
        nonlocal ticket_created
        ticket_created = True
        return TicketResponse(
            status="created",
            sys_id="sys_mw_forced",
            number="INC008877",
            url="https://mock.service-now.com/nav_to.do?uri=incident.do?sys_id=sys_mw_forced",
            group_id=request.group_id,
        )

    monkeypatch.setattr(BridgeClient, "create_ticket", mock_create_ticket)

    # Max-wait timer fires at t=1300.0
    mw_wake = TimerWakeRequest(
        group_id=group_id,
        deadline_type="max_wait",
        scheduled_epoch=1300,
    )
    mw_res = client.post("/timer/wake", json=mw_wake.model_dump(mode="json"))
    assert mw_res.status_code == 200
    assert mw_res.json()["status"] == "executed"
    assert ticket_created is True

    # Group must now be ticketed
    assert store.read(group_id).status == "ticketed"
    assert store.read(group_id).servicenow_number == "INC008877"


# ==============================================================================
# 5. Atomic OKF Store Concurrency Under Simulated Race Conditions
# ==============================================================================

class ConcurrencyMockBlob:
    """Mock GCS Blob simulating generation tracking and if_generation_match precondition."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.lock = threading.Lock()
        self.generation: int = 0
        self.content: Optional[str] = None
        self._exists: bool = False

    def reload(self) -> None:
        with self.lock:
            if not self._exists:
                raise exceptions.NotFound(f"Blob {self.name} not found")

    def download_as_text(self) -> str:
        with self.lock:
            if not self._exists:
                raise exceptions.NotFound(f"Blob {self.name} not found")
            return self.content or ""

    def upload_from_string(
        self,
        data: str,
        content_type: str = "text/markdown",
        if_generation_match: Optional[int] = None,
    ) -> None:
        with self.lock:
            if if_generation_match is not None:
                if if_generation_match != self.generation:
                    raise exceptions.PreconditionFailed(
                        f"Generation mismatch: expected {self.generation}, got {if_generation_match}"
                    )
            self.content = data
            self._exists = True
            self.generation += 1


class ConcurrencyMockBucket:
    def __init__(self) -> None:
        self.blobs: dict[str, ConcurrencyMockBlob] = {}
        self.lock = threading.Lock()

    def blob(self, blob_name: str) -> ConcurrencyMockBlob:
        with self.lock:
            if blob_name not in self.blobs:
                self.blobs[blob_name] = ConcurrencyMockBlob(blob_name)
            return self.blobs[blob_name]


class ConcurrencyMockStorageClient:
    def __init__(self) -> None:
        self.bucket_inst = ConcurrencyMockBucket()

    def bucket(self, name: str) -> ConcurrencyMockBucket:
        return self.bucket_inst


def test_atomic_okf_concurrency_race_conditions():
    """Verify concurrent writes with GCS if_generation_match retries preserve all alerts without data loss."""
    mock_client = ConcurrencyMockStorageClient()
    group_id = "test-concurrency-group"
    bucket_name = "test-bucket"

    # Initialize document
    initial_doc = OKFDocument(
        group_id=group_id,
        status="open",
        alerts=[{"incident_id": "alert-0"}],
    )
    store = OKFStore(bucket_name=bucket_name, storage_client=mock_client, is_local=False)
    store.write(group_id, initial_doc)

    num_threads = 10

    def worker(worker_id: int):
        def mutation(doc: OKFDocument) -> OKFDocument:
            time.sleep(0.005)  # Introduce slight jitter
            doc.add_alert({
                "incident_id": f"alert-{worker_id}",
                "policy_name": f"policy-{worker_id}",
                "resource_name": f"res-{worker_id}",
            })
            return doc

        store.update_atomically(group_id, mutation, max_retries=15)

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = [executor.submit(worker, i) for i in range(1, num_threads + 1)]
        for f in futures:
            f.result()

    final_doc = store.read(group_id)
    assert final_doc is not None
    # All 1 initial + 10 worker alerts must be present
    assert len(final_doc.alerts) == 11
    ids = {a["incident_id"] for a in final_doc.alerts}
    for i in range(num_threads + 1):
        assert f"alert-{i}" in ids


# ==============================================================================
# 6. Safe Upstream DAG Origin Promotion Tests
# ==============================================================================

def test_safe_upstream_origin_promotion(agent_cfg: Path):
    """Test safe upstream origin promotion: downstream -> upstream promoted, downstream not demoted."""
    store = OKFStore(config=agent_cfg)
    manager = GroupingManager(config=agent_cfg, store=store)

    # 1. Downstream alert arrives on ata-orders-api
    alert_orders = AlertEvent(
        incident_id="0.downstream-1",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-latency",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:15:00Z",
    )
    res1 = manager.ingest_alert(alert_orders)
    group_id = res1.group_id

    doc1 = store.read(group_id)
    assert doc1.origin == "ata-orders-api"

    # 2. Upstream alert arrives on ata-inventory-api
    alert_inventory = AlertEvent(
        incident_id="0.upstream-2",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-inventory-latency",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:15:30Z",
    )
    res2 = manager.ingest_alert(alert_inventory, bridge_group_id=group_id)
    doc2 = store.read(group_id)
    # Origin must be promoted to upstream ata-inventory-api!
    assert doc2.origin == "ata-inventory-api"
    assert "ata-orders-api" in doc2.blast_radius

    # 3. Subsequent downstream alert arrives on ata-apigee-orders-v1
    alert_apigee = AlertEvent(
        incident_id="0.downstream-3",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-apigee-latency",
        resource_name="ata-apigee-orders-v1",
        resource_type="apigee_proxy",
        state="open",
        started_at="2026-10-24T00:16:00Z",
    )
    res3 = manager.ingest_alert(alert_apigee, bridge_group_id=group_id)
    doc3 = store.read(group_id)
    # Origin MUST NOT be demoted back to apigee! It must remain upstream ata-inventory-api!
    assert doc3.origin == "ata-inventory-api"


# ==============================================================================
# 7. Duplicate Alert Suppression Tests
# ==============================================================================

def test_duplicate_alert_suppression(agent_cfg: Path):
    """Test that redelivered alerts with identical incident_id are suppressed idempotently."""
    store = OKFStore(config=agent_cfg)
    manager = GroupingManager(config=agent_cfg, store=store)

    alert = AlertEvent(
        incident_id="0.dup-test-999",
        policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-orders-5xx",
        resource_name="ata-orders-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:15:00Z",
    )
    res1 = manager.ingest_alert(alert)
    assert res1.status == "triaged"
    assert res1.is_duplicate is False

    # Redeliver same alert
    res2 = manager.ingest_alert(alert)
    assert res2.status == "duplicate"
    assert res2.is_duplicate is True
    assert res2.group_id == res1.group_id

    # Verify OKF has only 1 alert entry
    doc = store.read(res1.group_id)
    assert len(doc.alerts) == 1


# ==============================================================================
# 8. Timer Scheduler Tests
# ==============================================================================

def test_timer_scheduler_deterministic_id_and_already_exists():
    """Test deterministic task ID generation and AlreadyExists handling in scheduler."""
    target_dt = datetime(2026, 10, 24, 0, 17, 30, tzinfo=timezone.utc)
    epoch = int(target_dt.timestamp())

    # Mock hook verification
    hook_calls = []
    set_timer_scheduler_hook(lambda **kwargs: hook_calls.append(kwargs) or "mock-task")

    result = schedule_delayed_timer(
        project_id="cloud-assist-fde-2",
        location="us-west1",
        queue_name="ata-timer-queue",
        target_url="http://agent:8000",
        group_id="test_group_special_chars!@#",
        deadline_type="settle",
        deadline_dt=target_dt,
        service_account_email="sa@proj.iam.gserviceaccount.com",
    )
    assert result == "mock-task"
    assert len(hook_calls) == 1
    assert hook_calls[0]["deadline_type"] == "settle"

    # Reset hook to test default generation and live client handling
    set_timer_scheduler_hook(None)
    from unittest.mock import MagicMock, patch
    from google.api_core import exceptions as gcp_exceptions

    expected_id = f"ata-timer-grp-alpha-settle-{epoch}"
    mock_resp = MagicMock()
    mock_resp.name = f"projects/cloud-assist-fde-2/locations/us-west1/queues/ata-timer-queue/tasks/{expected_id}"
    mock_client = MagicMock()
    mock_client.create_task.return_value = mock_resp

    with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
        task_name = schedule_delayed_timer(
            project_id="cloud-assist-fde-2",
            location="us-west1",
            queue_name="ata-timer-queue",
            target_url="http://agent:8000",
            group_id="grp-alpha",
            deadline_type="settle",
            deadline_dt=target_dt,
            service_account_email="sa@proj.iam.gserviceaccount.com",
        )
        assert expected_id in task_name

    # Test AlreadyExists handling
    mock_client.create_task.side_effect = gcp_exceptions.AlreadyExists("Task already exists")
    with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
        task_name = schedule_delayed_timer(
            project_id="cloud-assist-fde-2",
            location="us-west1",
            queue_name="ata-timer-queue",
            target_url="http://agent:8000",
            group_id="grp-alpha",
            deadline_type="settle",
            deadline_dt=target_dt,
            service_account_email="sa@proj.iam.gserviceaccount.com",
        )
        assert expected_id in task_name


# ==============================================================================
# 9. BridgeClient Complete Method Suite and Retries
# ==============================================================================

def test_bridge_client_methods_and_retries(agent_cfg: Path):
    """Test BridgeClient create_ticket, append_work_note, update_report, resolve_ticket, query_ticket."""
    sample_report = InvestigationReport(
        group_id="grp-test-bc",
        filename="incident-grp-test-bc.md",
        summary="Test summary",
        observations="Test observations",
        recommended_actions=[],
        probable_root_cause=ProbableRootCause(text="Root cause", confidence=0.9),
        origin="ata-orders-api",
        blast_radius=["ata-orders-api"],
        priority="P1",
        status="In Progress",
    )

    request_history = []
    attempt_count = {"create": 0, "work_note": 0}

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        request_history.append((request.method, url_str))

        if "/instructions/ticket/create" in url_str:
            attempt_count["create"] += 1
            if attempt_count["create"] == 1:
                return httpx.Response(503, json={"error": "transient bridge unavailable"})
            return httpx.Response(200, json={
                "status": "created",
                "sys_id": "sys_bc_1",
                "number": "INC007711",
                "url": "https://snow/sys_bc_1",
                "group_id": "grp-test-bc",
            })

        if "/instructions/ticket/work-note" in url_str:
            return httpx.Response(200, json={
                "status": "success",
                "sys_id": "sys_bc_1",
                "group_id": "grp-test-bc",
                "message": "Note added",
            })

        if "/instructions/ticket/update-report" in url_str:
            return httpx.Response(200, json={
                "status": "updated",
                "sys_id": "sys_bc_1",
                "number": "INC007711",
                "url": "https://snow/sys_bc_1",
                "group_id": "grp-test-bc",
            })

        if "/instructions/ticket/resolve" in url_str:
            return httpx.Response(200, json={
                "status": "resolved",
                "sys_id": "sys_bc_1",
                "group_id": "grp-test-bc",
                "message": "Incident resolved",
            })

        if "/instructions/ticket/query/grp-test-bc" in url_str:
            return httpx.Response(200, json={
                "status": "found",
                "sys_id": "sys_bc_1",
                "number": "INC007711",
                "url": "https://snow/sys_bc_1",
                "group_id": "grp-test-bc",
            })

        if "/instructions/ticket/query/non-existent" in url_str:
            return httpx.Response(404, json={"error": "not found"})

        return httpx.Response(404, json={"error": "not found"})

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.Client(transport=transport)
    client = BridgeClient(config=agent_cfg, http_client=mock_http, max_retries=2, backoff_factor=0.01)

    # 1. create_ticket (tests 503 transient retry)
    c_res = client.create_ticket("http://testbridge", CreateTicketRequest(group_id="grp-test-bc", report=sample_report))
    assert c_res.number == "INC007711"
    assert attempt_count["create"] == 2

    # 2. append_work_note
    wn_res = client.append_work_note("http://testbridge", WorkNoteRequest(sys_id="sys_bc_1", work_note="Test note", group_id="grp-test-bc"))
    assert wn_res.status == "success"

    # 3. update_report
    ur_res = client.update_report("http://testbridge", UpdateReportRequest(sys_id="sys_bc_1", group_id="grp-test-bc", report=sample_report))
    assert ur_res.status == "updated"

    # 4. resolve_ticket
    res_res = client.resolve_ticket("http://testbridge", ResolveTicketRequest(sys_id="sys_bc_1", group_id="grp-test-bc", close_notes="Resolved"))
    assert res_res.status == "resolved"

    # 5. query_ticket
    q_res = client.query_ticket("http://testbridge", "grp-test-bc")
    assert q_res.number == "INC007711"

    # 6. query_ticket 404 (does NOT retry, raises HTTPStatusError immediately)
    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        client.query_ticket("http://testbridge", "non-existent")
    assert exc_info.value.response.status_code == 404


def test_post_resolution_alert_initializes_fresh_new_group(agent_cfg: Path):
    """An alert arriving after an incident group is resolved initializes a fresh new group."""
    app.dependency_overrides[get_agent_settings] = lambda: agent_cfg
    client = TestClient(app)
    store = OKFStore(config=agent_cfg)

    simulated_time = 1000.0
    set_clock_func(lambda: simulated_time)

    try:
        # 1. Initial alert creates group
        alert_initial = AlertEvent(
            incident_id="inc-init-1",
            policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-apigee-orders-429",
            resource_name="ata-orders-v1",
            resource_type="apigee_proxy",
            state="open",
            started_at="2026-10-23T05:16:00Z",
        )
        res1 = client.post("/triage", json=alert_initial.model_dump(mode="json"))
        assert res1.status_code == 200
        group_id_1 = res1.json()["group_id"]
        assert group_id_1 is not None

        # Verify group 1 is active
        doc1 = store.read(group_id_1)
        assert doc1.status == "open"

        # 2. Mark group 1 as resolved (simulating resolution after cooling expiry)
        store.update_atomically(
            group_id_1,
            lambda d: setattr(d, "status", "resolved") or setattr(d, "servicenow_sys_id", "sys_post_res_1") or d,
        )
        doc1_resolved = store.read(group_id_1)
        assert doc1_resolved.status == "resolved"

        # 3. New alert arrives on the exact same policy and resource after resolution
        simulated_time = 2000.0
        alert_new = AlertEvent(
            incident_id="inc-new-2",
            policy_name="projects/cloud-assist-fde-2/alertPolicies/ata-apigee-orders-429",
            resource_name="ata-orders-v1",
            resource_type="apigee_proxy",
            state="open",
            started_at="2026-10-23T05:25:00Z",
        )
        res2 = client.post("/triage", json=alert_new.model_dump(mode="json"))
        assert res2.status_code == 200
        group_id_2 = res2.json()["group_id"]

        # Must NOT reuse group_id_1
        assert group_id_2 != group_id_1
        doc2 = store.read(group_id_2)
        assert doc2.status == "open"
        assert doc2.group_id == group_id_2
        assert any(a.get("incident_id") == "inc-new-2" for a in doc2.alerts)
        assert not any(a.get("incident_id") == "inc-init-1" for a in doc2.alerts)
    finally:
        set_clock_func(time.time)

