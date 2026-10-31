"""Standalone Deployment Context MCP Server Stub Prototype."""

from app.config import Settings, get_settings
from app.indexer import DeploymentContextIndexer
from app.models import (
    AlertContext,
    DeploymentEvent,
    GetRecentDeploymentsRequest,
    GetRecentDeploymentsResponse,
    LookupAlertContextRequest,
    LookupAlertContextResponse,
)
from app.server import create_app, mcp

__all__ = [
    "AlertContext",
    "DeploymentContextIndexer",
    "DeploymentEvent",
    "GetRecentDeploymentsRequest",
    "GetRecentDeploymentsResponse",
    "LookupAlertContextRequest",
    "LookupAlertContextResponse",
    "Settings",
    "create_app",
    "get_settings",
    "mcp",
]
