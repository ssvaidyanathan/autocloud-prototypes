# Commit 990d912: Phase 3 Hot-Reload & Dynamic Tools Review

## Executive Summary
Phase 3 implements the hot-reload engine featuring `CachedSkillRegistry` with thread-safe TTL caching, the `POST /admin/cache/invalidate` endpoint, and dynamic tool scoping under `agent-registry-demo/`. While the in-memory cache architecture, invalidation speed (<5ms), and test suite pass rates (57/57 tests passing) are impressive, the changes introduce a critical build and runtime failure where `app/tools/registry.py` imports from the uncopied `demo/calculator_service/` package, crashing the agent container on startup with `ModuleNotFoundError` (Risk Level: HIGH).

---

## Review Scope & Summary of Changes
- **Target Commit**: `990d912` (`feat(agent-registry-demo): Phase 3 agent core, tools discovery and hot-reload engine`)
- **Author**: `ard-dev-p3`
- **Reviewer**: `ard-rev-p3` (Role: Senior Code Reviewer)
- **Repository Path**: `/scion-volumes/scratchpad/projects/agent-registry-demo/repo`
- **Reviewed Files**:
  - `agent-registry-demo/app/config.py`: Added configuration attributes `skill_cache_ttl`, `tools_registry_endpoint`, `calculator_service_url`, and `calculator_mcp_server`.
  - `agent-registry-demo/app/registry/cache.py`: Implemented `CachedSkillRegistry` with in-memory TTL caching, SHA-256 content hashing (`compute_skill_content_hash`), double-checked locking via `asyncio.Lock`, and atomic `invalidate()`.
  - `agent-registry-demo/app/registry/__init__.py`: Exported `CachedSkillRegistry` and `CacheEntry`.
  - `agent-registry-demo/app/skills_loader.py`: Integrated `CachedSkillRegistry` as default registry decorator with delegated invalidation.
  - `agent-registry-demo/app/tools/registry.py`: Implemented `ToolRegistryManager` querying Agent Registry `mcpServers/calculator` and resolving `McpToolset` over Streamable HTTP.
  - `agent-registry-demo/app/tools/__init__.py`: Exported `ToolRegistryManager`.
  - `agent-registry-demo/app/main.py`: Wired administrative cache invalidation endpoint `POST /admin/cache/invalidate`, integrated dynamic tool resolution in `POST /chat`, and enforced `adk_additional_tools` guardrails.
  - `agent-registry-demo/tests/conftest.py`: Added `set_tool_registry_manager` reset fixture hooks.
  - `agent-registry-demo/tests/test_hot_reload.py`: 12 new unit and integration test cases covering cache hits/misses/TTL expiry, invalidation latency, tool enforcement, and zero-restart hot-reload simulation.
  - `agent-registry-demo/.design/project-log/2026-10-20-phase-3-hot-reload.md`: Delivery documentation.

---

## Verification of Acceptance Criteria

| # | Acceptance Criterion | Status | Evidence / Verification Method |
|---|----------------------|--------|--------------------------------|
| 1 | Agent dynamically discovers tools via `AgentRegistry.get_mcp_toolset("calculator")` | **PARTIAL / BLOCKED** | Diverged from specification. `google.adk.integrations.agent_registry.AgentRegistry` is unimportable due to missing `google-api-core` dependency in `pyproject.toml` (Finding 3). `ToolRegistryManager` bypassed `AgentRegistry.get_mcp_toolset` and implemented a bespoke `query_mcp_server` method directly instantiating `McpToolset`. |
| 2 | `SkillToolset` only exposes tools to the model that are declared in the active skill's `metadata.adk_additional_tools` | **PASS (with Caveat)** | Verified in `test_adk_additional_tools_enforcement_rejects_unauthorized_tool` (rejects undeclared tools with HTTP 400). However, inside `app/main.py:302`, the instantiated `SkillToolset` is assigned to a throwaway variable (`_`), and scoping is enforced manually in `default_chat_executor` (Finding 4). |
| 3 | Calling `POST /admin/cache/invalidate` flushes cache atomically in $<5\text{ms}$ | **PASS** | Verified via `test_admin_cache_invalidate_endpoint` and `test_admin_cache_invalidate_all_endpoint`. Latency is $<1\text{ms}$ on in-memory dictionary clear. |
| 4 | Automated hot-reload test (R1 arithmetic $\rightarrow$ R2 upload + invalidate $\rightarrow$ R2 exponentiation with power & formatting) | **PASS** | Verified via `test_e2e_hot_reload_simulation`. Full multi-turn simulation succeeds without container or process restart. |
| 5 | Zero container restarts required for instruction/tool updates | **PASS** | Verified via `test_e2e_hot_reload_simulation` step 4; state transitions happen entirely in-memory within the active process. |
| 6 | Run tests: `pytest tests/` (100% pass rate) | **PASS** | 57 / 57 passed in 1.11s (100%). |
| 7 | Run linter: `ruff check .` (0 errors) | **PASS** | Clean (0 errors). |

---

## Critical

### Finding 1: Cross-Package Import of `demo.calculator_service.server` Causes Container Startup Crash
- **Category**: Architecture & Build / Deployment
- **Severity**: **Critical** (Blocks Merge)
- **Files**:
  - `agent-registry-demo/app/tools/registry.py:15`
  - `agent-registry-demo/Dockerfile:13-14, 31-32`
- **Code Snippet**:
  `agent-registry-demo/app/tools/registry.py:15`:
  ```python
  15: from demo.calculator_service.server import add, multiply, power
  ```
  `agent-registry-demo/Dockerfile:13-14, 31-32`:
  ```dockerfile
  13: COPY pyproject.toml /app/
  14: COPY app/ /app/app/
  ...
  31: COPY app/ /app/app/
  32: COPY pyproject.toml /app/
  ```
- **Description**:
  In `agent-registry-demo/Dockerfile`, the container image for `agent-service` only copies `app/` and `pyproject.toml` into `/app`. The `demo/` directory is intentionally excluded from the production agent image because `calculator-tool-service` is an independent, remotely hosted service.
  However, `app/tools/registry.py` imports `from demo.calculator_service.server import add, multiply, power` at top-level.
  When the container boots (`uvicorn app.main:app`), `app.main` imports `app.tools.registry`, immediately terminating the container process with:
  ```
  ModuleNotFoundError: No module named 'demo'
  ```
  This issue was hidden during local pytest runs because pytest was executed from the repo root where `demo/` was present in the Python search path.
  Furthermore, architecturally, importing the backend service implementation directly inside the consumer agent violates service boundary decoupling and bypasses the remote MCP communication boundary and zero-in-container execution requirements specified in `design.md` Sections 3.3.1 and 5.1.
- **Suggested Fix**:
  1. Remove `from demo.calculator_service.server import add, multiply, power` from `app/tools/registry.py`.
  2. If local arithmetic fallbacks are needed for offline/standalone execution when the remote MCP service is unreachable, implement self-contained fallback arithmetic functions directly inside `app/tools/registry.py` (e.g., `def _fallback_add(a: float, b: float) -> float: return a + b`, etc.), ensuring `app/` has zero dependencies on `demo/`.
  3. Add a test in `tests/test_calculator_service.py` or `tests/test_hot_reload.py` verifying that importing `app.main` succeeds when `demo` is absent from `sys.path`.

---

## Required

### Finding 2: Test Suite Silently Masks Remote MCP Connection Failures
- **Category**: Correctness & Test Coverage
- **Severity**: **Required** (Blocks Merge)
- **Files**:
  - `agent-registry-demo/tests/test_hot_reload.py:244-262`
  - `agent-registry-demo/app/tools/registry.py:91-114, 136-150`
- **Code Snippet**:
  `agent-registry-demo/tests/test_hot_reload.py:245-249`:
  ```python
  245: async def test_tool_registry_manager_discover_and_execute() -> None:
  246:     """Verify ToolRegistryManager discovers tools and executes operations."""
  247:     manager = ToolRegistryManager()
  248:     tools = await manager.discover_tools("calculator")
  249:     tool_names = [t.name for t in tools]
  ```
- **Description**:
  In `test_tool_registry_manager_discover_and_execute`, `manager` is initialized with default parameters (`calculator_service_url="http://localhost:8080/mcp"`). Because no service is running on `localhost:8080` during test execution, `await self._toolset.get_tools()` raises a connection error.
  `ToolRegistryManager.discover_tools` catches all exceptions (`except Exception:`) and silently falls back to `_fallback_tools()` (which called `demo.calculator_service.server`). Similarly, `execute_tool` caught tool execution exceptions and directly called local Python arithmetic.
  Consequently, the test suite never actually validated that `McpToolset` / Streamable HTTP discovery and FastMCP JSON-RPC execution work.
- **Suggested Fix**:
  In `tests/test_hot_reload.py`, explicitly test the real MCP client-server integration by wiring `ToolRegistryManager` to FastMCP's ASGI application via `httpx.ASGITransport` using the existing `http_client_factory` parameter:
  ```python
  from demo.calculator_service.server import create_app

  fastmcp_app = create_app()
  transport = httpx.ASGITransport(app=fastmcp_app)
  manager = ToolRegistryManager(
      tool_service_url="http://testserver/mcp",
      http_client_factory=lambda: httpx.AsyncClient(transport=transport, base_url="http://testserver"),
  )
  tools = await manager.discover_tools("calculator")
  assert "add" in [t.name for t in tools]
  ```
  Test fallback tools in a separate, explicit unit test rather than relying on silent fallback on connection failure.

### Finding 3: Missing `google-api-core` Dependency and Divergence from `AgentRegistry.get_mcp_toolset`
- **Category**: Correctness & Architecture
- **Severity**: **Required** (Blocks Merge)
- **Files**:
  - `agent-registry-demo/pyproject.toml:6-14`
  - `agent-registry-demo/app/tools/registry.py:20-82`
  - `agent-registry-demo/.design/project-log/2026-10-20-phase-3-hot-reload.md:36-42`
- **Code Snippet**:
  `pyproject.toml:6-14`:
  ```toml
  dependencies = [
      "google-adk>=2.11.0",
      "fastapi>=0.110.0",
      "uvicorn>=0.29.0",
      "httpx>=0.27.0",
      "pydantic>=2.7.0",
      "pydantic-settings>=2.2.0",
      "fastmcp>=0.1.0",
  ]
  ```
- **Description**:
  Acceptance Criterion 1 states:
  *"Agent dynamically discovers tools via `AgentRegistry.get_mcp_toolset('calculator')`."*
  However, attempting to import `from google.adk.integrations.agent_registry import AgentRegistry` fails with:
  ```
  ModuleNotFoundError: No module named 'google.api_core'
  ```
  Because `google-api-core` is missing from `pyproject.toml`, `ToolRegistryManager` could not import ADK's `AgentRegistry` and instead implemented a custom `query_mcp_server` method that queries the REST endpoint directly and instantiates `McpToolset` directly.
  While instantiating `McpToolset` with `StreamableHTTPConnectionParams` is a viable transport implementation, the missing dependency prevents native ADK `AgentRegistry` integration and contradicts the Phase 3 project log documentation.
- **Suggested Fix**:
  Add `google-api-core>=2.19.0` (and `google-auth>=2.29.0`) to `pyproject.toml` dependencies so `google.adk.integrations.agent_registry.AgentRegistry` is importable. In `ToolRegistryManager`, either leverage `AgentRegistry.get_mcp_toolset` or provide a fallback with clear documentation explaining the transport mechanism.

---

## Nit / Optional

### Finding 4: Unnecessary Instantiation and Discarding of `SkillToolset` in `/chat` Hot Path
- **Category**: Readability & Simplicity / Performance
- **Severity**: **Nit / Optional**
- **Files**:
  - `agent-registry-demo/app/main.py:300-303`
- **Code Snippet**:
  `agent-registry-demo/app/main.py:300-303`:
  ```python
  300:     tool_manager = get_tool_registry_manager()
  301:     toolset = tool_manager.resolve_toolset()
  302:     _ = loader.get_skill_toolset(additional_tools=[toolset])
  ```
- **Description**:
  On every incoming request to `POST /chat`, `tool_manager.resolve_toolset()` instantiates a new `McpToolset`, and `loader.get_skill_toolset(additional_tools=[toolset])` allocates a new `SkillToolset` whose result is assigned to `_` and immediately discarded.
  Because `default_chat_executor` performs tool execution directly via `tm.execute_tool`, this allocation in the hot path is dead code that adds garbage collection pressure without serving a functional purpose.
- **Suggested Fix**:
  Reuse the resolved `toolset` across turns or omit the redundant `_ = loader.get_skill_toolset(...)` allocation in `chat()`. If `SkillToolset` is intended to be used by an ADK Agent runner, wire the runner properly rather than discarding the instance.

### Finding 5: Concurrency Semantics in `CachedSkillRegistry.invalidate()`
- **Category**: Architecture & Correctness
- **Severity**: **Consider / Optional**
- **Files**:
  - `agent-registry-demo/app/registry/cache.py:71, 120-142, 149-166`
- **Description**:
  `CachedSkillRegistry` uses `self._lock = asyncio.Lock()`.
  1. `asyncio.Lock` synchronizes coroutines on a single asyncio event loop; it does not provide OS thread-safety across multiple threads (e.g., if Uvicorn runs multiple worker threads or thread pools). Labeling this "thread-safe" in docstrings is slightly inaccurate.
  2. `invalidate()` is a synchronous method that does not synchronize with `_lock`. If an in-flight `get_skill` request is currently fetching a skill package across the network inside `async with self._lock:`, an intervening call to `invalidate()` will clear the cache, but the in-flight fetch will immediately write the old/stale skill package back into `self._cache` upon completion.
- **Suggested Fix**:
  Add an invalidation epoch counter or generation timestamp to `CacheEntry` to reject writing entries whose fetch began prior to the most recent invalidation.

### Finding 6: Administrative Cache Invalidation Endpoint Lacks Authorization Guardrails
- **Category**: Security
- **Severity**: **Consider / Optional**
- **Files**:
  - `agent-registry-demo/app/main.py:240-254`
- **Description**:
  The `POST /admin/cache/invalidate` endpoint has no authentication, API key, or IAM role check. In an open deployment, unauthenticated clients could flood this endpoint to repeatedly evict cache entries, causing cache thrashing and exhausting Google Cloud Agent Registry API quotas.
- **Suggested Fix**:
  Enforce Cloud Run internal ingress or add a shared bearer token / header validation check on `/admin/*` routes.

### Finding 7: Invalidation with Empty String Flushes All Entries
- **Category**: Readability & Simplicity
- **Severity**: **Nit**
- **Files**:
  - `agent-registry-demo/app/registry/cache.py:158-166`
- **Description**:
  In `CachedSkillRegistry.invalidate(name=...)`, `if name:` is used to check for a specific skill. If a caller passes `name=""` (empty string), `if name:` evaluates to `False`, causing the method to flush all cache entries rather than returning an empty list or raising a validation error.
- **Suggested Fix**:
  Check `if name is not None:` and explicitly check `if not name.strip(): return []`.

---

## FYI
- `extract_skill_revision` includes convenient fallback detection for `2.0.0` -> `r2` and `[CALCULATOR-V2]` in instructions. In production with live Agent Registry, `skill._uri` reliably contains `/revisions/<id>`.
- The prototype maintains clean Separation of Concerns by placing caching logic into a decorator (`CachedSkillRegistry`) rather than bloating `GCPSkillRegistryClient`.

---

## Positive Feedback
- **High-Performance In-Memory Cache**: The lock-free fast-path read in `CachedSkillRegistry` achieves `< 0.1ms` latency, cleanly satisfying the `< 5ms` requirement.
- **Deterministic Content Hashing**: `compute_skill_content_hash` accurately includes skill instructions, frontmatter, and sorted assets/references using length-prefixed bytes to prevent hash collision attacks.
- **Thorough Test Scenarios**: `test_hot_reload.py` cleanly verifies cache hits, TTL expiry, double-checked locking under concurrency (10 parallel requests), and complete zero-downtime hot-reloading from Revision 1 to Revision 2.

---

## Test Coverage
- **New Code Paths Covered**:
  - Cache hit, miss, and TTL expiration: Covered.
  - Concurrent deduplicated fetches: Covered (`test_concurrent_get_skill_fetches_once`).
  - Cache invalidation endpoint and sub-5ms latency: Covered.
  - Unauthorized tool rejection via `adk_additional_tools`: Covered.
  - End-to-end hot-reload simulation without restarts: Covered.
- **Gaps**:
  - Remote FastMCP over Streamable HTTP execution is currently bypassed in tests due to fallback to local arithmetic (Finding 2).
  - Standalone container startup import path is unverified in tests (Finding 1).

---

## Backward Compatibility
- **Wire Formats**: Fully backwards-compatible. `POST /chat` request/response schemas remain unchanged.
- **New Endpoints**: `POST /admin/cache/invalidate` is additive.
- **Configuration**: All new settings in `app/config.py` provide sensible defaults (`skill_cache_ttl=60.0`, `calculator_mcp_server="calculator"`).

---

## Final Verdict
**REQUEST CHANGES**

### Gates Executed:
- **Unit & Integration Tests**: `/scion-volumes/scratchpad/projects/agent-registry-demo/.venv/bin/pytest tests/` (57 / 57 passed in 1.11s).
- **Linter**: `/scion-volumes/scratchpad/projects/agent-registry-demo/.venv/bin/ruff check .` (0 errors).
- **Import / Container Simulation**: Tested importing `app.main` without `demo` on `PYTHONPATH`, reproducing `ModuleNotFoundError: No module named 'demo'`.
- **FastMCP Transport Integration**: Verified `McpToolset` against FastMCP ASGI transport via `httpx.ASGITransport`.

Merge is blocked pending resolution of **Finding 1** (container startup crash on missing `demo` import), **Finding 2** (testing real remote MCP execution), and **Finding 3** (missing `google-api-core` dependency / ADK `AgentRegistry` alignment).
