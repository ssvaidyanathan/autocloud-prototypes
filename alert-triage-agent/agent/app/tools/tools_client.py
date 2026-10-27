"""Client for the deployed ata-tools FastMCP service (MCP streamable HTTP, JSON-RPC over httpx).

ata-tools serves ``mcp.http_app(stateless_http=True, json_response=True)``: every request is
self-contained, so a tool call is one ``POST <TOOLS_URL>/mcp`` with a JSON-RPC ``tools/call``
body and a JSON response; no ``initialize`` handshake or session id is needed. Plain httpx (an
existing agent dependency) is used instead of the ``fastmcp`` client, which would add fastmcp
and the MCP SDK to the agent image for one request/response call.

Authentication: a Google-signed ID token of the agent's service account whose audience is the
URL called (``TOOLS_URL``). Cloud Run IAM (``roles/run.invoker`` on ``<prefix>-tools``) checks
it, and the ata-tools AuthMiddleware can verify it again.

Every failure raises ``ToolsCallError`` with a short factual reason (stated in the ticket):
unreachable, timeout, non-2xx HTTP status, JSON-RPC error, tool error or a malformed result.
Transient failures (connection error, timeout, HTTP 429/5xx) are retried with bounded
exponential backoff first.
"""

import asyncio
from collections.abc import Awaitable, Callable
import json
import logging
import re
from typing import Any, Optional

import httpx

from ..bridge_client import fetch_id_token
from ..config import AgentConfig
from .mcp_toolset import read_only_tool_filter

logger = logging.getLogger(__name__)

# Streamable HTTP endpoint of a FastMCP http_app (fastmcp.settings.streamable_http_path).
MCP_PATH = "/mcp"
# Longest backoff between two attempts.
MAX_BACKOFF_SECONDS = 10.0
# Longest server-supplied error text kept in a reason.
MAX_REASON_DETAIL = 200
TRANSIENT_STATUS = {429, 500, 502, 503, 504}

_default_transport: Optional[httpx.AsyncBaseTransport] = None


def set_default_transport(transport: Optional[httpx.AsyncBaseTransport]) -> None:
    """Route ata-tools calls through ``transport`` (tests: httpx.MockTransport / ASGITransport)."""
    global _default_transport
    _default_transport = transport


class ToolsCallError(Exception):
    """An ata-tools call failed; ``reason`` is a short factual statement for the report."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class ToolsNotConfigured(ToolsCallError):
    """TOOLS_URL is not set (allowed only in local mode, where nothing is queried)."""


def _short(text: Any) -> str:
    """One line, HTML tags removed (Cloud Run error pages are HTML), at most MAX_REASON_DETAIL."""
    text = " ".join(re.sub(r"<[^>]*>", " ", str(text)).split())
    return text if len(text) <= MAX_REASON_DETAIL else text[: MAX_REASON_DETAIL - 3] + "..."


def _parse_body(resp: httpx.Response, service: str) -> dict[str, Any]:
    """The JSON-RPC message of a JSON (or single-event SSE) response."""
    text = resp.text
    if resp.headers.get("content-type", "").startswith("text/event-stream"):
        data = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
        text = data[-1] if data else ""
    try:
        body = json.loads(text)
    except ValueError as exc:
        raise ToolsCallError(f"{service} returned a malformed response (not JSON)") from exc
    if not isinstance(body, dict):
        raise ToolsCallError(f"{service} returned a malformed response (not a JSON-RPC object)")
    return body


def _tool_result(body: dict[str, Any], name: str, service: str) -> dict[str, Any]:
    """The structured result of a JSON-RPC tools/call response, or ToolsCallError."""
    if body.get("error") is not None:
        err = body["error"] if isinstance(body["error"], dict) else {"message": body["error"]}
        raise ToolsCallError(f"{service} MCP error {err.get('code')}: {_short(err.get('message', ''))}")
    result = body.get("result")
    if not isinstance(result, dict):
        raise ToolsCallError(f"{service} returned a malformed response (no JSON-RPC result)")
    content = result.get("content") if isinstance(result.get("content"), list) else []
    texts = [c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text"]
    if result.get("isError"):
        raise ToolsCallError(f"{service} tool {name} failed: {_short(' '.join(texts) or 'no detail')}")
    structured = result.get("structuredContent")
    if isinstance(structured, dict):
        return structured
    if texts:
        try:
            parsed = json.loads(texts[0])
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            return parsed
    raise ToolsCallError(f"{service} tool {name} returned a malformed result (no JSON object)")


async def call_tool(
    config: AgentConfig,
    name: str,
    arguments: dict[str, Any],
    *,
    token_fetcher: Optional[Callable[[str], str]] = None,
    sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
) -> dict[str, Any]:
    """Call read-only tool ``name`` on ata-tools and return its structured result."""
    if not read_only_tool_filter(name):
        raise ToolsCallError(f"tool {name} is not in the read-only allowlist")
    if not config.tools_url:
        raise ToolsNotConfigured("TOOLS_URL is not set")
    audience = config.tools_url
    service = f"{config.name_prefix}-tools"
    endpoint = f"{audience}{MCP_PATH}"
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    fetch = token_fetcher or fetch_id_token
    try:
        headers["Authorization"] = f"Bearer {await asyncio.to_thread(fetch, audience)}"
    except Exception as exc:
        raise ToolsCallError(f"no ID token for {audience} ({type(exc).__name__}: {_short(exc)})") from exc

    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    attempts = config.tools_retry_attempts
    async with httpx.AsyncClient(transport=_default_transport, timeout=config.tools_timeout_seconds) as client:
        for attempt in range(1, attempts + 1):
            reason: Optional[str] = None
            try:
                resp = await client.post(endpoint, json=payload, headers=headers)
            except httpx.TimeoutException as exc:
                reason = f"{service} timed out after {config.tools_timeout_seconds:g}s ({type(exc).__name__})"
            except httpx.TransportError as exc:
                reason = f"{service} unreachable ({type(exc).__name__}: {_short(exc)})"
            else:
                if resp.status_code in TRANSIENT_STATUS:
                    reason = f"{service} returned HTTP {resp.status_code}"
                elif not 200 <= resp.status_code < 300:
                    raise ToolsCallError(f"{service} returned HTTP {resp.status_code}: {_short(resp.text) or 'no body'}")
                else:
                    return _tool_result(_parse_body(resp, service), name, service)
            if attempt >= attempts:
                raise ToolsCallError(f"{reason}; {attempts} attempt(s)")
            delay = min(MAX_BACKOFF_SECONDS, config.tools_retry_base_seconds * (2 ** (attempt - 1)))
            logger.warning("%s %s: %s (attempt %d of %d); retrying in %.1fs", service, name, reason, attempt, attempts, delay)
            await sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover


async def fetch_change_timeline(
    config: AgentConfig,
    *,
    window_hours: int = 2,
    **kwargs: Any,
) -> dict[str, Any]:
    """ata-tools get_change_timeline for this stack's project and region; validated shape."""
    service = f"{config.name_prefix}-tools"
    result = await call_tool(
        config,
        "get_change_timeline",
        {"window_hours": window_hours, "project_id": config.project_id, "region": config.region},
        **kwargs,
    )
    if not isinstance(result.get("changes"), list):
        raise ToolsCallError(f"{service} get_change_timeline returned a malformed result (no changes list)")
    return result
