"""FastMCP Server definitions, tools, and ASGI application assembly."""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from app.auth import GoogleAuthMiddleware
from app.config import Settings, get_settings
from app.indexer import DeploymentContextIndexer

logger = logging.getLogger(__name__)

# Global indexer instance accessed by FastMCP tools
_global_indexer: DeploymentContextIndexer = DeploymentContextIndexer()


def get_indexer() -> DeploymentContextIndexer:
    """Get the active deployment context indexer instance."""
    return _global_indexer


def set_indexer(indexer: DeploymentContextIndexer) -> None:
    """Set the active deployment context indexer instance."""
    global _global_indexer
    _global_indexer = indexer


def create_mcp_server(indexer: Optional[DeploymentContextIndexer] = None) -> FastMCP:
    """Factory creating a new FastMCP server instance configured with deployment context tools."""
    target_indexer = indexer if indexer is not None else get_indexer()

    server = FastMCP(
        name="mcp-deploy-context-stub",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    @server.tool()
    def lookup_alert_context(
        target_ip: Optional[str] = None,
        check_id: Optional[str] = None,
        service_hint: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Resolve alert target indicators (IP, check ID, service hint) to topology and metadata."""
        idx = target_indexer if target_indexer is not None else get_indexer()
        res = idx.lookup_alert_context(
            target_ip=target_ip,
            check_id=check_id,
            service_hint=service_hint,
        )
        return res.model_dump()

    @server.tool()
    def get_recent_deployments(
        workload: Optional[str] = None,
        cluster: Optional[str] = None,
        limit: int = 5,
        workload_name: Optional[str] = None,
        cluster_name: Optional[str] = None,
        namespace: Optional[str] = None,
        start_time: Optional[str] = None,
        end_time: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Fetch recent deployments, scaling actions, and rollout patches for a workload."""
        idx = target_indexer if target_indexer is not None else get_indexer()
        res = idx.get_recent_deployments(
            workload=workload,
            cluster=cluster,
            limit=limit,
            workload_name=workload_name,
            cluster_name=cluster_name,
            namespace=namespace,
            start_time=start_time,
            end_time=end_time,
        )
        return res.model_dump()

    return server


# Default module-level instance for direct imports
mcp = create_mcp_server(_global_indexer)


def create_app(
    indexer: Optional[DeploymentContextIndexer] = None,
    settings: Optional[Settings] = None,
    mcp_server: Optional[FastMCP] = None,
) -> Starlette:
    """Create and configure the Starlette ASGI application with Streamable HTTP and Auth."""
    active_settings = settings or get_settings()

    if indexer is not None:
        set_indexer(indexer)

    server = mcp_server or create_mcp_server(indexer)
    app = server.streamable_http_app()

    # Health check endpoint for Cloud Run and container liveness probes
    async def health_check(_request: Request) -> Response:
        return JSONResponse(
            {
                "status": "ok",
                "service": "mcp-deploy-context-stub",
                "version": "0.1.0",
            }
        )

    app.add_route("/health", health_check, methods=["GET"])

    # Attach Google OIDC authentication middleware
    app.add_middleware(GoogleAuthMiddleware, settings=active_settings)

    return app
