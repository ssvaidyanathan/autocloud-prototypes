"""Pytest configuration and shared fixtures for ambient-agent-registry-demo."""

import io
import json
import zipfile
from collections.abc import Iterator
from typing import Callable

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import (
    app,
    set_agent_loader,
    set_skills_loader,
    set_tool_registry_manager,
)
from app.tools.registry import ToolRegistryManager


def create_skill_zip(
    name: str = "ambient-privilege-audit-skill",
    description: str = "Precision arithmetic calculation skill.",
    instructions: str = (
        "# Ambient Privilege Audit Skill\n\n"
        "Instructions:\n"
        "1. For arithmetic operations, you MUST call the registered calculator tools.\n"
        "2. Format output strictly as: 'Result: <value> (Computed via <tools_used>)'\n"
    ),
    allowed_tools: str = "add multiply",
    version: str = "1.0.0",
    references: dict[str, str] | None = None,
    assets: dict[str, str] | None = None,
    include_scripts: bool = False,
    script_files: dict[str, str] | None = None,
) -> bytes:
    """Build an in-memory ZIP archive matching the GCP Agent Registry skill package format."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        frontmatter = (
            f"---\n"
            f'name: "{name}"\n'
            f'description: "{description}"\n'
            f'allowed-tools: "{allowed_tools}"\n'
            f"metadata:\n"
            f"  version: '{version}'\n"
            f"  adk_additional_tools:\n"
        )
        for tool in allowed_tools.split():
            frontmatter += f"    - {tool}\n"
        frontmatter += f"---\n{instructions}\n"

        z.writestr("SKILL.md", frontmatter)

        if references:
            for filename, content in references.items():
                z.writestr(f"references/{filename}", content)

        if assets:
            for filename, content in assets.items():
                z.writestr(f"assets/{filename}", content)

        if include_scripts:
            scripts = script_files or {"helper.sh": "#!/bin/bash\necho bad"}
            for filename, content in scripts.items():
                z.writestr(f"scripts/{filename}", content)

    return buf.getvalue()


@pytest.fixture
def mock_zip_builder() -> Callable[..., bytes]:
    """Factory fixture to create in-memory skill ZIP packages."""
    return create_skill_zip


@pytest.fixture
def valid_skill_zip() -> bytes:
    """Fixture providing a standard valid ambient calculator skill ZIP package."""
    return create_skill_zip(
        name="ambient-privilege-audit-skill",
        description="Basic arithmetic calculation skill for addition and multiplication.",
        references={"arithmetic_rules.md": "# Arithmetic Rules\nPrecision is mandatory."},
    )


@pytest.fixture
def forbidden_scripts_zip() -> bytes:
    """Fixture providing a skill ZIP package containing a forbidden scripts/ directory."""
    return create_skill_zip(
        name="malicious-skill",
        description="A skill that illicitly bundles executable scripts.",
        include_scripts=True,
    )


def make_mock_mcp_transport() -> httpx.MockTransport:
    """Build mock MCP HTTP transport handling JSON-RPC lifecycle."""

    def mcp_mock_handler(request: httpx.Request) -> httpx.Response:
        data = json.loads(request.content.decode())
        method = data.get("method")
        req_id = data.get("id")
        if method == "initialize":
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "capabilities": {"tools": {}},
                        "protocolVersion": "2024-11-05",
                        "serverInfo": {"name": "ambient-mock-calculator", "version": "1.0.0"},
                    },
                },
            )
        elif method == "tools/list":
            tools = [
                {
                    "name": "add",
                    "description": "Add two numbers with precision arithmetic.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                    },
                },
                {
                    "name": "multiply",
                    "description": "Multiply two numbers with precision arithmetic.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                        "required": ["a", "b"],
                    },
                },
                {
                    "name": "power",
                    "description": "Raise base to the power of exponent.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "base": {"type": "number"},
                            "exponent": {"type": "number"},
                        },
                        "required": ["base", "exponent"],
                    },
                },
            ]
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "id": req_id, "result": {"tools": tools}}
            )
        elif method == "tools/call":
            params = data.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "add":
                val = float(args["a"]) + float(args["b"])
            elif name == "multiply":
                val = float(args["a"]) * float(args["b"])
            elif name == "power":
                val = float(args["base"]) ** float(args["exponent"])
            else:
                val = 0.0
            return httpx.Response(
                200,
                json={
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "result": {
                        "content": [{"type": "text", "text": str(val)}],
                        "structuredContent": {"result": val},
                        "isError": False,
                    },
                },
            )
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": req_id, "result": {}})

    return httpx.MockTransport(mcp_mock_handler)


@pytest.fixture
def test_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """Fixture providing a FastAPI TestClient configured for offline testing."""
    monkeypatch.setenv("PROJECT_ID", "demo-project-id")
    monkeypatch.setenv("MOCK_REGISTRY", "true")
    get_settings.cache_clear()

    # Reset global overrides
    set_skills_loader(None)
    set_agent_loader(None)
    api_server = getattr(app.state, "api_server", None)
    if api_server is not None:
        api_server.runner_dict.clear()

    mcp_transport = make_mock_mcp_transport()
    default_tm = ToolRegistryManager(
        project_id="demo-project-id",
        location="us-central1",
        tool_service_url="http://127.0.0.1:8080/mcp",
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=mcp_transport, base_url="http://127.0.0.1:8080", **kwargs
        ),
    )
    set_tool_registry_manager(default_tm)

    client = TestClient(app)
    yield client

    set_skills_loader(None)
    set_agent_loader(None)
    set_tool_registry_manager(None)
    if api_server is not None:
        api_server.runner_dict.clear()
    get_settings.cache_clear()
