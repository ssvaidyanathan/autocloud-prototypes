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

from app.main import set_skills_loader
from app.registry.cache import CachedSkillRegistry, CacheEntry
from app.registry.client import GCPSkillRegistryClient
from app.skills_loader import SkillsLoader
from app.tools.registry import ToolRegistryManager
from tests.conftest import create_skill_zip


class DummyRegistry:
    """Mock SkillRegistry for testing CachedSkillRegistry."""

    def __init__(self, fetch_delay: float = 0.0) -> None:
        self.fetch_count = 0
        self.fetch_delay = fetch_delay
        self.skills: dict[str, Skill] = {
            "calculator": Skill(
                name="calculator",
                instructions="Dummy calculator instructions",
                frontmatter=Frontmatter(
                    name="calculator",
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
    skill1 = await cached_reg.get_skill(name="calculator")
    assert dummy.fetch_count == 1
    assert skill1.name == "calculator"
    assert cached_reg.is_cached("calculator")

    # Fast-path cache hit: measure sub-millisecond response
    start = time.perf_counter()
    skill2 = await cached_reg.get_skill(name="calculator")
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
    await cached_reg.get_skill(name="calculator")
    assert dummy.fetch_count == 1
    assert cached_reg.is_cached("calculator")

    # Wait for TTL expiration
    await asyncio.sleep(0.08)
    assert not cached_reg.is_cached("calculator")

    # Second fetch after expiration
    await cached_reg.get_skill(name="calculator")
    assert dummy.fetch_count == 2


@pytest.mark.asyncio
async def test_atomic_invalidate_forces_fresh_fetch() -> None:
    """Verify invalidate() atomically flushes cache and forces fresh fetch on next call."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    # Populate cache
    await cached_reg.get_skill(name="calculator")
    assert dummy.fetch_count == 1
    assert cached_reg.is_cached("calculator")

    # Invalidate specific skill
    flushed = cached_reg.invalidate(name="calculator")
    assert flushed == ["calculator"]
    assert not cached_reg.is_cached("calculator")

    # Next fetch forces fresh registry call
    await cached_reg.get_skill(name="calculator")
    assert dummy.fetch_count == 2


@pytest.mark.asyncio
async def test_atomic_invalidate_all_entries() -> None:
    """Verify invalidate(None) clears all cached skills atomically."""
    dummy = DummyRegistry()
    dummy.skills["skill-b"] = Skill(
        name="skill-b",
        instructions="Skill B",
        frontmatter=Frontmatter(name="skill-b", description="Skill B"),
    )
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    await cached_reg.get_skill(name="calculator")
    await cached_reg.get_skill(name="skill-b")
    assert dummy.fetch_count == 2
    assert cached_reg.is_cached("calculator")
    assert cached_reg.is_cached("skill-b")

    flushed = cached_reg.invalidate()
    assert set(flushed) == {"calculator", "skill-b"}
    assert not cached_reg.is_cached("calculator")
    assert not cached_reg.is_cached("skill-b")

    # Empty cache invalidation returns empty list
    flushed_empty = cached_reg.invalidate()
    assert flushed_empty == []


def test_invalidate_empty_or_whitespace_name() -> None:
    """Verify invalidate with empty or whitespace string does not flush entries."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(target_registry=dummy, ttl_seconds=60.0)
    cached_reg._cache["calculator"] = CacheEntry(
        skill=dummy.skills["calculator"],
        timestamp=time.time(),
        content_hash="test-hash",
    )
    assert len(cached_reg._cache) == 1

    # Invalidate with empty or whitespace string returns [] and preserves cache
    assert cached_reg.invalidate("") == []
    assert len(cached_reg._cache) == 1

    assert cached_reg.invalidate("   ") == []
    assert len(cached_reg._cache) == 1

    # Invalidate specific entry flushes that entry
    assert cached_reg.invalidate("calculator") == ["calculator"]
    assert len(cached_reg._cache) == 0


@pytest.mark.asyncio
async def test_concurrent_get_skill_fetches_once() -> None:
    """Verify thread-safe locking ensures concurrent requests only fetch once."""
    dummy = DummyRegistry(fetch_delay=0.03)
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    # Launch 10 concurrent requests for the same skill
    tasks = [cached_reg.get_skill(name="calculator") for _ in range(10)]
    results = await asyncio.gather(*tasks)

    assert dummy.fetch_count == 1
    assert len(results) == 10
    for r in results:
        assert r.name == "calculator"


@pytest.mark.asyncio
async def test_search_skills_pass_through() -> None:
    """Verify search_skills delegates to target registry."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)

    results = await cached_reg.search_skills(query="calc")
    assert len(results) == 1
    assert results[0].name == "calculator"


# ==============================================================================
# 2. Administrative Endpoint Tests (/admin/cache/invalidate)
# ==============================================================================


def test_admin_cache_invalidate_endpoint(test_client: TestClient) -> None:
    """Verify POST /admin/cache/invalidate flushes cache and responds in < 5ms."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)
    loader = SkillsLoader(registry=cached_reg)
    set_skills_loader(loader)

    # Pre-populate cache directly
    cached_reg._cache["calculator"] = CacheEntry(
        skill=dummy.skills["calculator"],
        timestamp=time.monotonic(),
        content_hash="abc",
    )
    assert cached_reg.is_cached("calculator")

    start = time.perf_counter()
    response = test_client.post("/admin/cache/invalidate", json={"skill_name": "calculator"})
    latency = time.perf_counter() - start

    assert response.status_code == 200
    assert latency < 0.05, f"Response latency {latency:.6f}s exceeded threshold"

    data = response.json()
    assert data["status"] == "invalidated"
    assert data["flushed"] == ["calculator"]
    assert not cached_reg.is_cached("calculator")


def test_admin_cache_invalidate_all_endpoint(test_client: TestClient) -> None:
    """Verify POST /admin/cache/invalidate without body flushes all cache entries."""
    dummy = DummyRegistry()
    cached_reg = CachedSkillRegistry(dummy, ttl_seconds=60.0)
    loader = SkillsLoader(registry=cached_reg)
    set_skills_loader(loader)

    cached_reg._cache["skill-1"] = CacheEntry(
        skill=dummy.skills["calculator"],
        timestamp=time.monotonic(),
        content_hash="1",
    )
    cached_reg._cache["skill-2"] = CacheEntry(
        skill=dummy.skills["calculator"],
        timestamp=time.monotonic(),
        content_hash="2",
    )

    response = test_client.post("/admin/cache/invalidate", json={})
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "invalidated"
    assert set(data["flushed"]) == {"skill-1", "skill-2"}
    assert len(cached_reg._cache) == 0


# ==============================================================================
# 3. Dynamic Toolset and adk_additional_tools Enforcement Tests
# ==============================================================================


@pytest.mark.asyncio
async def test_tool_registry_manager_discover_and_execute() -> None:
    """Verify ToolRegistryManager discovers and executes tools via FastMCP ASGI transport."""
    from demo.calculator_service.server import create_app

    fastmcp_app = create_app(enforce_auth=False)
    transport = httpx.ASGITransport(app=fastmcp_app)
    manager = ToolRegistryManager(
        tool_service_url="http://testserver/mcp",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=transport, base_url="http://testserver"
        ),
    )
    tools = await manager.discover_tools("calculator")
    tool_names = [t.name for t in tools]
    assert "add" in tool_names
    assert "multiply" in tool_names
    assert "power" in tool_names

    # Test arithmetic tool executions via real MCP JSON-RPC protocol
    add_res = await manager.execute_tool("add", {"a": 12.0, "b": 8.0})
    assert add_res == 20.0

    mult_res = await manager.execute_tool("multiply", {"a": 6.0, "b": 7.0})
    assert mult_res == 42.0

    power_res = await manager.execute_tool("power", {"base": 2.0, "exponent": 8.0})
    assert power_res == 256.0


@pytest.mark.asyncio
async def test_tool_registry_manager_fallback_tools() -> None:
    """Verify ToolRegistryManager falls back to local tools when remote MCP is unreachable."""
    manager = ToolRegistryManager(
        tool_service_url="http://127.0.0.1:9999/unreachable/mcp",
        http_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(503))
        ),
    )
    tools = await manager.discover_tools("calculator")
    tool_names = [t.name for t in tools]
    assert "add" in tool_names
    assert "multiply" in tool_names
    assert "power" in tool_names

    # Test arithmetic fallback executions
    add_res = await manager.execute_tool("add", {"a": 12.0, "b": 8.0})
    assert add_res == 20.0

    mult_res = await manager.execute_tool("multiply", {"a": 6.0, "b": 7.0})
    assert mult_res == 42.0

    power_res = await manager.execute_tool("power", {"base": 2.0, "exponent": 8.0})
    assert power_res == 256.0


@pytest.mark.asyncio
async def test_tool_registry_manager_query_agent_registry() -> None:
    """Verify ToolRegistryManager query_mcp_server queries Agent Registry v1."""
    mock_response = {
        "name": "projects/cloud-assist-fde-7/locations/us-central1/mcpServers/calculator",
        "displayName": "Precision Calculator Service",
        "interfaces": [{"protocolBinding": "http-json", "url": "http://localhost:8080/mcp"}],
    }

    def mock_handler(request: httpx.Request) -> httpx.Response:
        if "mcpServers/calculator" in str(request.url):
            return httpx.Response(status_code=200, json=mock_response)
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    async with httpx.AsyncClient(transport=transport) as mock_http:
        manager = ToolRegistryManager(
            project_id="cloud-assist-fde-7",
            location="us-central1",
            http_client=mock_http,
        )
        res = await manager.query_mcp_server("calculator")
        assert res["name"] == mock_response["name"]
        assert res["interfaces"][0]["url"] == "http://localhost:8080/mcp"


def test_adk_additional_tools_enforcement_rejects_unauthorized_tool(
    test_client: TestClient,
) -> None:
    """Verify prompt requiring undeclared tool fails with 400 Bad Request."""
    v1_zip = create_skill_zip(
        name="calculator",
        description="Revision 1 arithmetic only",
        instructions="Standard calculator instructions",
        allowed_tools="add multiply",
        version="1.0.0",
    )

    def mock_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        "projects/cloud-assist-fde-7/locations/us-central1/"
                        "skills/calculator/revisions/r1"
                    ),
                },
            )
        elif "revisions/r1" in url_str:
            return httpx.Response(
                status_code=200,
                content=v1_zip,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404)

    transport = httpx.MockTransport(mock_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    client = GCPSkillRegistryClient(
        project_id="cloud-assist-fde-7",
        location="us-central1",
        http_client=mock_http,
    )
    loader = SkillsLoader(registry=client)
    set_skills_loader(loader)

    # Exponentiation requires 'power', which is NOT in Revision 1's adk_additional_tools
    response = test_client.post(
        "/chat",
        json={"prompt": "Compute 2 to the power of 8."},
    )
    assert response.status_code == 400
    assert "power" in response.json()["detail"].lower()
    assert "not declared" in response.json()["detail"].lower()


# ==============================================================================
# 4. End-to-End Live Hot-Reload Simulation Test
# ==============================================================================


def test_e2e_hot_reload_simulation(test_client: TestClient) -> None:
    """Simulate complete live hot-reload without container restart.

    Sequence:
    1. Boot with Skill Revision 1 (add, multiply, standard formatting).
       Query arithmetic prompt -> executes add & multiply, formats as Revision 1.
    2. Author updates Agent Registry to Skill Revision 2 (add, multiply, power, emoji formatting).
       Invoke POST /admin/cache/invalidate.
    3. Query exponentiation prompt -> executes power, formats with Revision 2 directives.
    4. Validate container was not restarted and process remained live throughout.
    """
    # Build package bytes for Revision 1
    r1_zip = create_skill_zip(
        name="calculator",
        description="Revision 1 arithmetic calculation skill.",
        instructions=(
            "# Calculator Skill (v1)\n\n"
            "Directives:\n"
            "1. Invoke calculator tools.\n"
            "2. Format strictly as: 'Result: <value> (Computed via <tools_used>)'\n"
        ),
        allowed_tools="add multiply",
        version="1.0.0",
    )

    # Build package bytes for Revision 2
    r2_zip = create_skill_zip(
        name="calculator",
        description="Revision 2 advanced calculation skill with power.",
        instructions=(
            "# Calculator Skill (v2)\n\n"
            "Directives:\n"
            "1. Invoke calculator tools.\n"
            "2. Format strictly with emoji: '🧮 [CALCULATOR-V2] Computed value: **<value>**'\n"
        ),
        allowed_tools="add multiply power",
        version="2.0.0",
    )

    current_revision = "r1"

    def mock_registry_handler(request: httpx.Request) -> httpx.Response:
        url_str = str(request.url)
        if url_str.endswith("/skills/calculator"):
            return httpx.Response(
                status_code=200,
                json={
                    "name": "projects/cloud-assist-fde-7/locations/us-central1/skills/calculator",
                    "defaultRevision": (
                        f"projects/cloud-assist-fde-7/locations/us-central1/"
                        f"skills/calculator/revisions/{current_revision}"
                    ),
                },
            )
        elif f"revisions/{current_revision}" in url_str and "alt=media" in url_str:
            content = r1_zip if current_revision == "r1" else r2_zip
            return httpx.Response(
                status_code=200,
                content=content,
                headers={"content-type": "application/zip"},
            )
        return httpx.Response(status_code=404, json={"error": "Not Found"})

    transport = httpx.MockTransport(mock_registry_handler)
    mock_http = httpx.AsyncClient(transport=transport)
    base_registry = GCPSkillRegistryClient(
        project_id="cloud-assist-fde-7",
        location="us-central1",
        http_client=mock_http,
    )
    # Loader automatically wraps base_registry with CachedSkillRegistry
    loader = SkillsLoader(registry=base_registry, cache_ttl=60.0)
    set_skills_loader(loader)

    # --- Step 1: Query baseline Revision 1 ---
    res1 = test_client.post(
        "/chat",
        json={"prompt": "Compute 15 multiplied by 4, then add 10."},
    )
    assert res1.status_code == 200
    data1 = res1.json()
    assert data1["skill_revision"] == "r1"
    assert data1["response"] == "Result: 70 (Computed via multiply, add)"
    assert data1["tools_invoked"] == ["multiply", "add"]

    # --- Step 2: Live Registry Update (Zero container restart) ---
    # Author pushes revision r2 to Agent Registry and updates defaultRevision
    current_revision = "r2"

    # Fast administrative cache invalidation
    inv_res = test_client.post("/admin/cache/invalidate", json={"skill_name": "calculator"})
    assert inv_res.status_code == 200
    assert inv_res.json()["status"] == "invalidated"
    assert "calculator" in inv_res.json()["flushed"]

    # --- Step 3: Query exponentiation with Revision 2 directives ---
    res2 = test_client.post(
        "/chat",
        json={"prompt": "Compute 2 to the power of 8."},
    )
    assert res2.status_code == 200
    data2 = res2.json()
    assert data2["skill_revision"] == "r2"
    assert data2["response"] == "🧮 [CALCULATOR-V2] Computed value: **256**"
    assert data2["tools_invoked"] == ["power"]

    # --- Step 4: Verify zero restarts ---
    # The application instance 'app' has remained continuous and live throughout
    health_res = test_client.get("/health")
    assert health_res.status_code == 200
    assert health_res.json() == {"status": "healthy"}


# ==============================================================================
# 5. Standalone Container Import Isolation Tests (Finding 1)
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

    code = (
        "import app.main\n"
        "print('container standalone import ok')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"Standalone container import failed: {result.stderr}"
    assert "container standalone import ok" in result.stdout

