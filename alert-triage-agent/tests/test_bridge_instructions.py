"""Unit tests for Bridge Instruction API Endpoints (/instructions/ticket/*).

Validates:
1. POST /instructions/ticket/create:
   - Valid OIDC token creates ticket and returns TicketResponse with status="created".
   - Idempotent: existing correlation_id returns status="already_exists" without creating duplicate ticket.
   - Rejects unauthenticated / unauthorized requests (401 / 403).
2. POST /instructions/ticket/work-note:
   - Appends work note to ServiceNow incident and returns ActionStatusResponse.
   - Rejects unauthenticated / unauthorized requests (401 / 403).
3. POST /instructions/ticket/update-report:
   - Updates ServiceNow incident table fields and HTML attachment, returning TicketResponse.
   - Rejects unauthenticated / unauthorized requests (401 / 403).
4. POST /instructions/ticket/resolve:
   - Sets state="6", close_code, and close_notes, returning ActionStatusResponse.
   - Rejects unauthenticated / unauthorized requests (401 / 403).
5. GET /instructions/ticket/query/{group_id}:
   - Returns existing ticket if correlation_id found.
   - Returns 404 if not found.
   - Rejects unauthenticated / unauthorized requests (401 / 403).
"""

import json
from typing import Any
import httpx
import pytest
from starlette.testclient import TestClient

from bridge.app.adapters.servicenow import ServiceNowAdapter, ServiceNowIncidentRef
from bridge.app.config import BridgeConfig, reset_settings
from bridge.app.contracts.instructions import (
    CreateTicketRequest,
    ResolveTicketRequest,
    UpdateReportRequest,
    WorkNoteRequest,
)
from bridge.app.contracts.report import InvestigationReport, ProbableRootCause
from bridge.app.main import app, set_servicenow_adapter


AUTH_HEADERS = {"Authorization": "Bearer valid-agent-token"}


@pytest.fixture
def test_config():
    config = BridgeConfig(
        project_id="cloud-assist-fde-2",
        name_prefix="ata",
        region="us-west1",
        servicenow_instance_url="https://mock.service-now.com",
        servicenow_username="admin",
        servicenow_password="password",
        incidents_bucket="cloud-assist-fde-2-ata-incidents",
        is_local=True,
    )
    reset_settings(config)
    yield config
    reset_settings(None)


@pytest.fixture
def mock_adapter(test_config):
    posted_notes = []
    created_incidents = {}
    resolved_incidents = []

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        method = request.method

        if method == "GET" and "/api/now/table/sys_user" in url_str:
            return httpx.Response(
                200,
                json={"result": [{"sys_id": "usr-sn-admin-01", "user_name": "admin"}]},
            )

        if method == "GET" and "/api/now/table/incident" in url_str and "sysparm_query" in url_str:
            for cid, inc in created_incidents.items():
                if cid in url_str:
                    return httpx.Response(200, json={"result": [{"sys_id": inc["sys_id"], "number": inc["number"]}]})
            return httpx.Response(200, json={"result": []})

        if method == "GET" and "/api/now/table/incident/" in url_str:
            sys_id = url_str.split("/api/now/table/incident/")[-1].split("?")[0]
            if sys_id in created_incidents:
                return httpx.Response(200, json={"result": created_incidents[sys_id]})
            return httpx.Response(404)

        if method == "POST" and "/api/now/table/incident" in url_str:
            body = json.loads(request.read())
            sys_id = f"sn-sys-{len(created_incidents) + 1}"
            number = f"INC00{10000 + len(created_incidents)}"
            rec = {
                "sys_id": sys_id,
                "number": number,
                "state": body.get("state", "1"),
                "correlation_id": body.get("correlation_id", ""),
                "short_description": body.get("short_description", ""),
            }
            created_incidents[sys_id] = rec
            created_incidents[rec["correlation_id"]] = rec
            return httpx.Response(201, json={"result": rec})

        if method == "PATCH" and "/api/now/table/incident/" in url_str:
            sys_id = url_str.split("/api/now/table/incident/")[-1].split("?")[0]
            body = json.loads(request.read())
            if sys_id in created_incidents:
                created_incidents[sys_id].update(body)
            if "work_notes" in body:
                posted_notes.append(body["work_notes"])
            if body.get("state") == "6":
                resolved_incidents.append({"sys_id": sys_id, "body": body})
            return httpx.Response(200, json={"result": created_incidents.get(sys_id, {"sys_id": sys_id})})

        if method == "POST" and "/api/now/attachment/file" in url_str:
            return httpx.Response(201, json={"result": {"sys_id": "att-mock-01"}})

        return httpx.Response(404)

    client = httpx.Client(transport=httpx.MockTransport(mock_handler))
    adapter = ServiceNowAdapter(config=test_config, http_client=client)
    adapter._posted_notes = posted_notes  # type: ignore[attr-defined]
    adapter._created_incidents_mock = created_incidents  # type: ignore[attr-defined]
    adapter._resolved_incidents_mock = resolved_incidents  # type: ignore[attr-defined]
    set_servicenow_adapter(adapter)
    yield adapter
    set_servicenow_adapter(None)


@pytest.fixture
def client(test_config, mock_adapter):
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def sample_report():
    return InvestigationReport(
        group_id="grp-instructions-test-01",
        summary="Test incident for bridge instruction endpoints",
        observations="Latency spike across inventory backend",
        probable_root_cause=ProbableRootCause(text="Backend latency", confidence=0.91),
        origin="ata-inventory-api",
        blast_radius=["ata-inventory-api", "ata-orders-api"],
        priority="P1",
    )


# ==============================================================================
# 1. POST /instructions/ticket/create Tests
# ==============================================================================

def test_create_ticket_instruction_success(client, sample_report):
    """Assert creating a ticket returns status='created' and valid TicketResponse."""
    req = CreateTicketRequest(group_id="grp-instructions-test-01", report=sample_report)
    resp = client.post("/instructions/ticket/create", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "created"
    assert data["group_id"] == "grp-instructions-test-01"
    assert data["sys_id"].startswith("sn-sys-")
    assert data["number"].startswith("INC00")
    assert "service-now.com" in data["url"]


def test_create_ticket_instruction_idempotency(client, sample_report):
    """Assert subsequent create calls for same group_id return status='already_exists' without creating duplicates."""
    req = CreateTicketRequest(group_id="grp-instructions-test-01", report=sample_report)
    # First call
    resp1 = client.post("/instructions/ticket/create", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp1.status_code == 200
    data1 = resp1.json()
    assert data1["status"] == "created"

    # Second call (idempotency check)
    resp2 = client.post("/instructions/ticket/create", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp2.status_code == 200
    data2 = resp2.json()
    assert data2["status"] == "already_exists"
    assert data2["sys_id"] == data1["sys_id"]
    assert data2["number"] == data1["number"]
    assert data2["group_id"] == data1["group_id"]


def test_create_ticket_instruction_auth_failure(client, sample_report):
    """Assert unauthenticated, invalid, or unauthorized tokens fail with 401 or 403."""
    req = CreateTicketRequest(group_id="grp-instructions-test-01", report=sample_report)
    body = req.model_dump(mode="json")

    # Missing header -> 401
    assert client.post("/instructions/ticket/create", json=body).status_code == 401

    # Invalid token -> 401
    assert client.post(
        "/instructions/ticket/create",
        headers={"Authorization": "Bearer invalid-token"},
        json=body,
    ).status_code == 401

    # Wrong audience -> 403
    assert client.post(
        "/instructions/ticket/create",
        headers={"Authorization": "Bearer wrong-audience-token"},
        json=body,
    ).status_code == 403

    # Unauthorized SA -> 403
    assert client.post(
        "/instructions/ticket/create",
        headers={"Authorization": "Bearer unauthorized-sa-token"},
        json=body,
    ).status_code == 403


# ==============================================================================
# 2. POST /instructions/ticket/work-note Tests
# ==============================================================================

def test_work_note_instruction_success(client, mock_adapter):
    """Assert appending a work note returns ActionStatusResponse and calls adapter."""
    req = WorkNoteRequest(
        sys_id="sn-sys-work-note-01",
        work_note="[code]<b>Correlated Alert Ingested:</b> Late alert attached[/code]",
        group_id="grp-work-note-01",
    )
    resp = client.post("/instructions/ticket/work-note", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "success"
    assert data["sys_id"] == "sn-sys-work-note-01"
    assert data["group_id"] == "grp-work-note-01"
    assert len(mock_adapter._posted_notes) >= 1
    assert "Late alert attached" in mock_adapter._posted_notes[-1]


def test_work_note_instruction_auth_failure(client):
    """Assert work note endpoint enforces authentication."""
    req = WorkNoteRequest(sys_id="sn-sys-1", work_note="Note")
    assert client.post("/instructions/ticket/work-note", json=req.model_dump(mode="json")).status_code == 401


# ==============================================================================
# 3. POST /instructions/ticket/update-report Tests
# ==============================================================================

def test_update_report_instruction_success(client, sample_report):
    """Assert updating a report returns TicketResponse with status='updated'."""
    req = UpdateReportRequest(
        sys_id="sn-sys-update-01",
        group_id="grp-instructions-test-01",
        report=sample_report,
    )
    resp = client.post("/instructions/ticket/update-report", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "updated"
    assert data["sys_id"] == "sn-sys-update-01"
    assert data["group_id"] == "grp-instructions-test-01"


def test_update_report_instruction_auth_failure(client, sample_report):
    """Assert update report endpoint enforces authentication."""
    req = UpdateReportRequest(
        sys_id="sn-sys-update-01",
        group_id="grp-instructions-test-01",
        report=sample_report,
    )
    assert client.post("/instructions/ticket/update-report", json=req.model_dump(mode="json")).status_code == 401


# ==============================================================================
# 4. POST /instructions/ticket/resolve Tests
# ==============================================================================

def test_resolve_ticket_instruction_success(client, mock_adapter):
    """Assert resolving a ticket calls adapter and returns ActionStatusResponse."""
    req = ResolveTicketRequest(
        sys_id="sn-sys-resolve-01",
        group_id="grp-resolve-01",
        close_notes="Resolved: rolled back container revision",
        close_code="Solved (Permanently)",
    )
    resp = client.post("/instructions/ticket/resolve", headers=AUTH_HEADERS, json=req.model_dump(mode="json"))
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "resolved"
    assert data["sys_id"] == "sn-sys-resolve-01"
    assert data["group_id"] == "grp-resolve-01"

    # Verify adapter called with state=6 and close_code
    assert len(mock_adapter._resolved_incidents_mock) == 1
    res_rec = mock_adapter._resolved_incidents_mock[0]
    assert res_rec["sys_id"] == "sn-sys-resolve-01"
    assert res_rec["body"]["state"] == "6"
    assert res_rec["body"]["close_code"] == "Solved (Permanently)"
    assert res_rec["body"]["close_notes"] == "Resolved: rolled back container revision"


def test_resolve_ticket_instruction_auth_failure(client):
    """Assert resolve endpoint enforces authentication."""
    req = ResolveTicketRequest(
        sys_id="sn-sys-1",
        group_id="grp-1",
        close_notes="Notes",
    )
    assert client.post("/instructions/ticket/resolve", json=req.model_dump(mode="json")).status_code == 401


# ==============================================================================
# 5. GET /instructions/ticket/query/{group_id} Tests
# ==============================================================================

def test_query_ticket_instruction_found(client, mock_adapter):
    """Assert query returns existing ticket with status='found'."""
    # Pre-populate incident in mock adapter
    ref = ServiceNowIncidentRef(
        sys_id="sn-sys-query-99",
        number="INC0099999",
        url="https://mock.service-now.com/nav_to.do?uri=incident.do?sys_id=sn-sys-query-99",
    )
    mock_adapter._created_incidents = {"grp-found-01": ref}

    resp = client.get("/instructions/ticket/query/grp-found-01", headers=AUTH_HEADERS)
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "found"
    assert data["sys_id"] == "sn-sys-query-99"
    assert data["number"] == "INC0099999"
    assert data["group_id"] == "grp-found-01"


def test_query_ticket_instruction_not_found(client):
    """Assert query returns 404 if ticket does not exist for group_id."""
    resp = client.get("/instructions/ticket/query/non-existent-group", headers=AUTH_HEADERS)
    assert resp.status_code == 404
    assert "not found" in resp.json()["detail"].lower()


def test_query_ticket_instruction_auth_failure(client):
    """Assert query endpoint enforces authentication."""
    assert client.get("/instructions/ticket/query/any-group").status_code == 401
