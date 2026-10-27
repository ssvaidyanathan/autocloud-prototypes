"""ata-tools MCP client: SSE responses and JSON-RPC id checking (test review M4)."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from agent.app.config import AgentConfig
from agent.app.tools import tools_client
from agent.app.tools.tools_client import ToolsCallError, fetch_change_timeline

TOOLS_URL = "https://ata-tools-abc-uw.a.run.app"


def _run(handler):
    cfg = AgentConfig(project_id="test-project", region="us-west1", is_local=False, tools_url=TOOLS_URL,
                      tools_retry_base_seconds=0.0)
    tools_client.set_default_transport(httpx.MockTransport(handler))
    try:
        return asyncio.run(fetch_change_timeline(cfg, token_fetcher=lambda aud: "tok"))
    finally:
        tools_client.set_default_transport(None)


def _msg(result: Any, msg_id: Any = 1) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": msg_id, "result": result})


def _sse(*events: str) -> httpx.Response:
    body = "".join(f"event: message\ndata: {e}\n\n" for e in events)
    return httpx.Response(200, headers={"content-type": "text/event-stream"}, text=body)


OK = {"structuredContent": {"changes": [{"source": "cloud_run"}]}, "isError": False, "content": []}
PROGRESS = {"structuredContent": {"changes": []}, "isError": False, "content": []}


def test_sse_response_uses_the_last_data_event():
    assert _run(lambda req: _sse(_msg(PROGRESS), _msg(OK))) == OK["structuredContent"]


def test_single_event_sse_response():
    assert _run(lambda req: _sse(_msg(OK))) == OK["structuredContent"]


def test_sse_without_data_is_a_stated_malformed_response():
    resp = httpx.Response(200, headers={"content-type": "text/event-stream"}, text="event: ping\n\n")
    with pytest.raises(ToolsCallError, match="malformed response"):
        _run(lambda req: resp)


@pytest.mark.parametrize("msg_id", [2, None, "1"])
def test_json_rpc_id_mismatch_is_refused(msg_id):
    with pytest.raises(ToolsCallError, match="another request"):
        _run(lambda req: httpx.Response(200, json=json.loads(_msg(OK, msg_id))))
    with pytest.raises(ToolsCallError, match="another request"):
        _run(lambda req: _sse(_msg(OK, msg_id)))
