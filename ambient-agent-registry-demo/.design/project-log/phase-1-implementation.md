# Phase 1 Implementation Log: Core ADK /run Runtime & Unit Tests

**Date**: 2026-10-20  
**Phase**: Phase 1 (Core ADK `/run` Runtime & Tests)  
**Branch**: `feat/ambient-agent-registry-demo`  
**Status**: Completed  

## 1. Overview & Objectives
Phase 1 establishes the core native Google Agent Development Kit (ADK) runtime for `ambient-agent-registry-demo`, adapted from `agent-registry-demo` (commit `74b4b6b`). All implementation resides strictly within `ambient-agent-registry-demo/` with zero modifications to existing directories outside this scope.

The implementation transitions from custom `/chat` JSON endpoints to the native ADK execution pipeline (`POST /run`, `POST /run_sse`, and `GET /health`), preserving the fixed zero-fallback architecture and strict remote FastMCP tool resolution.

## 2. Key Components & Deliverables

### A. Configuration & Safety Guard (`app/config.py`)
- Standardized environment prefix `AMBIENT_` with fallback to unprefixed standard GCP environment variables (`PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`, `LOCATION`, `REGION`).
- Defaults:
  - `service_name`: `"ambient-agent-service"`
  - `skill_name`: `"ambient-privilege-audit-skill"`
  - `calculator_mcp_server`: `"ambient-calculator"`
  - `model_name`: `"gemini-2.5-flash"`
- **Strict Project Isolation**: Zero hardcoded GCP project literals.
- **Production Safety Guard**: Prohibits starting the application with `mock_registry=True` in production environments (when `K_SERVICE` is set or when `PROJECT_ID != "demo-project-id"`).

### B. In-Memory Registry Client & Cache (`app/registry/`)
- `GCPSkillRegistryClient`: In-memory unpacking of ZIP archives downloaded from GCP Agent Registry v1alpha APIs without writing unverified files to the host filesystem.
- `Security Guardrail`: Explicitly rejects skills containing executable directories (e.g. `scripts/`), raising `ValueError` on malicious packages.
- `CachedSkillRegistry`: Thread-safe TTL cache wrapping the client with atomic invalidation (`invalidate(name)`).

### C. Remote FastMCP Toolset Management (`app/tools/registry.py`)
- `ToolRegistryManager`: Discovers and executes tools using ADK's `McpToolset` configured with Streamable HTTP (`/mcp`).
- **Zero Fallback Guarantee**: Arithmetic is never computed locally via fallback routines or regex evaluators. If upstream MCP servers or registries fail, the manager fails loudly with `RuntimeError` / HTTP 502 Bad Gateway.

### D. Dynamic Agent Loader (`app/agent_loader.py`)
- `AmbientAgentLoader(BaseAgentLoader)`: Subclasses ADK's `BaseAgentLoader` exposing `ambient_agent`.
- **Sync/Async Isolation**: Uses `concurrent.futures.ThreadPoolExecutor` to isolate async skill resolution and MCP tool discovery from the synchronous `load_agent(agent_name)` contract, avoiding event loop conflicts.
- **Dynamic Tool Filtering**: Dynamically attaches tools declared in the skill package's frontmatter metadata (`adk_additional_tools`).
- **Mock Model Support**: Integrates `MockGeminiModel` for deterministic offline testing without live Vertex AI credentials.

### E. Native ADK FastAPI Application (`app/main.py`)
- Configures application using `google.adk.cli.fast_api.get_fast_api_app` with `use_local_storage=False` and `auto_create_session=True`.
- Mounted administrative route: `POST /admin/cache/invalidate` for hot-reloading skills without server restarts.
- Custom exception handlers for `RuntimeError`, `httpx.HTTPError`, and `ValueError` to map upstream failures directly to HTTP 502/400.

## 3. Verification & Test Suite (`tests/`)
- **Pytest Suite**: 63 passing unit and integration tests across:
  - `tests/test_calculator_service.py`: FastMCP calculator endpoints, arithmetic operations, OIDC token authentication, registration script parsing, Dockerfile packaging.
  - `tests/test_skill_registry.py`: Agent Registry client ZIP unpacking, path traversal defense, forbidden scripts rejection, config defaults, health checks.
  - `tests/test_hot_reload.py`: TTL cache expiration, atomic invalidation, admin invalidation endpoints, end-to-end hot-reload without restart, container import isolation.
  - `tests/test_run.py`: Native ADK `POST /run` execution returning `list[Event]`, zero fallback on MCP failure (HTTP 502), skill not found (HTTP 404), undeclared tool guardrail enforcement.
- **Ruff Linting**: 0 errors across `ambient-agent-registry-demo/`.
- **Project Literal Scan**: Zero forbidden literal project IDs verified by `scripts/check-no-literal-projects.sh`.

## 4. Phase 1 Review Remediation
Following the Phase 1 Code Review (`reviews/phase-1-review.md`), the following fixes were implemented:
- **Cross-Event-Loop / Cross-Thread Concurrency Safety (`app/registry/cache.py`)**:
  - Replaced `asyncio.Lock()` with `threading.RLock()`.
  - Guarded cache dictionary access (`self._cache`) with `with self._lock:`.
  - Implemented cross-thread and cross-event-loop safe in-flight request coordination using `concurrent.futures.Future` and `asyncio.wrap_future`, eliminating thread/coroutine deadlocks while guaranteeing single fetches for concurrent cache misses.
  - Added unit test `test_multithreaded_concurrent_get_skill_across_event_loops` in `tests/test_hot_reload.py` verifying multi-threaded concurrent execution across separate `asyncio.run()` event loops.
- **Standardized `ambient_` Environment Prefix (`app/config.py`)**:
  - Configured `env_prefix="ambient_"` in `SettingsConfigDict(env_prefix="ambient_", case_sensitive=False, extra="ignore")`.
  - Mapped all settings fields to support both `AMBIENT_<FIELD>` and standard GCP/generic names via `validation_alias=AliasChoices(...)`.
  - Added unit test `test_config_settings_ambient_prefix` in `tests/test_skill_registry.py`.
- **Dead Code Cleanup & API Server Logging (`app/main.py`)**:
  - Removed orphaned `ToolInvocation` and `extract_provenance_from_events` routines and unused `Event` import.
  - Added `logger.warning(...)` in `create_app()` if `_find_api_server` cannot resolve `ApiServer` from route closures.
- **Dead Code Cleanup & Reusable ThreadPool (`app/agent_loader.py`)**:
  - Removed unused module-level helper `extract_skill_revision`.
  - Replaced per-call `ThreadPoolExecutor` creation in `_sync_worker` with class-level reusable executor `_EXECUTOR = concurrent.futures.ThreadPoolExecutor(max_workers=4)`.

## 5. Verification & Test Suite (`tests/`)
- **Pytest Suite**: 65 passing unit and integration tests (expanded from 63).
- **Ruff Linting**: Clean (0 errors).
- **Project Literal Scan**: Zero forbidden literal project IDs verified.

## 6. Next Phase Readiness
Phase 1 core ADK runtime and review remediations are complete and fully verified, ready for Phase 2 Terraform infrastructure definition and Phase 3 Cloud Build CI/CD automation.
