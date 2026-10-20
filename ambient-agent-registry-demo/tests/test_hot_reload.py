"""Unit and integration tests for Hot-Reload Engine, Cache Invalidation, and Dynamic Tools."""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from google.adk.skills.models import Frontmatter, Skill

from app.main import set_agent_loader, set_skills_loader
from app.registry.cache import CachedSkillRegistry, CacheEntry
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager


class DummyRegistry:
    """Mock SkillRegistry for testing CachedSkillRegistry."""

    def __init__(self, fetch_delay: float = 0.0) -> None:
        self.fetch_count = 0
        self.fetch_delay = fetch_delay
        self.skills: dict[str, Skill] = {
            "ambient-privilege-audit-skill": Skill(
                name="ambient-privilege-audit-skill",
                instructions="Dummy calculator instructions",
                frontmatter=Frontmatter(
                    name="ambient-privilege-audit-skill",
                    description="Dummy calc",
                    metadata={"version": "1.0.0", "adk_additional_tools": ["add", "multiply"]},
                ),
            )
        }

    async def get_skill(self, *, name: str = "", **kwargs: Any) -> Skill:
        resolved = name or kwargs.get("name")
        self.fetch_count += 1
        if self.fetch_delay > 0:
            await asyncio.sleep(self.fetch_delay)
        if resolved in self.skills:
            return self.skills[resolved]
        raise ValueError(f"Skill '{resolved}' not found")

    async def search_skills(self, *, query: str, **kwargs: Any) -> list[Frontmatter]:
        return [
            s.frontmatter
            for s in self.skills.values()
            if query.lower() in (s.frontmatter.description or "").lower()
        ]


# ==============================================================================
# 1. CachedSkillRegistry Unit Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_cache_hit_within_ttl() -> None:
    """Verify cache hit within TTL does not re-fetch from underlying target registry."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    # First fetch (cache miss)
    skill1 = await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    assert dummy.fetch_count == 1
    assert skill1.name == "ambient-privilege-audit-skill"
    assert cached_reg.is_cached("ambient-privilege-audit-skill")

    # Fast-path cache hit: measure sub-millisecond response
    start = time.perf_counter()
    skill2 = await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    latency = time.perf_counter() - start

    assert dummy.fetch_count == 1  # Did NOT re-fetch
    assert skill1 is skill2
    assert latency < 0.005, f"Cache hit latency {latency:.6f}s exceeded 5ms"


@pytest.mark.asyncio
async def test_cache_expiration_after_ttl() -> None:
    """Verify cache expires after TTL and triggers fresh fetch from registry."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=0.05)

    # Initial fetch
    await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    assert dummy.fetch_count == 1
    assert cached_reg.is_cached("ambient-privilege-audit-skill")

    # Wait for TTL expiration
    await asyncio.sleep(0.08)

    # Cache must report expired
    assert not cached_reg.is_cached("ambient-privilege-audit-skill")

    # Second fetch after expiry triggers fresh fetch
    skill2 = await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    assert dummy.fetch_count == 2
    assert skill2.name == "ambient-privilege-audit-skill"


@pytest.mark.asyncio
async def test_atomic_invalidate_forces_fresh_fetch() -> None:
    """Verify explicit atomic invalidate evicts entry immediately and forces fetch."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    assert dummy.fetch_count == 1
    assert cached_reg.is_cached("ambient-privilege-audit-skill")

    flushed = cached_reg.invalidate(name="ambient-privilege-audit-skill")
    assert "ambient-privilege-audit-skill" in flushed
    assert not cached_reg.is_cached("ambient-privilege-audit-skill")

    await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    assert dummy.fetch_count == 2


@pytest.mark.asyncio
async def test_atomic_invalidate_all_entries() -> None:
    """Verify calling invalidate() with None flushes entire in-memory cache."""
    dummy = DummyRegistry()
    dummy.skills["audit-skill-2"] = Skill(
        name="audit-skill-2",
        instructions="Second skill",
        frontmatter=Frontmatter(name="audit-skill-2", description="Skill 2"),
    )
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    await cached_reg.get_skill(name="audit-skill-2")
    assert dummy.fetch_count == 2
    assert cached_reg.is_cached("ambient-privilege-audit-skill")
    assert cached_reg.is_cached("audit-skill-2")

    flushed = cached_reg.invalidate(name=None)
    assert len(flushed) == 2
    assert "ambient-privilege-audit-skill" in flushed
    assert "audit-skill-2" in flushed
    assert not cached_reg.is_cached("ambient-privilege-audit-skill")
    assert not cached_reg.is_cached("audit-skill-2")


@pytest.mark.asyncio
async def test_invalidate_empty_or_whitespace_name() -> None:
    """Verify invalidating empty or whitespace-only name returns empty without purging."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    await cached_reg.get_skill(name="ambient-privilege-audit-skill")
    flushed = cached_reg.invalidate(name="   ")
    assert flushed == []
    assert cached_reg.is_cached("ambient-privilege-audit-skill")


@pytest.mark.asyncio
async def test_concurrent_get_skill_fetches_once() -> None:
    """Verify concurrent requests during a cache miss serialize and fetch only once."""
    dummy = DummyRegistry(fetch_delay=0.05)
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    results = await asyncio.gather(
        cached_reg.get_skill(name="ambient-privilege-audit-skill"),
        cached_reg.get_skill(name="ambient-privilege-audit-skill"),
        cached_reg.get_skill(name="ambient-privilege-audit-skill"),
    )

    assert dummy.fetch_count == 1
    assert results[0] is results[1] is results[2]


@pytest.mark.asyncio
async def test_search_skills_pass_through() -> None:
    """Verify search_skills passes through to underlying registry."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy)
    results = await cached_reg.search_skills(query="dummy")
    assert len(results) == 1
    assert results[0].name == "ambient-privilege-audit-skill"


# ==============================================================================
# 2. Administrative Invalidation Endpoints
# ==============================================================================


def test_admin_cache_invalidate_endpoint(test_client: TestClient) -> None:
    """Test POST /admin/cache/invalidate flushes specified skill."""
    from app.main import get_cached_registry

    cached_reg = get_cached_registry()
    cached_reg._cache["ambient-privilege-audit-skill"] = CacheEntry(
        skill=Skill(
            name="ambient-privilege-audit-skill",
            instructions="Test instructions",
            frontmatter=Frontmatter(name="ambient-privilege-audit-skill", description="desc"),
        ),
        timestamp=time.monotonic(),
        content_hash="abc",
    )
    assert cached_reg.is_cached("ambient-privilege-audit-skill")

    response = test_client.post(
        "/admin/cache/invalidate",
        json={"skill_name": "ambient-privilege-audit-skill"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "invalidated"
    assert any("ambient-privilege-audit-skill" in s for s in data["flushed"])
    assert not cached_reg.is_cached("ambient-privilege-audit-skill")


def test_admin_cache_invalidate_all_endpoint(test_client: TestClient) -> None:
    """Test POST /admin/cache/invalidate with empty payload flushes all cache entries."""
    from app.main import get_cached_registry

    cached_reg = get_cached_registry()
    cached_reg._cache["skill-a"] = CacheEntry(
        skill=Skill(
            name="skill-a",
            instructions="Test instructions",
            frontmatter=Frontmatter(name="skill-a", description="a"),
        ),
        timestamp=time.monotonic(),
        content_hash="a",
    )
    cached_reg._cache["skill-b"] = CacheEntry(
        skill=Skill(
            name="skill-b",
            instructions="Test instructions",
            frontmatter=Frontmatter(name="skill-b", description="b"),
        ),
        timestamp=time.monotonic(),
        content_hash="b",
    )

    response = test_client.post("/admin/cache/invalidate", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "invalidated"
    assert len(data["flushed"]) == 2
    assert "skill-a" in data["flushed"]
    assert "skill-b" in data["flushed"]
    assert len(cached_reg._cache) == 0


# ==============================================================================
# 3. ToolRegistryManager Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_registry_manager_discover_and_execute() -> None:
    """Test ToolRegistryManager tool discovery and execution via mock MCP transport."""
    from tests.conftest import make_mock_mcp_transport

    mock_transport = make_mock_mcp_transport()
    tm = ToolRegistryManager(
        project_id="demo-project-id",
        location="us-central1",
        tool_service_url="http://127.0.0.1:8080/mcp",
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=mock_transport, base_url="http://127.0.0.1:8080", **kwargs
        ),
    )

    tools = await tm.discover_tools("ambient-calculator")
    assert len(tools) == 3
    tool_names = [t.name for t in tools]
    assert "add" in tool_names
    assert "multiply" in tool_names
    assert "power" in tool_names

    res_add = await tm.execute_tool("add", {"a": 12.0, "b": 8.0})
    assert res_add == 20.0

    res_mult = await tm.execute_tool("multiply", {"a": 4.0, "b": 5.0})
    assert res_mult == 20.0

    res_pow = await tm.execute_tool("power", {"base": 2.0, "exponent": 5.0})
    assert res_pow == 32.0


@pytest.mark.asyncio
async def test_tool_registry_manager_fails_loudly_on_mcp_error() -> None:
    """Test that McpToolset discovery failure raises loud error with zero fallback."""
    failing_transport = httpx.MockTransport(lambda req: httpx.Response(500, text="Internal Error"))
    tm = ToolRegistryManager(
        project_id="demo-project-id",
        location="us-central1",
        tool_service_url="http://failing-mcp-server/mcp",
        http_client_factory=lambda **kwargs: httpx.AsyncClient(
            transport=failing_transport, base_url="http://failing-mcp-server", **kwargs
        ),
    )

    with pytest.raises(RuntimeError, match="Failed to discover tools from remote MCP server"):
        await tm.discover_tools("ambient-calculator")


@pytest.mark.asyncio
async def test_tool_registry_manager_query_agent_registry() -> None:
    """Test querying GCP Agent Registry tools catalog endpoint."""

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if "mcpServers/ambient-calculator" in url_str:
            return httpx.Response(
                200,
                json={
                    "name": (
                        "projects/demo-project-id/locations/us-central1/"
                        "mcpServers/ambient-calculator"
                    ),
                    "serviceUrl": "http://127.0.0.1:8080/mcp",
                },
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        tm = ToolRegistryManager(
            project_id="demo-project-id",
            location="us-central1",
            http_client=mock_http,
        )
        data = await tm.query_mcp_server("ambient-calculator")
        assert data["serviceUrl"] == "http://127.0.0.1:8080/mcp"


# ==============================================================================
# 4. Hot-Reload Lifecycle Simulation with /run
# ==============================================================================


def test_e2e_hot_reload_simulation(
    test_client: TestClient,
    mock_zip_builder: Any,
) -> None:
    """Demonstrate end-to-end hot-reload lifecycle using native ADK /run runtime."""
    from app.registry.client import GCPSkillRegistryClient

    v1_zip = mock_zip_builder(
        name="ambient-privilege-audit-skill",
        description="Version 1: add and multiply only.",
        instructions="Instructions for v1",
        allowed_tools="add multiply",
        version="1.0.0",
    )

    v2_zip = mock_zip_builder(
        name="ambient-privilege-audit-skill",
        description="Version 2: includes power operation.",
        instructions="Instructions for [CALCULATOR-V2]",
        allowed_tools="add multiply power",
        version="2.0.0",
    )

    current_revision = "r1"

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        nonlocal current_revision
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
                        f"skills/ambient-privilege-audit-skill/revisions/{current_revision}"
                    ),
                },
            )
        elif f"revisions/{current_revision}" in url_str:
            content = v1_zip if current_revision == "r1" else v2_zip
            return httpx.Response(
                status_code=200,
                content=content,
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

    # --- Step 1: Query multiplication & addition under Revision 1 ---
    res1 = test_client.post(
        "/run",
        json={
            "app_name": "ambient_agent",
            "user_id": "demo-user",
            "session_id": "session-act-1",
            "new_message": {
                "role": "user",
                "parts": [{"text": "Compute 15 multiplied by 4, then add 10."}],
            },
        },
    )
    assert res1.status_code == 200
    events1 = res1.json()
    function_calls_1 = [
        p["functionCall"]["name"]
        for ev in events1
        for p in ev.get("content", {}).get("parts", [])
        if "functionCall" in p
    ]
    assert "multiply" in function_calls_1
    assert "add" in function_calls_1

    # --- Mutation: Publish Revision 2 enabling 'power' ---
    current_revision = "r2"

    # Fast administrative cache invalidation
    inv_res = test_client.post(
        "/admin/cache/invalidate",
        json={"skill_name": "ambient-privilege-audit-skill"},
    )
    assert inv_res.status_code == 200
    assert inv_res.json()["status"] == "invalidated"

    # --- Step 3: Query exponentiation under Revision 2 ---
    res2 = test_client.post(
        "/run",
        json={
            "app_name": "ambient_agent",
            "user_id": "demo-user",
            "session_id": "session-act-2",
            "new_message": {
                "role": "user",
                "parts": [{"text": "Compute 2 to the power of 8."}],
            },
        },
    )
    assert res2.status_code == 200
    events2 = res2.json()
    function_calls_2 = [
        p["functionCall"]["name"]
        for ev in events2
        for p in ev.get("content", {}).get("parts", [])
        if "functionCall" in p
    ]
    assert "power" in function_calls_2

    text_parts_2 = [
        p["text"]
        for ev in events2
        for p in ev.get("content", {}).get("parts", [])
        if "text" in p
    ]
    assert any("256" in t for t in text_parts_2)

    # --- Step 4: Verify zero restarts ---
    health_res = test_client.get("/health")
    assert health_res.status_code == 200
    assert health_res.json() == {"status": "ok"}


# ==============================================================================
# 5. Standalone Container Import Isolation Tests
# ==============================================================================


def test_import_app_main_without_demo_in_sys_path() -> None:
    """Verify app.main imports cleanly when demo package is completely blocked/absent."""
    code = (
        "import sys\n"
        "sys.modules['demo'] = None\n"
        "import app.main\n"
        "print('app.main imported cleanly without demo package')\n"
    )
    project_root = str(Path(__file__).resolve().parent.parent)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
        cwd=project_root,
    )
    assert result.returncode == 0, f"Import failed with stderr: {result.stderr}"
    assert "app.main imported cleanly without demo package" in result.stdout


def test_app_main_standalone_container_simulation(tmp_path: Path) -> None:
    """Verify app.main imports in container-like environment where demo/ does not exist."""
    repo_app = Path(__file__).resolve().parent.parent / "app"
    container_app = tmp_path / "app"
    shutil.copytree(repo_app, container_app)

    code = "import app.main\nprint('container standalone import ok')\n"
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Standalone container import failed: {result.stderr}"
    assert "container standalone import ok" in result.stdout
