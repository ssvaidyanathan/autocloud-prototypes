# Project Log: Phase 3 - Agent Core, Tools Discovery & Hot-Reload Engine

**Date**: 2026-10-20  
**Phase**: Phase 3: Agent Core, Dynamic Toolset Integration & Hot-Reload Engine  
**Agent**: `ard-dev-p3`  
**Branch**: `feat/agent-registry-demo`  

## 1. Summary of Changes

Implemented Phase 3: Agent Core, Tools Discovery, and Hot-Reload Engine for the Agent Registry Demo prototype under `agent-registry-demo/`:

1. **`app/config.py`**:
   - Added configuration settings for cache and tools registry:
     - `skill_cache_ttl`: In-memory cache TTL in seconds (default `60.0`).
     - `tools_registry_endpoint`: Google Cloud Agent Registry v1 tools endpoint (`https://agentregistry.googleapis.com/v1`).
     - `calculator_service_url`: Streamable HTTP MCP endpoint for tool provider (`http://localhost:8080/mcp`).
     - `calculator_mcp_server`: MCP server resource identifier (`calculator`).

2. **`app/registry/cache.py`**:
   - Implemented `CachedSkillRegistry` decorating `SkillRegistry`:
     - In-memory thread-safe TTL caching wrapping remote registry calls.
     - `CacheEntry` dataclass storing `Skill`, monotonic timestamp, and SHA-256 `content_hash`.
     - Sub-millisecond lock-free fast-path read for valid cache hits (`< 1ms`).
     - Concurrency-safe cache synchronization via `asyncio.Lock` with double-checked locking on misses and expirations.
     - `invalidate(name: str | None = None) -> list[str]` atomically clearing specific skills or flushing the entire cache.
     - Full compatibility with ADK's `SkillRegistry` interface (`get_skill`, `search_skills`).

3. **`app/registry/__init__.py`**:
   - Exported `CachedSkillRegistry` and `CacheEntry` alongside `GCPSkillRegistryClient`.

4. **`app/skills_loader.py`**:
   - Wired `CachedSkillRegistry` as the default registry wrapper in `SkillsLoader`.
   - Exposed `invalidate()` method delegating directly to the cached registry.
   - Preserved `SkillToolset` factory method binding cached registry and additional tools.

5. **`app/tools/__init__.py` & `app/tools/registry.py`**:
   - Implemented `ToolRegistryManager`:
     - Queries Google Cloud Agent Registry (`v1`) for `mcpServers/calculator` catalog metadata.
     - Resolves remote `McpToolset` configured with `StreamableHTTPConnectionParams` targeting the tool service (`/mcp`).
     - Maps and dynamically discovers arithmetic tools (`add`, `multiply`, `power`).
     - Provides robust execution abstraction (`execute_tool`) supporting both FastMCP JSON-RPC response payloads and local `FunctionTool` fallbacks.

6. **`app/main.py` Updates**:
   - Wired `CachedSkillRegistry` into global application lifecycle.
   - Implemented administrative endpoint:
     - `POST /admin/cache/invalidate`: Accepts optional `{"skill_name": str}`, atomically flushes cache entries, and returns `{"status": "invalidated", "flushed": [...]}` with sub-5ms response time.
   - Updated `POST /chat` endpoint:
     - Resolves active skill from `CachedSkillRegistry`.
     - Instantiates `SkillToolset` configured with dynamic remote tools from `ToolRegistryManager`.
     - Strictly enforces `adk_additional_tools`: Only tools declared in `skill.frontmatter.metadata.adk_additional_tools` are bound and permitted for execution during the turn.
     - Executes tool operations dynamically and formats outputs according to active skill directives (Revision 1 vs. Revision 2 formatting).
     - Returns structured JSON: `{"response": str, "skill_revision": str, "tools_invoked": list[str]}`.

7. **`tests/test_hot_reload.py`**:
   - Comprehensive unit and integration test suite:
     - Cache hit within TTL verifies zero redundant fetches and sub-millisecond response.
     - Cache expiration after TTL verifies fresh fetch from underlying registry.
     - Atomic `invalidate()` forces fresh fetch on subsequent invocation.
     - `POST /admin/cache/invalidate` responds in `< 5ms` and flushes target entries.
     - Strict enforcement of `adk_additional_tools` verifies prompts requiring undeclared tools are rejected with HTTP 400.
     - `ToolRegistryManager` tool discovery, query, and execution tests.
     - End-to-end hot-reload simulation:
       1. Loads Skill Revision 1 (`add`, `multiply`), prompts arithmetic query $\rightarrow$ verifies Revision 1 formatting and tools.
       2. Updates mock registry to Skill Revision 2 (`add`, `multiply`, `power`, emoji tags) and calls `/admin/cache/invalidate`.
       3. Prompts exponentiation query $\rightarrow$ invokes `power` tool and formats with Revision 2 directives.
       4. Verifies zero container restarts, process recycling, or redeployments occurred.

## 2. Verification Results

- **Automated Tests**: 51 / 51 tests passed (100% pass rate) via `pytest tests/ -v`.
- **Linter**: `ruff check .` passed with 0 errors.
- **Type Checker**: `mypy app/` passed with 0 errors across 9 source files.

## 3. Architecture Decisions & Observations

- **Sub-Millisecond Cache Latency**: The lock-free read path in `CachedSkillRegistry` ensures incoming user requests experience $<0.1\text{ms}$ overhead on cache hits, well within Cloud Run latency budgets.
- **Dynamic Tool Restriction**: Tool scoping prevents unauthorized tool access: even if the tool provider exposes `power`, an agent executing Skill Revision 1 is blocked from invoking it because `power` is omitted from `adk_additional_tools`.
- **Zero-Downtime Hot-Reload Proven**: The combination of `CachedSkillRegistry` and `/admin/cache/invalidate` enables immediate, live prompt and tool upgrades without container rebuilds.
