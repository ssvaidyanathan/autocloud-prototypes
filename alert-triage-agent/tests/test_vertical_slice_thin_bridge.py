"""Comprehensive end-to-end tests exercising the Thin Bridge vertical slice.

Verifies:
1. End-to-end flow: Ingest alert -> write atomic OKF with GCS precondition -> simulate timer wake ->
   synthesize report -> call POST /instructions/ticket/create on Bridge -> ServiceNow adapter creates
   incident (State 1, State 2, 4-bullet work note, HTML report attachment) -> ServiceNow incident ref
   returned and written to OKF frontmatter.
2. Idempotency: Second POST /instructions/ticket/create for same group_id returns 'already_exists'
   with existing ticket number and sys_id without creating a new incident.
3. Concurrency / Precondition: Concurrent writes to same OKF blob with PreconditionFailed retry
   succeeds without data loss or clobbering.
4. BridgeClient transient retry on 5xx and non-retry on 4xx client errors.
5. Local filesystem atomic store fallback with file locking.
"""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading
from typing import Any, Optional
import httpx
import pytest
from starlette.testclient import TestClient
from google.api_core import exceptions

from agent.app.bridge_client import BridgeClient
from agent.app.config import AgentConfig
from agent.app.contracts.alert import AlertEvent
from agent.app.contracts.instructions import CreateTicketRequest, TicketResponse
from agent.app.contracts.report import (
    EvidenceItem,
    InvestigationReport,
    ProbableRootCause,
    TimelineItem,
)
from agent.app.grouping.store import (
    ConcurrencyLockError,
    OKFDocument,
    OKFStore,
    update_okf_atomically,
)
from bridge.app.adapters.servicenow import ServiceNowAdapter
from bridge.app.config import BridgeConfig, get_settings
from bridge.app.main import app, set_servicenow_adapter


# ==============================================================================
# In-Memory Mock GCS Infrastructure with Generation Preconditions
# ==============================================================================

class MockGCSBlob:
    """Mock GCS Blob simulating generation tracking and if_generation_match precondition."""

    def __init__(self, name: str, lock: threading.Lock) -> None:
        self.name = name
        self._lock = lock
        self.generation: int = 0
        self.content: Optional[str] = None
        self._exists: bool = False

    def reload(self) -> None:
        with self._lock:
            if not self._exists:
                raise exceptions.NotFound(f"Blob {self.name} not found")

    def download_as_text(self) -> str:
        with self._lock:
            if not self._exists:
                raise exceptions.NotFound(f"Blob {self.name} not found")
            return self.content or ""

    def upload_from_string(
        self,
        data: str,
        content_type: str = "text/markdown",
        if_generation_match: Optional[int] = None,
    ) -> None:
        with self._lock:
            if if_generation_match is not None:
                if if_generation_match != self.generation:
                    raise exceptions.PreconditionFailed(
                        f"Generation mismatch for {self.name}: expected {self.generation}, "
                        f"got if_generation_match={if_generation_match}"
                    )
            self.content = data
            self._exists = True
            self.generation += 1


class MockGCSBucket:
    """Mock GCS Bucket holding mock blobs."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.blobs: dict[str, MockGCSBlob] = {}
        self._lock = threading.Lock()

    def blob(self, blob_name: str) -> MockGCSBlob:
        with self._lock:
            if blob_name not in self.blobs:
                self.blobs[blob_name] = MockGCSBlob(blob_name, self._lock)
            return self.blobs[blob_name]


class MockGCSClient:
    """Mock GCS Client."""

    def __init__(self) -> None:
        self.buckets: dict[str, MockGCSBucket] = {}
        self._lock = threading.Lock()

    def bucket(self, bucket_name: str) -> MockGCSBucket:
        with self._lock:
            if bucket_name not in self.buckets:
                self.buckets[bucket_name] = MockGCSBucket(bucket_name)
            return self.buckets[bucket_name]


# ==============================================================================
# Mock ServiceNow HTTP Transport
# ==============================================================================

class MockServiceNowState:
    """State tracker for ServiceNow Table and Attachment API mock calls."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.incidents: dict[str, dict[str, Any]] = {}
        self.attachments: list[dict[str, Any]] = []
        self.post_incident_count: int = 0
        self.patch_state_count: int = 0
        self.work_notes_appended: list[str] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url_str = str(request.url)
        method = request.method

        # 1. Sys user dynamic caller lookup
        if method == "GET" and "/api/now/table/sys_user" in url_str:
            return httpx.Response(
                200,
                json={"result": [{"sys_id": "usr-sn-admin-01", "user_name": "admin", "name": "System Administrator"}]},
            )

        # 2. Query incident by correlation_id or sys_id
        if method == "GET" and "/api/now/table/incident" in url_str and "sysparm_query" in url_str:
            for inc in self.incidents.values():
                cid = inc.get("correlation_id", "")
                if cid and cid in url_str:
                    return httpx.Response(200, json={"result": [{"sys_id": inc["sys_id"], "number": inc["number"]}]})
            return httpx.Response(200, json={"result": []})

        # 3. Readback single incident record
        if method == "GET" and "/api/now/table/incident/" in url_str:
            sys_id = url_str.split("/api/now/table/incident/")[-1].split("?")[0]
            if sys_id in self.incidents:
                return httpx.Response(200, json={"result": self.incidents[sys_id]})
            return httpx.Response(404)

        # 4. Create incident (state=1)
        if method == "POST" and "/api/now/table/incident" in url_str:
            self.post_incident_count += 1
            body = json.loads(request.read())
            sys_id = "sn-sys-tb-p1-001"
            number = "INC0010999"
            incident_rec = {
                "sys_id": sys_id,
                "number": number,
                "state": body.get("state", "1"),
                "correlation_id": body.get("correlation_id", ""),
                "short_description": body.get("short_description", ""),
                "description": body.get("description", ""),
            }
            self.incidents[sys_id] = incident_rec
            return httpx.Response(201, json={"result": incident_rec})

        # 5. Patch incident (state=2 or work_notes)
        if method == "PATCH" and "/api/now/table/incident/" in url_str:
            sys_id = url_str.split("/api/now/table/incident/")[-1].split("?")[0]
            body = json.loads(request.read())
            if sys_id in self.incidents:
                if "state" in body:
                    self.incidents[sys_id]["state"] = body["state"]
                    if body["state"] == "2":
                        self.patch_state_count += 1
                if "work_notes" in body:
                    self.work_notes_appended.append(body["work_notes"])
                return httpx.Response(200, json={"result": self.incidents[sys_id]})
            return httpx.Response(404)

        # 6. Upload attachment
        if method == "POST" and "/api/now/attachment/file" in url_str:
            self.attachments.append({
                "url": url_str,
                "content_type": request.headers.get("Content-Type"),
                "bytes": request.read(),
            })
            return httpx.Response(201, json={"result": {"sys_id": "att-tb-p1-001", "file_name": "diagnostic.html"}})

        return httpx.Response(404)


# ==============================================================================
# Test Fixtures
# ==============================================================================

@pytest.fixture
def mock_storage():
    return MockGCSClient()


@pytest.fixture
def servicenow_state():
    return MockServiceNowState()


@pytest.fixture
def bridge_adapter(servicenow_state):
    config = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        servicenow_instance_url="https://devXXXXX.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        is_local=True,
    )
    client = httpx.Client(transport=httpx.MockTransport(servicenow_state.handle_request))
    adapter = ServiceNowAdapter(config=config, http_client=client)
    set_servicenow_adapter(adapter)
    app.dependency_overrides[get_settings] = lambda: config
    yield adapter
    set_servicenow_adapter(None)
    app.dependency_overrides.clear()


@pytest.fixture
def test_client():
    return TestClient(app)


# ==============================================================================
# Tests
# ==============================================================================

def test_vertical_slice_end_to_end(mock_storage, bridge_adapter, servicenow_state, test_client):
    """End-to-End Vertical Slice: Ingest -> Atomic OKF -> Settle Wake -> Bridge Instruction -> ServiceNow -> OKF Updated."""
    bucket_name = "cloud-assist-fde-2-ata-incidents"
    group_id = "ata-backend-5xx-inventory-api-20261024t001500z"
    blob_key = f"knowledge/incident-manager/incident-{group_id}.md"

    # 1. Ingest alert -> write atomic OKF with GCS precondition
    alert = AlertEvent(
        incident_id="0.s1_test_cascade_001",
        policy_name="ata-inventory-5xx",
        resource_name="ata-inventory-api",
        resource_type="cloud_run_revision",
        state="open",
        started_at="2026-10-24T00:15:00Z",
    )

    def init_fn(doc: OKFDocument) -> OKFDocument:
        doc.group_id = group_id
        doc.status = "open"
        doc.policy_name = alert.policy_name
        doc.resource_name = alert.resource_name
        doc.settle_deadline = "2026-10-24T00:17:00Z"
        doc.max_wait_deadline = "2026-10-24T00:20:00Z"
        doc.add_alert(alert.model_dump(mode="json"))
        return doc

    doc1 = update_okf_atomically(
        bucket_name=bucket_name,
        blob_name=blob_key,
        update_fn=init_fn,
        storage_client=mock_storage,
    )
    assert doc1.status == "open"
    assert len(doc1.alerts) == 1
    assert doc1.alerts[0]["incident_id"] == "0.s1_test_cascade_001"

    # Verify GCS mock blob generation advanced from 0 to 1
    blob = mock_storage.bucket(bucket_name).blob(blob_key)
    assert blob.generation == 1

    # 2. Simulate timer wake at settle expiration (t=120s) -> transition OKF to 'investigating'
    doc2 = update_okf_atomically(
        bucket_name=bucket_name,
        blob_name=blob_key,
        update_fn=lambda d: setattr(d, "status", "investigating") or d,
        storage_client=mock_storage,
    )
    assert doc2.status == "investigating"
    assert blob.generation == 2

    # 3. Multi-agent investigation synthesizes diagnostic report
    report = InvestigationReport(
        group_id=group_id,
        filename=f"incident-{group_id}.md",
        summary="Backend 5xx Cascade on ata-inventory-api",
        observations="Active 500 error cascade observed across dependent services.",
        recommended_actions=[
            "Inspect container revision health on ata-inventory-api",
            "Verify downstream Cloud SQL database response latency",
        ],
        probable_root_cause=ProbableRootCause(
            text="Exhaustion of backend database connection pool causing 500 responses.",
            confidence=0.95,
        ),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api", "ata-inventory-v1"],
        priority="P1",
        timeline=[
            TimelineItem(timestamp="2026-10-24T00:15:00Z", description="Initial 5xx rate exceeded 5% threshold"),
        ],
        evidence_by_area={
            "cloud_run": [
                EvidenceItem(headline="5xx HTTP Status Metric", content="run.googleapis.com/request_count response_code=500")
            ]
        },
        alerts=[alert.model_dump(mode="json")],
    )

    # 4. Agent calls Bridge Instruction Endpoint: POST /instructions/ticket/create
    agent_cfg = AgentConfig(is_local=True)
    bridge_client = BridgeClient(config=agent_cfg, http_client=test_client)

    create_req = CreateTicketRequest(group_id=group_id, report=report)
    ticket_res = bridge_client.create_ticket("http://testserver", create_req)

    # 5. Validate TicketResponse returned from bridge
    assert ticket_res.status == "created"
    assert ticket_res.sys_id == "sn-sys-tb-p1-001"
    assert ticket_res.number == "INC0010999"
    assert ticket_res.group_id == group_id
    assert "INC0010999" in ticket_res.url or "sn-sys-tb-p1-001" in ticket_res.url

    # 6. Verify complete ServiceNow call sequence
    # Sequence: 1. sys_user GET -> 2. incident POST (state=1) -> 3. incident PATCH (state=2) ->
    #           4. work_notes PATCH (4-bullet summary) -> 5. attachment POST -> 6. readback GET
    assert servicenow_state.post_incident_count == 1
    assert servicenow_state.patch_state_count == 1
    assert len(servicenow_state.attachments) == 1
    assert servicenow_state.attachments[0]["content_type"] == "text/html"

    # Verify 4-bullet executive summary in work notes
    assert len(servicenow_state.work_notes_appended) >= 1
    notes = servicenow_state.work_notes_appended[0]
    assert "[code]" in notes
    assert "<b>Problem:</b>" in notes
    assert "<b>Systems:</b>" in notes
    assert "<code>ata-inventory-api</code>" in notes
    assert "<b>Observations:</b>" in notes
    assert "<b>Recommended Actions:</b>" in notes

    # 7. Agent updates OKF frontmatter with ServiceNow ticket linkage atomically
    def ticket_link_fn(d: OKFDocument) -> OKFDocument:
        d.status = "ticketed"
        d.servicenow_sys_id = ticket_res.sys_id
        d.servicenow_number = ticket_res.number
        d.servicenow_url = ticket_res.url
        return d

    doc3 = update_okf_atomically(
        bucket_name=bucket_name,
        blob_name=blob_key,
        update_fn=ticket_link_fn,
        storage_client=mock_storage,
    )

    assert doc3.status == "ticketed"
    assert doc3.servicenow_sys_id == "sn-sys-tb-p1-001"
    assert doc3.servicenow_number == "INC0010999"
    assert doc3.servicenow_url == ticket_res.url
    assert blob.generation == 3

    # Verify OKF markdown content contains frontmatter and valid YAML
    raw_md = blob.download_as_text()
    parsed_doc = OKFDocument.from_markdown(raw_md)
    assert parsed_doc.servicenow_sys_id == "sn-sys-tb-p1-001"
    assert parsed_doc.servicenow_number == "INC0010999"
    assert parsed_doc.status == "ticketed"


def test_vertical_slice_idempotency(bridge_adapter, servicenow_state, test_client):
    """Idempotency: Second call to POST /instructions/ticket/create returns already_exists without creating a second incident."""
    group_id = "ata-backend-5xx-inventory-api-idempotency"
    agent_cfg = AgentConfig(is_local=True)
    bridge_client = BridgeClient(config=agent_cfg, http_client=test_client)

    report = InvestigationReport(
        group_id=group_id,
        filename=f"incident-{group_id}.md",
        summary="Initial incident report",
        observations="Observations on 5xx errors",
        recommended_actions=["Action 1"],
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api"],
        priority="P1",
    )
    req = CreateTicketRequest(group_id=group_id, report=report)

    # First call: creates ticket
    res1 = bridge_client.create_ticket("http://testserver", req)
    assert res1.status == "created"
    assert res1.sys_id == "sn-sys-tb-p1-001"
    assert res1.number == "INC0010999"
    assert servicenow_state.post_incident_count == 1

    # Second call for the same group_id: idempotency check returns already_exists
    res2 = bridge_client.create_ticket("http://testserver", req)
    assert res2.status == "already_exists"
    assert res2.sys_id == res1.sys_id
    assert res2.number == res1.number
    assert res2.group_id == group_id

    # CRITICAL: No second POST to /api/now/table/incident was made!
    assert servicenow_state.post_incident_count == 1


def test_vertical_slice_concurrency_precondition_retry(mock_storage):
    """Concurrency / Precondition: Concurrent writes triggering PreconditionFailed retry without data loss or clobbering."""
    bucket_name = "test-concurrency-bucket"
    group_id = "ata-concurrent-race-group"
    blob_key = f"knowledge/incident-manager/incident-{group_id}.md"

    # Initialize empty document
    update_okf_atomically(
        bucket_name=bucket_name,
        blob_name=blob_key,
        update_fn=lambda d: setattr(d, "status", "open") or d,
        storage_client=mock_storage,
    )

    # 10 worker threads concurrently appending alerts to the same document
    num_workers = 10
    worker_ids = [f"alert-concurrent-{i}" for i in range(num_workers)]

    def worker_append(alert_id: str):
        def add_fn(doc: OKFDocument) -> OKFDocument:
            doc.add_alert({
                "incident_id": alert_id,
                "policy_name": "ata-concurrency-policy",
                "resource_name": "ata-inventory-api",
                "state": "open",
            })
            return doc

        return update_okf_atomically(
            bucket_name=bucket_name,
            blob_name=blob_key,
            update_fn=add_fn,
            max_retries=10,
            storage_client=mock_storage,
        )

    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(worker_append, worker_ids))

    # Verify final document has all 10 alerts present without loss
    blob = mock_storage.bucket(bucket_name).blob(blob_key)
    final_doc = OKFDocument.from_markdown(blob.download_as_text())

    assert len(final_doc.alerts) == num_workers
    found_ids = {a["incident_id"] for a in final_doc.alerts}
    assert found_ids == set(worker_ids)


def test_bridge_client_retries_transient_5xx():
    """BridgeClient retries 5xx server errors and network errors up to 3 times with exponential backoff."""
    attempts = 0

    def mock_transport_fn(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, json={"detail": "Service Unavailable"})
        return httpx.Response(
            200,
            json={
                "status": "created",
                "sys_id": "sn-retry-5xx-01",
                "number": "INC0010888",
                "url": "https://service-now.com/nav_to.do?uri=incident.do?sys_id=sn-retry-5xx-01",
                "group_id": "grp_retry_test",
            },
        )

    client = httpx.Client(transport=httpx.MockTransport(mock_transport_fn))
    bridge_client = BridgeClient(
        config=AgentConfig(is_local=True),
        http_client=client,
        backoff_factor=0.01,
    )

    req = CreateTicketRequest(
        group_id="grp_retry_test",
        report=InvestigationReport(
            group_id="grp_retry_test",
            summary="Retry test",
            observations="Retry observations",
            origin="ata-orders-api",
            blast_radius=["ata-orders-api"],
        ),
    )

    res = bridge_client.create_ticket("http://mockbridge", req)
    assert res.status == "created"
    assert res.number == "INC0010888"
    assert attempts == 3


def test_bridge_client_4xx_does_not_retry():
    """BridgeClient raises immediately without retry on 4xx client errors."""
    attempts = 0

    def mock_transport_fn(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(403, json={"detail": "Forbidden caller identity"})

    client = httpx.Client(transport=httpx.MockTransport(mock_transport_fn))
    bridge_client = BridgeClient(
        config=AgentConfig(is_local=True),
        http_client=client,
        backoff_factor=0.01,
    )

    req = CreateTicketRequest(
        group_id="grp_403_test",
        report=InvestigationReport(
            group_id="grp_403_test",
            summary="403 test",
            observations="403 observations",
            origin="ata-orders-api",
            blast_radius=["ata-orders-api"],
        ),
    )

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        bridge_client.create_ticket("http://mockbridge", req)

    assert exc_info.value.response.status_code == 403
    assert attempts == 1  # Did NOT retry


def test_local_filesystem_atomicity(tmp_path):
    """Local filesystem fallback with file locking and atomic rename preserves concurrent writes."""
    store = OKFStore(storage_path=tmp_path, is_local=True)
    group_id = "grp_local_test_100"

    # Initialize
    store.update_atomically(group_id, lambda d: setattr(d, "status", "open") or d)

    num_threads = 8
    alert_ids = [f"local-alert-{i}" for i in range(num_threads)]

    def local_append(aid: str):
        def add(d: OKFDocument) -> OKFDocument:
            d.add_alert({"incident_id": aid, "state": "open"})
            return d
        return store.update_atomically(group_id, add)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(local_append, alert_ids))

    final_doc = store.read(group_id)
    assert final_doc is not None
    assert len(final_doc.alerts) == num_threads
    found = {a["incident_id"] for a in final_doc.alerts}
    assert found == set(alert_ids)
