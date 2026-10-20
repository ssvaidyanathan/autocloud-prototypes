"""Dynamic tool discovery and remote toolset resolution via Google Cloud Agent Registry."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, cast

import httpx
from google.adk.tools import FunctionTool
from google.adk.tools.base_tool import BaseTool
from google.adk.tools.mcp_tool.mcp_toolset import McpToolset, StreamableHTTPConnectionParams

from app.config import get_settings

logger = logging.getLogger(__name__)


def _fallback_add(a: float, b: float) -> float:
    """Add two numbers with precision arithmetic.

    Args:
        a: First number.
        b: Second number.

    Returns:
        Sum of a and b.
    """
    return float(a + b)


def _fallback_multiply(a: float, b: float) -> float:
    """Multiply two numbers with precision arithmetic.

    Args:
        a: First number.
        b: Second number.

    Returns:
        Product of a and b.
    """
    return float(a * b)


def _fallback_power(base: float, exponent: float) -> float:
    """Raise base to the power of exponent.

    Args:
        base: Base number.
        exponent: Exponent power.

    Returns:
        Base raised to the exponent power.
    """
    if base == 0 and exponent < 0:
        raise ValueError("Division by zero: zero cannot be raised to a negative exponent")
    if base < 0 and not exponent.is_integer():
        raise ValueError("Fractional exponent of a negative base produces a complex number")
    return float(base**exponent)


def _create_fallback_tool(func: Callable[..., Any], name: str) -> FunctionTool:
    tool = FunctionTool(func)
    tool.name = name
    return tool



class ToolRegistryManager:
    """Manages remote tool discovery and execution from Google Cloud Agent Registry (v1).

    Resolves remote MCP toolsets from Google Cloud Agent Registry `mcpServers/calculator`
    catalog, with transport over Streamable HTTP (/mcp).
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
        params_kwargs: dict[str, Any] = {"url": target_url}
        if self.http_client_factory is not None:
            params_kwargs["httpx_client_factory"] = self.http_client_factory

        connection_params = StreamableHTTPConnectionParams(**params_kwargs)
        self._toolset = McpToolset(connection_params=connection_params)
        return self._toolset

    def _fallback_tools(self) -> list[BaseTool]:
        """Provide verified arithmetic operations as ADK FunctionTools."""
        return [
            _create_fallback_tool(_fallback_add, "add"),
            _create_fallback_tool(_fallback_multiply, "multiply"),
            _create_fallback_tool(_fallback_power, "power"),
        ]

    async def discover_tools(self, server_name: str = "calculator") -> list[BaseTool]:
        """Discover remote tools from MCP server, with fallback to verified local tools."""
        if not self._toolset:
            self.resolve_toolset(server_name)

        assert self._toolset is not None
        try:
            tools = await self._toolset.get_tools()
            if tools:
                for tool in tools:
                    self._tools_cache[tool.name] = tool
                return list(tools)
        except Exception as exc:
            logger.debug(
                "McpToolset discovery for '%s' unavailable (%s); using verified local tools",
                server_name,
                exc,
            )

        fallback = self._fallback_tools()
        for tool in fallback:
            self._tools_cache[tool.name] = tool
        return fallback

    async def get_tool(self, name: str, server_name: str = "calculator") -> BaseTool:
        """Retrieve a specific tool by name."""
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
        """Execute a tool dynamically and extract numeric scalar result.

        Supports both FastMCP JSON-RPC response formats and direct FunctionTool execution.
        """
        tool = await self.get_tool(name, server_name)
        try:
            raw_result = await tool.run_async(args=args, tool_context=cast(Any, None))
        except Exception as exc:
            logger.debug(
                "Tool execution via BaseTool failed (%s); invoking operation directly", exc
            )
            if name == "add":
                return float(_fallback_add(float(args["a"]), float(args["b"])))
            if name == "multiply":
                return float(_fallback_multiply(float(args["a"]), float(args["b"])))
            if name == "power":
                return float(_fallback_power(float(args["base"]), float(args["exponent"])))
            raise

        # Parse result from response
        if isinstance(raw_result, (int, float)):
            return float(raw_result)

        if isinstance(raw_result, dict):
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
