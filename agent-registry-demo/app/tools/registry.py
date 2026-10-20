"""Dynamic tool discovery and remote toolset resolution via Google Cloud Agent Registry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

from app.config import get_settings

logger = logging.getLogger(__name__)


class ToolRegistryManager:
    """Manages remote tool discovery and execution from Google Cloud Agent Registry (v1).

    Resolves remote MCP toolsets from Google Cloud Agent Registry `mcpServers/calculator`
    catalog, with transport over Streamable HTTP (/mcp).
    Strictly prohibits local fallback tools or local computation.
    """

    def __init__(
        self,
        *,
        project_id: str | None = None,
        location: str | None = None,
        base_url: str | None = None,
        tool_service_url: str | None = None,
        http_client_factory: Callable[..., httpx.AsyncClient] | None = None,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        settings = get_settings()
        self.project_id = project_id or settings.project_id
        self.location = location or settings.region
        self.base_url = (base_url or settings.tools_registry_endpoint).rstrip("/")
        self.tool_service_url = tool_service_url or settings.calculator_service_url
        self.http_client_factory = http_client_factory
        self._custom_http_client = http_client
        self._toolset: McpToolset | None = None
        self._tools_cache: dict[str, BaseTool] = {}

    @property
    def endpoint_url(self) -> str:
        """Return configured or resolved MCP server endpoint URL."""
        return self.tool_service_url

    async def query_mcp_server(self, server_name: str = "calculator") -> dict[str, Any]:
        """Query Agent Registry v1 mcpServers catalog for endpoint metadata.

        Returns:
            Dictionary containing mcpServer resource data from Agent Registry.
        """
        url = (
            f"{self.base_url}/projects/{self.project_id}/"
            f"locations/{self.location}/mcpServers/{server_name}"
        )
        if self._custom_http_client:
            resp = await self._custom_http_client.get(url)
            resp.raise_for_status()
            return resp.json()

        async with httpx.AsyncClient() as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return resp.json()

    def resolve_toolset(
        self,
        server_name: str = "calculator",
        *,
        url: str | None = None,
    ) -> McpToolset:
        """Resolve and instantiate McpToolset configured with Streamable HTTP transport."""
        target_url = url or self.tool_service_url
        params_kwargs: dict[str, Any] = {"url": target_url, "timeout": 30.0}
        if self.http_client_factory is not None:
            params_kwargs["httpx_client_factory"] = self.http_client_factory

        connection_params = StreamableHTTPConnectionParams(**params_kwargs)
        self._toolset = McpToolset(connection_params=connection_params)
        return self._toolset

    async def discover_tools(self, server_name: str = "calculator") -> list[BaseTool]:
        """Discover remote tools from MCP server. Fails loudly if MCP server is unreachable."""
        if not self._toolset:
            self.resolve_toolset(server_name)

        assert self._toolset is not None
        try:
            tools = await self._toolset.get_tools()
            if not tools:
                raise RuntimeError(
                    f"Remote MCP server '{server_name}' returned no tools at "
                    f"'{self.tool_service_url}'"
                )
            for tool in tools:
                self._tools_cache[tool.name] = tool
            return list(tools)
        except Exception as exc:
            logger.error(
                "McpToolset discovery for '%s' failed at '%s': %s",
                server_name,
                self.tool_service_url,
                exc,
            )
            raise RuntimeError(
                f"Failed to discover tools from remote MCP server '{server_name}' at "
                f"'{self.tool_service_url}': {exc}"
            ) from exc

    async def get_tool(self, name: str, server_name: str = "calculator") -> BaseTool:
        """Retrieve a specific tool by name from remote MCP catalog."""
        if name in self._tools_cache:
            return self._tools_cache[name]

        await self.discover_tools(server_name)
        if name in self._tools_cache:
            return self._tools_cache[name]

        raise ValueError(f"Tool '{name}' not found in registry for server '{server_name}'")

    async def execute_tool(
        self,
        name: str,
        args: dict[str, Any],
        server_name: str = "calculator",
    ) -> float:
        """Execute a tool dynamically via remote MCP and extract numeric scalar result.

        Never falls back to local computation.
        """
        tool = await self.get_tool(name, server_name)
        try:
            raw_result = await tool.run_async(args=args, tool_context=cast(Any, None))
        except Exception as exc:
            logger.error("Remote MCP execution failed for tool '%s': %s", name, exc)
            raise RuntimeError(f"Remote tool '{name}' execution failed: {exc}") from exc

        # Parse result from response
        if isinstance(raw_result, (int, float)):
            return float(raw_result)

        if isinstance(raw_result, dict):
            if raw_result.get("isError"):
                raise RuntimeError(f"Remote tool '{name}' returned error: {raw_result}")

            # Check structuredContent first
            structured = raw_result.get("structuredContent")
            if isinstance(structured, dict) and "result" in structured:
                return float(structured["result"])

            # Check content list
            content_list = raw_result.get("content")
            if isinstance(content_list, list) and len(content_list) > 0:
                first = content_list[0]
                if isinstance(first, dict) and "text" in first:
                    return float(first["text"])

            if "result" in raw_result:
                return float(raw_result["result"])

        return float(str(raw_result))
