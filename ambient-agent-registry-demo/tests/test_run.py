"""Unit and integration tests for ADK native /run endpoint and provenance."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from app.main import (
    set_agent_loader,
    set_skills_loader,
    set_tool_registry_manager,
)
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager


def test_run_endpoint_minimal(test_client: TestClient, valid_skill_zip: bytes) -> None:
    """Verify POST /run executes turn and returns native ADK list[Event] execution trace."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/ambient-privilege-audit-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    registry = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=registry)
    set_skills_loader(loader)

    payload = {
        "app_name": "ambient_agent",
        "user_id": "demo-user",
        "session_id": "test-session-1",
        "new_message": {
            "role": "user",
            "parts": [{"text": "Compute 15 multiplied by 4, then add 10."}],
        },
    }
    response = test_client.post("/run", json=payload)

    assert response.status_code == 200
    events = response.json()
    assert isinstance(events, list)
    assert len(events) >= 3

    # Verify function calls and responses exist in event stream
    function_calls = [
        p["functionCall"]["name"]
        for ev in events
        for p in ev.get("content", {}).get("parts", [])
        if "functionCall" in p
    ]
    assert "multiply" in function_calls
    assert "add" in function_calls

    function_responses = [
        p["functionResponse"]["name"]
        for ev in events
        for p in ev.get("content", {}).get("parts", [])
        if "functionResponse" in p
    ]
    assert "multiply" in function_responses
    assert "add" in function_responses

    # Verify model final text response
    text_responses = [
        p["text"]
        for ev in events
        for p in ev.get("content", {}).get("parts", [])
        if "text" in p
    ]
    assert any("70" in t for t in text_responses)


def test_run_endpoint_mcp_failure_returns_http_502_zero_fallback(
    test_client: TestClient,
    valid_skill_zip: bytes,
) -> None:
    """Verify that MCP server failure produces HTTP 502/503 and NEVER computes fallback locally."""

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/ambient-privilege-audit-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    reg_transport = httpx.MockTransport(mock_registry_handler)
    mock_reg_http = httpx.AsyncClient(transport=reg_transport)
    client = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_reg_http,
    )
    loader = SkillsLoader(registry=client)
    set_skills_loader(loader)

    # Point tool manager to an unreachable / failing MCP service (HTTP 503)
    failing_mcp_transport = httpx.MockTransport(
        lambda req: httpx.Response(503, text="Service Unavailable")
    )
    failing_tm = ToolRegistryManager(
        project_id="demo-project-id",
        location="us-central1",
        tool_service_url="http://failing-mcp-server/mcp",
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=failing_mcp_transport, base_url="http://failing-mcp-server", **kwargs
        ),
    )
    set_tool_registry_manager(failing_tm)

    # Reset agent loader to pick up new managers
    set_agent_loader(None)

    # Evict cached runners
    inv_res = test_client.post("/admin/cache/invalidate")
    assert inv_res.status_code == 200

    response = test_client.post(
        "/run",
        json={
            "app_name": "ambient_agent",
            "user_id": "demo-user",
            "session_id": "test-session-failing",
            "new_message": {
                "role": "user",
                "parts": [{"text": "Compute 15 multiplied by 4, then add 10."}],
            },
        },
    )

    # MUST return 502/503 Bad Gateway, NEVER a computed fallback answer
    assert response.status_code in (502, 503)
    detail = response.json().get("detail", "").lower()
    assert "mcp" in detail or "tool" in detail or "failure" in detail or "connection" in detail


def test_run_endpoint_skill_not_found(test_client: TestClient) -> None:
    """Verify POST /run returns 404 when requested skill cannot be resolved."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=404, json={"error": "Skill not found"})

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    registry = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=registry)
    set_skills_loader(loader)
    set_agent_loader(None)

    # Invalidate cache
    test_client.post("/admin/cache/invalidate")

    response = test_client.post(
        "/run",
        json={
            "app_name": "ambient_agent",
            "user_id": "demo-user",
            "session_id": "test-session-404",
            "new_message": {"role": "user", "parts": [{"text": "Hello"}]},
        },
    )
    assert response.status_code == 404
    assert "not found" in response.json().get("detail", "").lower()


def test_run_endpoint_unknown_agent_returns_404(test_client: TestClient) -> None:
    """Verify POST /run returns 404 when an unknown agent name is supplied."""
    response = test_client.post(
        "/run",
        json={
            "app_name": "non_existent_agent",
            "user_id": "demo-user",
            "session_id": "test-session-unknown",
            "new_message": {"role": "user", "parts": [{"text": "Hello"}]},
        },
    )
    assert response.status_code == 404


def test_tool_guardrail_blocks_undeclared_power_tool_in_v1(
    test_client: TestClient,
    valid_skill_zip: bytes,
) -> None:
    """Security Guardrail: Attempting to invoke power on v1 (undeclared tool) fails safely."""

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/ambient-privilege-audit-skill"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill"
                    ),
                    "defaultRevision": (
                        "projects/demo-project-id/locations/us-central1/"
                        "skills/ambient-privilege-audit-skill/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=valid_skill_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_registry_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    client = GCPSkillRegistryClient(
        project_id="demo-project-id",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=client)
    set_skills_loader(loader)
    set_agent_loader(None)
    test_client.post("/admin/cache/invalidate")

    # In Revision 1, allowed_tools is 'add multiply' (power is NOT declared)
    response = test_client.post(
        "/run",
        json={
            "app_name": "ambient_agent",
            "user_id": "demo-user",
            "session_id": "test-session-guardrail",
            "new_message": {
                "role": "user",
                "parts": [{"text": "Compute 2 to the power of 8."}],
            },
        },
    )

    # In mock mode, undeclared tool invocation raises ValueError caught as 400/500/502
    assert response.status_code in (400, 500, 502)
