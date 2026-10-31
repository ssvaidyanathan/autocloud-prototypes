"""Unit tests for FastMCP server and Starlette ASGI routes."""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from app.config import Settings
from app.indexer import DeploymentContextIndexer
from app.server import create_app, create_mcp_server, mcp


@pytest.mark.asyncio
async def test_fastmcp_list_tools() -> None:
    """Verify registered FastMCP tools."""
    tools = await mcp.list_tools()
    tool_names = {t.name for t in tools}
    assert "lookup_alert_context" in tool_names
    assert "get_recent_deployments" in tool_names


@pytest.mark.asyncio
async def test_fastmcp_call_tool_lookup_alert_context(
    populated_indexer: DeploymentContextIndexer,
) -> None:
    """Verify direct FastMCP tool execution for lookup_alert_context."""
    server = create_mcp_server(populated_indexer)

    # 1. Matched lookup
    _content, res = await server.call_tool(
        "lookup_alert_context",
        {"check_id": "sample-check-1"},
    )
    result_data = res.get("result", res)
    assert result_data["found"] is True
    assert result_data["context"]["service_name"] == "sample-workload"
    assert result_data["context"]["cluster_name"] == "customer-cluster-1"

    # 2. Unmatched lookup
    _content, res_unmatched = await server.call_tool(
        "lookup_alert_context",
        {"check_id": "non-existent-id"},
    )
    unmatched_data = res_unmatched.get("result", res_unmatched)
    assert unmatched_data["found"] is False
    assert unmatched_data["context"] is None


@pytest.mark.asyncio
async def test_fastmcp_call_tool_get_recent_deployments(
    populated_indexer: DeploymentContextIndexer,
) -> None:
    """Verify direct FastMCP tool execution for get_recent_deployments."""
    server = create_mcp_server(populated_indexer)

    # 1. Matched query
    _content, res = await server.call_tool(
        "get_recent_deployments",
        {"workload": "sample-workload", "limit": 5},
    )
    result_data = res.get("result", res)
    assert result_data["total_count"] == 1
    assert len(result_data["deployments"]) == 1
    assert result_data["deployments"][0]["revision"] == "89"

    # 2. Unmatched query
    _content, res_empty = await server.call_tool(
        "get_recent_deployments",
        {"workload": "unknown-service"},
    )
    empty_data = res_empty.get("result", res_empty)
    assert empty_data["total_count"] == 0
    assert empty_data["deployments"] == []


def test_asgi_health_endpoint(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify /health endpoint returns 200 without requiring authentication."""
    app = create_app(
        indexer=populated_indexer,
        settings=Settings(
            AUTH_DISABLED=False,
            AUTHORIZED_CALLER_SERVICE_ACCOUNTS="caller@example.com",
        ),
    )

    with TestClient(app) as client:
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["service"] == "mcp-deploy-context-stub"


def test_asgi_streamable_http_initialize(populated_indexer: DeploymentContextIndexer) -> None:
    """Verify Streamable HTTP JSON-RPC initialize request over /mcp."""
    app = create_app(
        indexer=populated_indexer,
        settings=Settings(AUTH_DISABLED=True),
    )

    with TestClient(app) as client:
        resp = client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {},
                    "clientInfo": {"name": "test-runner", "version": "1.0.0"},
                },
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
        )
        assert resp.status_code == 200
        assert "serverInfo" in resp.text
        assert "mcp-deploy-context-stub" in resp.text
