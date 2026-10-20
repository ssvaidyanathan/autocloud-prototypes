# Project Log: Phase 7 - Remediation v2 (Pure ADK Agent, Provenance & Safety Guards)

**Date**: 2026-10-20  
**Phase**: Phase 7 Remediation: Zero Fallback Tools, Pure ADK LlmAgent + Runner, Provenance & Production Safety Guards  
**Agent**: `ard-dev-p7`  
**Branch**: `feat/agent-registry-demo`  

## 1. Summary of Changes

Implemented Phase 7 Remediation addressing all findings from reviewer and lead architecture audits:

1. **Elimination of Fallback Tools & Local Computation (`app/tools/registry.py`)**:
   - Completely deleted all in-process arithmetic fallback implementations (`_fallback_add`, `_fallback_multiply`, `_fallback_power`, `_create_fallback_tool`, `_fallback_tools`).
   - Refactored `ToolRegistryManager.discover_tools` and `execute_tool` to fail loudly (raising `RuntimeError`) when remote MCP calls fail or tools are unavailable.
   - Zero fallback computation exists in the codebase. All tool execution strictly routes through remote FastMCP over Streamable HTTP.
   - Added `endpoint_url` property to `ToolRegistryManager` for provenance reporting.

2. **Removal of Silent Retry in Client (`app/registry/client.py`)**:
   - Removed the silent 404 retry logic that automatically prepended `private-` in `get_skill`.
   - Updated `Settings.skill_name` default in `app/config.py` to `"private-calculator"`.
   - Updated `CachedSkillRegistry.invalidate` in `app/registry/cache.py` to flush both base name and `private-` aliases atomically.

3. **Real ADK `LlmAgent` + `Runner` Execution Flow (`app/main.py`)**:
   - Eliminated regex-based prompt matching and pseudo-agent simulation.
   - Wired ADK `LlmAgent` with `model=settings.model_name` (defaulting to `"gemini-2.0-flash-001"`), `instruction=active_skill.instructions`, and tools dynamically populated via `SkillToolset` / `McpToolset`.
   - Managed multi-turn sessions using ADK `Runner` and `InMemorySessionService`.
   - Added `extract_provenance_from_events` to inspect ADK run events, extracting `FunctionCall` arguments and `FunctionResponse` results.
   - For offline mock mode (`MOCK_REGISTRY=true`), agent executes actual JSON-RPC `tools/call` requests over the mock transport against the FastMCP router, capturing dynamic function execution events without requiring live Vertex AI credentials.
   - Mapped remote MCP failures and tool execution errors directly to HTTP 502 (Bad Gateway) or HTTP 503 (Service Unavailable).

4. **Structured Provenance in `/chat` Response**:
   - Enhanced `ChatResponse` model with:
     - `skill`: Structured `SkillProvenance` (`name`, `revision`, `resource_name`).
     - `mcp_server`: Remote tool server URL.
     - `tools_invoked`: Detailed list of `ToolInvocation` items capturing `tool`, `name`, `args`, and `result`.
     - `skill_revision`: Retained for backward compatibility with existing callers.

5. **Production Safety Guards (`app/config.py`)**:
   - Added `Settings.model_post_init` safety guard ensuring `mock_registry=True` is strictly rejected if running in production (detected via `K_SERVICE` environment variable or when `project_id != "demo-project-id"`).
   - Prevents accidental mock execution in live Cloud Run production environments.

6. **Demonstration Script & Mock Server Alignment**:
   - Updated `scripts/mock_server.py` to route both `/skills/calculator` and `/skills/private-calculator`, and wired FastMCP JSON-RPC transport into `ToolRegistryManager`.
   - Updated `scripts/run_demo.sh` `jq` extractions in Act 1 and Act 2 to handle structured tool invocation objects while retaining backward compatibility with string arrays.

7. **Test Suite Expansion & Quality Verification**:
   - Added `test_production_safety_guard_blocks_mock_registry` validating fail-fast behavior.
   - Added `test_chat_endpoint_mcp_failure_returns_http_502_zero_fallback` confirming zero-fallback policy and HTTP 502 status.
   - Added `test_chat_endpoint_provenance_and_adk_events` verifying structured provenance parsing.
   - Fixed all linter line length issues across test files.

## 2. Verification Results

- **Automated Tests**:
  - `pytest tests/ -v`: **72 / 72 passed (100% pass rate)**.
- **Code Linter**:
  - `ruff check .`: **0 errors**.
- **Literal Project & Model Checks**:
  - `scripts/check-no-literal-projects.sh`: **PASS** (Zero literal projects, zero un-externalized Gemini model literals).
- **Demo Script Validations**:
  - `PROJECT_ID=demo-project-id ./scripts/run_demo.sh --mock-mode`: **PASS** (100% assertions passed across all 5 steps).
  - `./scripts/run_demo.sh --dry-run`: **PASS**.
