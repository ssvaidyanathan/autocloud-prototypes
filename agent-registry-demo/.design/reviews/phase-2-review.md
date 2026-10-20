# Commit 30d3c78: Phase 2 Remote Tool Provider Service (FastMCP Calculator) — Review

## Executive Summary
Phase 2 implements the remote tool provider service (`calculator-tool-service`) using FastMCP over Streamable HTTP (`/mcp`), container health probes (`GET /health`), Bearer and Google OIDC authentication middleware, and the `gcloud agent-registry services` registration automation. While the service implementation, JSON-RPC protocol handling, and test suites are exceptionally well written with 100% passing tests (17/17), the standalone Dockerfile packaging fails to build due to missing source files in the builder stage and missing Hatchling wheel configuration (Risk Level: MEDIUM).

---

## Review Scope & Summary of Changes
- **Target Commit**: `30d3c78` (`feat(agent-registry-demo): Phase 2 remote tool provider service (FastMCP calculator)`)
- **Author**: `ard-dev-p2`
- **Reviewer**: `ard-rev-p2` (Role: Senior Code Reviewer)
- **Repository Path**: `/scion-volumes/scratchpad/projects/agent-registry-demo/repo`
- **Reviewed Files**:
  - `agent-registry-demo/demo/calculator_service/server.py`: FastMCP server implementation exposing arithmetic operations (`add`, `multiply`, `power`), `/health` probe endpoint, Starlette `AuthMiddleware` with Bearer/OIDC validation, and `create_app()` factory.
  - `agent-registry-demo/demo/calculator_service/main.py`: Production entrypoint loading host, port, and log level from environment variables and running Uvicorn.
  - `agent-registry-demo/demo/calculator_service/__init__.py`: Package export interface for calculator service tools and app factory.
  - `agent-registry-demo/demo/calculator_service/pyproject.toml`: Standalone package manifest declaring dependencies (`fastmcp`, `fastapi`, `uvicorn`, `httpx`, `google-auth`).
  - `agent-registry-demo/demo/calculator_service/Dockerfile`: Multi-stage Dockerfile based on `python:3.12-slim` targeting Cloud Run port 8080 with non-root user `appuser` (UID 10001).
  - `agent-registry-demo/scripts/register_tool.sh`: Shell script automating `gcloud agent-registry services create/update` for `mcpServers` projection with dry-run support.
  - `agent-registry-demo/tests/test_calculator_service.py`: Test suite with 17 test cases verifying health probes, direct function execution, FastMCP JSON-RPC (`tools/list`, `tools/call`), auth rejection/acceptance, OIDC claims verification, and dry-run registration script output.
  - `agent-registry-demo/.design/project-log/2026-10-20-phase-2-calculator-service.md`: Delivery log for Phase 2.

---

## Verification of Acceptance Criteria

| # | Acceptance Criterion | Status | Evidence / Verification Method |
|---|----------------------|--------|--------------------------------|
| 1 | `calculator-tool-service` runs cleanly in a standalone container on port 8080 | **BLOCKED** | Service code (`main.py`, `server.py`) configures port 8080 cleanly. However, `demo/calculator_service/Dockerfile` builder stage fails during `pip install .` because source code is not copied and Hatchling package discovery fails (Finding 1). |
| 2 | FastMCP endpoint `/mcp` returns valid JSON-RPC responses for `tools/list` and `tools/call` | **PASS** | Verified via `test_mcp_tools_list` and `test_mcp_tools_call_*`. Returns standard JSON-RPC 2.0 payloads with structured content and schema properties. |
| 3 | Arithmetic operations exposed: `add(a, b)`, `multiply(a, b)`, and `power(base, exponent)` | **PASS** | Verified via unit tests (`test_direct_*`) and MCP RPC invocations (`test_mcp_tools_call_*`). |
| 4 | Health check endpoint `GET /health` returns HTTP 200 OK | **PASS** | Verified via `test_health_check_returns_200` and `test_health_check_bypasses_auth`. Returns `{"status": "healthy"}`. |
| 5 | Registration script `scripts/register_tool.sh` creates or updates `services create calculator` in Agent Registry | **PASS** | Verified against `gcloud agent-registry services` v1 CLI syntax and automated dry-run test (`test_register_tool_script_dry_run`). |
| 6 | Tool service supports rejecting unauthenticated requests when authentication is enabled | **PASS** | Verified via tests: rejects missing header (401), malformed/empty tokens (401), unauthorized service accounts (403), and expired tokens (401). |
| 7 | Run tests: `pytest tests/test_calculator_service.py` (must pass 100%) | **PASS** | 17 passed in 1.27s (100%). Full suite: 38 passed in 1.95s. |
| 8 | Run linter: `ruff check demo/ tests/test_calculator_service.py` (must be clean) | **PASS** | Clean (0 errors). |

---

## Critical
None.

---

## Required

### Finding 1: Dockerfile Builder Stage Fails on Package Installation
- **Category**: Architecture / Build & Deployment
- **Severity**: Required (Blocks Merge)
- **Files**:
  - `agent-registry-demo/demo/calculator_service/Dockerfile:13-14`
  - `agent-registry-demo/demo/calculator_service/pyproject.toml:14-16`
- **Code Snippet**:
  `demo/calculator_service/Dockerfile:13-14`:
  ```dockerfile
  13: COPY pyproject.toml /app/
  14: RUN pip install --no-cache-dir --prefix=/install .
  ```
  `demo/calculator_service/pyproject.toml:14-16`:
  ```toml
  14: [build-system]
  15: requires = ["hatchling"]
  16: build-backend = "hatchling.build"
  ```
- **Description**:
  In `demo/calculator_service/Dockerfile`, lines 13–14 attempt to install the application package into `--prefix=/install` by copying only `pyproject.toml` into `/app` and running `pip install .`.
  Because `pyproject.toml` specifies Hatchling as the build backend, Hatchling attempts to locate Python source packages matching the project name `calculator-tool-service` (looking for `calculator_tool_service/`, `src/calculator_tool_service/`, or `calculator_tool_service.py`).
  This fails for two reasons:
  1. No Python source code exists in `/app` during the builder step (only `pyproject.toml` was copied).
  2. Even when all files in `demo/calculator_service/` are present, the directory layout is flat (`main.py`, `server.py`, `__init__.py`) and there is no `calculator_tool_service/` subdirectory, nor is there a `[tool.hatch.build.targets.wheel]` configuration in `pyproject.toml` instructing Hatchling which modules to include.
  As a result, building the container image with `docker build` fails at step 14 with `hatchling.builders.exceptions.PackageFindError`.
- **Suggested Fix**:
  Either:
  **Option A (Recommended)**: Copy the service files into `/app` during the builder stage and configure Hatchling package discovery in `pyproject.toml`:
  In `demo/calculator_service/Dockerfile`:
  ```dockerfile
  COPY pyproject.toml /app/
  COPY __init__.py main.py server.py /app/
  RUN pip install --no-cache-dir --prefix=/install .
  ```
  And in `demo/calculator_service/pyproject.toml`:
  ```toml
  [tool.hatch.build.targets.wheel]
  packages = ["."]
  ```
  **Option B**: Install dependencies directly in the builder stage without packaging the local directory:
  ```dockerfile
  COPY pyproject.toml /app/
  RUN pip install --no-cache-dir --prefix=/install \
      "fastmcp>=0.1.0" \
      "fastapi>=0.110.0" \
      "uvicorn>=0.29.0" \
      "httpx>=0.27.0" \
      "google-auth>=2.29.0"
  ```

---

## Nit / Optional

### Finding 2: Per-Request `GoogleAuthRequest` / `requests.Session` Allocation in OIDC Middleware
- **Category**: Performance / Architecture
- **Severity**: Optional / Consider
- **File**: `agent-registry-demo/demo/calculator_service/server.py:121-130`
- **Code Snippet**:
  ```python
  121:         if self.verify_oidc:
  122:             try:
  123:                 from google.auth.transport.requests import Request as GoogleAuthRequest
  124:                 from google.oauth2 import id_token
  125: 
  126:                 claims: dict[str, Any] = id_token.verify_oauth2_token(
  127:                     token,
  128:                     GoogleAuthRequest(),
  129:                     audience=self.expected_audience,
  130:                 )
  ```
- **Description**:
  When `verify_oidc=True`, line 128 instantiates `GoogleAuthRequest()` on every incoming request. Internally, `google.auth.transport.requests.Request` constructs a new `requests.Session()` with new connection adapters. This defeats HTTP connection pooling, forces repeated TLS handshakes when retrieving Google's public signing keys, and induces socket allocation churn under high request rates on Cloud Run.
- **Suggested Fix**:
  Instantiate and reuse a single `GoogleAuthRequest` instance on the `AuthMiddleware` instance or module:
  ```python
  class AuthMiddleware(BaseHTTPMiddleware):
      def __init__(...):
          ...
          self._auth_request = GoogleAuthRequest() if verify_oidc else None
  ```
  And pass `self._auth_request` into `id_token.verify_oauth2_token()`.

---

### Finding 3: Unhandled Complex and Arithmetic Boundary Conditions in `power` Tool
- **Category**: Correctness
- **Severity**: Optional / Consider
- **File**: `agent-registry-demo/demo/calculator_service/server.py:58-69`
- **Code Snippet**:
  ```python
  58: @mcp.tool()
  59: def power(base: float, exponent: float) -> float:
  ...
  68:     return float(base**exponent)
  ```
- **Description**:
  In Python, evaluating fractional exponents of negative bases (e.g. `power(-4.0, 0.5)`) yields a `complex` number (`1.22e-16 + 2.0j`). Passing a complex number to `float()` raises `TypeError: can't convert complex to float`. In addition, zero raised to a negative power (`power(0.0, -1.0)`) raises `ZeroDivisionError: float division by zero`, and very large values (e.g. `power(10.0, 1000.0)`) raise `OverflowError`.
  FastMCP catches internal exceptions and returns a generic tool error with `isError: True`, but validating inputs and raising explicit `ValueError` exceptions with actionable messages provides clearer diagnostic feedback to the LLM agent.
- **Suggested Fix**:
  Add defensive boundary checks:
  ```python
  @mcp.tool()
  def power(base: float, exponent: float) -> float:
      if base == 0.0 and exponent < 0.0:
          raise ValueError("Division by zero: zero cannot be raised to a negative exponent")
      if base < 0.0 and not exponent.is_integer():
          raise ValueError("Fractional exponent of a negative base produces a complex number")
      return float(base**exponent)
  ```

---

### Finding 4: Exact Path Matching in Health Check Probe Bypass
- **Category**: Correctness / Resilience
- **Severity**: Nit / Optional
- **File**: `agent-registry-demo/demo/calculator_service/server.py:98`
- **Code Snippet**:
  ```python
  98:         if not self.enforce_auth or request.url.path == "/health" or request.method == "OPTIONS":
  99:             return await call_next(request)
  ```
- **Description**:
  The health check probe bypass performs an exact string comparison: `request.url.path == "/health"`. If an external load balancer, proxy, or health probe requests `/health/` with a trailing slash, the condition fails and subjects the request to authentication, returning HTTP 401 Unauthorized.
- **Suggested Fix**:
  Strip trailing slashes when checking the health path:
  ```python
  if not self.enforce_auth or request.url.path.rstrip("/") == "/health" or request.method == "OPTIONS":
  ```

---

### Finding 5: Registration Script CLI Option Parser Does Not Support Space-Separated Flag Arguments
- **Category**: Readability / Usability
- **Severity**: Nit / Optional
- **File**: `agent-registry-demo/scripts/register_tool.sh:56-90`
- **Code Snippet**:
  ```bash
  58:     --dry-run)
  59:       DRY_RUN=true
  60:       shift
  61:       ;;
  62:     --project=*)
  63:       PROJECT_ID="${1#*=}"
  64:       shift
  65:       ;;
  ```
- **Description**:
  The option parser in `scripts/register_tool.sh` matches only `--flag=value` syntax. If a user or script invokes `./scripts/register_tool.sh --project my-project calculator https://...`, `--project` is matched by `-*` and the script terminates with `Error: Unknown option --project`. Standard POSIX/GNU CLI conventions support both space-separated and equals-separated flag syntax.
- **Suggested Fix**:
  Add space-separated flag matching to the `case` loop:
  ```bash
  --project)
    PROJECT_ID="$2"
    shift 2
    ;;
  --project=*)
    PROJECT_ID="${1#*=}"
    shift
    ;;
  ```

---

### Finding 6: Dual Relative/Absolute Import Fallback Pattern in `main.py`
- **Category**: Architecture / Readability
- **Severity**: Nit / Optional
- **File**: `agent-registry-demo/demo/calculator_service/main.py:12-23`
- **Code Snippet**:
  ```python
  12: try:
  13:     from .server import add, app, create_app, health_check, mcp, multiply, power
  14: except ImportError:
  15:     from server import (  # type: ignore[no-redef]
  ...
  23:     )
  ```
- **Description**:
  `demo/calculator_service/main.py` includes a `try/except ImportError` shim to allow running both as a package (`python -m demo.calculator_service.main`) and as a standalone script (`python main.py`). Because `__init__.py` exists in the same directory, Mypy reports duplicate module definitions when executed directly unless `--explicit-package-bases` is passed. Standardizing on absolute package imports or setting `PYTHONPATH=/app` (as in Dockerfile) eliminates the need for the fallback shim.
- **Suggested Fix**:
  Use consistent module imports and execute via `python -m calculator_service.main` or configure `PYTHONPATH`.

---

## FYI

### Finding 7: Token Verification Behavior When `verify_oidc` is Disabled
- **Category**: Security / Documentation
- **Severity**: FYI
- **File**: `agent-registry-demo/demo/calculator_service/server.py:101-120`
- **Description**:
  When `enforce_auth=True` but `verify_oidc=False`, `AuthMiddleware` verifies only that an `Authorization: Bearer <token>` header is present and non-empty. Any arbitrary string is accepted. While this is intentional and useful for offline development and local test fixtures, production deployments on Cloud Run must ensure `VERIFY_OIDC=true` and `EXPECTED_AUDIENCE` are configured in environment variables to enforce cryptographic token validation.

---

### Finding 8: Exit Code Ambiguity in Registry Service Existence Check
- **Category**: Operational / Reliability
- **Severity**: FYI
- **File**: `agent-registry-demo/scripts/register_tool.sh:122-130`
- **Description**:
  In `register_tool.sh`, `gcloud agent-registry services describe` treats any non-zero exit code as "service does not exist" and falls back to `services create`. If the failure is caused by an expired gcloud token, missing IAM permissions, or network failure rather than `NOT_FOUND`, the subsequent `create` command will also fail. Parsing `gcloud` output or checking for `NOT_FOUND` specifically improves error clarity in CI/CD pipelines.

---

## Positive Feedback
- **FastMCP Stateless HTTP Mode**: Configuring FastMCP with `stateless_http=True` and `json_response=True` is an ideal architectural choice for Cloud Run. It avoids in-memory session pinning and supports seamless horizontal auto-scaling and stateless request dispatch.
- **Comprehensive FastMCP Test Suite**: `test_calculator_service.py` provides 17 high-quality test cases covering JSON-RPC 2.0 wire formats, input schema validation, tool invocation results, auth bypass for health probes, and Google OIDC claim validation.
- **Accurate Tools Registry v1 CLI Syntax**: `register_tool.sh` matches the exact `gcloud agent-registry services create/update` syntax required by the Agent Registry v1 API, including `--mcp-server-spec-type=no-spec` and `--interfaces="protocolBinding=http-json,url=..."`.
- **Security-Minded Auth Middleware**: `AuthMiddleware` provides clean separation of concerns: health probes and CORS preflights are exempted while protected routes enforce Bearer tokens and validate Google ID tokens, audience claims, and allowed service account emails.

---

## Test Coverage
- **Automated Tests**: 17 / 17 passed (100% pass rate) in `tests/test_calculator_service.py`.
- **Combined Regression Suite**: 38 / 38 passed (100% pass rate) across `test_calculator_service.py` and `test_skill_registry.py`.
- **Coverage Areas**:
  - `test_health_check_returns_200`: HTTP 200 OK and healthy status.
  - `test_health_check_bypasses_auth`: Verification that health probes remain unauthenticated.
  - `test_direct_add`, `test_direct_multiply`, `test_direct_power`: Unit math correctness.
  - `test_mcp_tools_list`: JSON-RPC protocol compliance and schema property inspection.
  - `test_mcp_tools_call_add`, `test_mcp_tools_call_multiply`, `test_mcp_tools_call_power`: Accurate JSON-RPC tool dispatch.
  - `test_mcp_tools_call_unknown_tool`: Error response handling on non-existent tools.
  - `test_auth_rejected_when_token_missing`, `test_auth_rejected_when_token_malformed`: 401 Unauthorized enforcement.
  - `test_auth_accepted_with_valid_bearer_token`: 200 OK with valid bearer token.
  - `test_auth_with_oidc_verification_success`: Google OIDC token claim decoding and verification.
  - `test_auth_with_oidc_verification_unauthorized_service_account`: 403 Forbidden enforcement on untrusted service accounts.
  - `test_auth_with_oidc_verification_invalid_token`: 401 Unauthorized on expired or invalid OIDC tokens.
  - `test_register_tool_script_dry_run`: Validation of `scripts/register_tool.sh` flag generation and gcloud commands.

---

## Backward Compatibility
The Phase 2 changes introduce a standalone remote microservice (`demo/calculator_service/`), a tool registration utility (`scripts/register_tool.sh`), and dedicated tests (`tests/test_calculator_service.py`). No changes were made to existing Phase 1 modules (`app/`, `tests/test_skill_registry.py`). Zero backward compatibility regressions.

---

## Final Verdict
**REQUEST CHANGES**

Phase 2 implementation exhibits high engineering quality across server implementation, JSON-RPC protocol handling, and automated test coverage. However, merge is gated on resolving **Finding 1** (Required): `demo/calculator_service/Dockerfile` builder stage fails to build due to missing source files and missing Hatchling wheel configuration. Once the Dockerfile packaging is corrected and verified, the change is ready for approval.

### Gates Run:
1. `pytest tests/test_calculator_service.py`: 17 passed in 1.27s (100%).
2. `pytest tests/`: 38 passed in 1.95s (100%).
3. `ruff check demo/ tests/test_calculator_service.py`: Clean (0 errors).
4. `ruff check .`: Clean (0 errors).
5. `mypy --explicit-package-bases demo/calculator_service tests/test_calculator_service.py`: Clean (0 issues across 4 source files).
6. `scripts/register_tool.sh --dry-run`: Clean (0 errors, outputs verified gcloud syntax).

### Gates Not Run (Environment Limitations):
- `docker build demo/calculator_service/`: Docker CLI and container daemon are not installed in the review container (`docker: command not found`). The build failure was verified via static analysis of the Dockerfile builder layer steps and Hatchling packaging requirements.
