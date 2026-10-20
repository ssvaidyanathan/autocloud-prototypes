# Project Log: Phase 2 Remote Tool Provider Service (FastMCP Calculator)

**Date**: 2026-10-20  
**Phase**: Phase 2: Remote Tool Provider Service (FastMCP Calculator)  
**Agent**: `ard-dev-p2`  
**Branch**: `feat/agent-registry-demo`  

## 1. Summary of Changes

Implemented the Phase 2 remote tool provider service and registration automation for the Agent Registry Demo prototype:

1. **`demo/calculator_service/server.py`**:
   - Implemented FastMCP server (`Precision Calculator Service`) exposing JSON-RPC over Streamable HTTP on `/mcp`.
   - Exposed verified arithmetic operations:
     - `add(a: float, b: float) -> float`: Precision addition.
     - `multiply(a: float, b: float) -> float`: Precision multiplication.
     - `power(base: float, exponent: float) -> float`: Precision power computation.
   - Container health check probe endpoint `GET /health` returning `{"status": "healthy"}` with HTTP 200 OK.
   - Configurable `AuthMiddleware` supporting Bearer token and optional Google OIDC ID token verification (`verify_oauth2_token` with audience and allowed service account restrictions). Bypasses health probes and CORS preflights.
   - Application factory `create_app()` returning configured Starlette ASGI application with stateless HTTP (`stateless_http=True`) and JSON response mode (`json_response=True`).

2. **`demo/calculator_service/main.py`**:
   - Production entrypoint script loading host, port, and log level from environment variables (`HOST`, `PORT`, `LOG_LEVEL`).
   - Runs server via Uvicorn on Cloud Run default port 8080.

3. **`demo/calculator_service/__init__.py`**:
   - Exported package components (`add`, `multiply`, `power`, `mcp`, `app`, `create_app`, `health_check`).

4. **`demo/calculator_service/pyproject.toml`**:
   - Standalone package specification declaring dependencies (`fastmcp>=0.1.0`, `fastapi>=0.110.0`, `uvicorn>=0.29.0`, `httpx>=0.27.0`, `google-auth>=2.29.0`).

5. **`demo/calculator_service/Dockerfile`**:
   - Multi-stage Docker build targeting Python 3.12-slim.
   - Non-root user `appuser` (UID 10001) for least-privilege container execution on Cloud Run.
   - Exposes container port 8080.

6. **`scripts/register_tool.sh`**:
   - Executable bash script automating registration and updates of the tool service in Google Cloud Agent Registry (`agentregistry.googleapis.com/v1`) as an `mcpServers` resource.
   - Exact verified command syntax:
     ```bash
     gcloud agent-registry services create calculator \
       --project="${PROJECT_ID:-cloud-assist-fde-7}" \
       --location="${REGION:-us-central1}" \
       --display-name="Precision Calculator Service" \
       --description="Provides verified arithmetic operations (add, multiply, power)" \
       --mcp-server-spec-type=no-spec \
       --interfaces="protocolBinding=http-json,url=${TOOL_SERVICE_URL}/mcp"
     ```
   - Automated check for existing service via `gcloud agent-registry services describe` to toggle between `services create` and `services update`.
   - Comprehensive `--dry-run` flag support and argument parsing.

7. **`tests/test_calculator_service.py`**:
   - Comprehensive test suite covering 17 test cases:
     - `GET /health` probe returning HTTP 200 OK and health probe authentication bypass.
     - Direct unit execution of `add`, `multiply`, and `power`.
     - FastMCP `/mcp` endpoint handling `tools/list` returning all 3 arithmetic tools with schemas.
     - FastMCP `/mcp` tool invocation (`tools/call`) for `add`, `multiply`, and `power` verifying computation accuracy.
     - Authentication enforcement: missing header rejection (401), malformed token rejection (401), valid Bearer token acceptance (200), Google OIDC token claim validation, caller service account email restriction (403), and expired token rejection (401).
     - Registration script dry-run validation.

## 2. Verification Results

- **Automated Tests**: 17 / 17 tests passed (100% pass rate) via `pytest tests/test_calculator_service.py`.
- **Full Test Suite**: 34 / 34 tests passed (100% pass rate) across all test suites (`test_calculator_service.py` and `test_skill_registry.py`).
- **Linter**: `ruff check demo/ tests/test_calculator_service.py` clean (0 errors, 0 warnings).
- **Type Checker**: `mypy --explicit-package-bases demo/calculator_service tests/test_calculator_service.py` clean (0 issues across 4 source files).

## 3. Architecture Decisions & Next Steps

- **Stateless HTTP Mode**: Configured FastMCP with `stateless_http=True` and `json_response=True` to seamlessly support Cloud Run serverless concurrency, load balancing, and direct JSON-RPC client interactions without requiring persistent in-memory session pinning.
- **Strict File Boundaries**: Honored file ownership constraints by strictly touching only Phase 2 files and avoiding modifications to `app/`, `tests/test_skill_registry.py`, and `infra/terraform/`.
- **Handoff to Phase 3**: Phase 3 will integrate dynamic tool discovery (`AgentRegistry.get_mcp_toolset`) and turn-level hot-reloading with in-memory TTL caching in the main agent service.
