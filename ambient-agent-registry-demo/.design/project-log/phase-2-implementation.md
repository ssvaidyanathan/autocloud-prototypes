# Phase 2 Implementation Log: Remote Tool Provider & Demo Tooling

## Date
2026-10-20

## Author
`ambient-dev-p2`

## Status
COMPLETED

## Objectives
Implement Phase 2 for `ambient-agent-registry-demo`:
1. FastMCP Calculator Service (`ambient-calculator-service`) exposing `/mcp` and `/health`.
2. Registry Skill Packages (`ambient-privilege-audit-skill` v1 and v2) with zero executable scripts.
3. Registration & Publishing scripts (`register_tool.sh`, `publish_skill.sh`, `check-no-literal-projects.sh`).
4. End-to-end hot-reload demo runner (`run_demo.sh`) rewritten for native Google ADK synchronous `POST /run` returning `list[Event]`, alongside mock server (`mock_server.py`).

---

## Deliverables & Architecture

### 1. FastMCP Calculator Service (`demo/calculator_service/`)
- **`server.py`**:
  - Implements FastMCP service named `ambient-calculator-service` with precision arithmetic tools `add`, `multiply`, and `power`.
  - Exposes `/mcp` JSON-RPC stream endpoint.
  - Exposes unauthenticated `/health` probe for Cloud Run container lifecycle checks.
  - Implements `AuthMiddleware` with Bearer token validation and optional Google OIDC audience verification, exempting `/health` and `OPTIONS` CORS preflight requests.
- **`main.py`**: Uvicorn server entrypoint listening on `HOST` and `PORT` (default 8080).
- **`__init__.py`**: Exports service tools and application factory.
- **`pyproject.toml`**: Package metadata and dependency definitions for Hatchling build.
- **`Dockerfile`**: Multi-stage build using `python:3.12-slim` and non-root execution (`appuser:10001`).

### 2. Registry Packages (`demo/registry_packages/`)
- **`v1/calculator/SKILL.md`**:
  - Skill name: `ambient-privilege-audit-skill`.
  - Allowed tools & `adk_additional_tools`: `add`, `multiply`.
  - Enforces mandatory tool execution for arithmetic and standard format `Result: <value> (Computed via <tools_used>)`.
  - References: `references/arithmetic_rules.md`.
  - Guardrail: ZERO executable scripts in directory.
- **`v2/calculator/SKILL.md`**:
  - Skill name: `ambient-privilege-audit-skill`.
  - Allowed tools & `adk_additional_tools`: `add`, `multiply`, `power`.
  - Enforces exponentiation tool execution and emoji format `🧮 [CALCULATOR-V2] Computed value: **<value>**`.
  - References: `references/arithmetic_rules.md`.
  - Guardrail: ZERO executable scripts in directory.

### 3. Registry & Validation Scripts (`scripts/`)
- **`register_tool.sh`**:
  - Registers `ambient-calculator` service in GCP Agent Registry Tools Registry (`https://agentregistry.googleapis.com/v1`) under `mcpServers`.
  - Configurable with `--dry-run`, `--project`, `--location` (default `us-central1`), `--service-name`, and `--tool-url`.
- **`publish_skill.sh`**:
  - Packages skill folder into zip archive and publishes revision to GCP Agent Registry (`https://agentregistry.googleapis.com/v1alpha`).
  - Strict security guardrail rejecting packages containing a `scripts/` folder.
  - Configurable with `--dry-run`, `--project`, `--location` (default `global`), and `--set-default`.
- **`check-no-literal-projects.sh`**:
  - Validates zero forbidden environment-specific identifiers (e.g. `FORBIDDEN_LITERALS`) and un-externalized Gemini model literals in code files.
- **`mock_server.py`**:
  - Lightweight FastAPI mock server exposing `/health`, `/admin/cache/invalidate`, and native ADK `POST /run`.
  - Generates ADK-compliant `list[Event]` sequences with `functionCall` and `functionResponse` for offline `--mock-mode` testing.
- **`run_demo.sh`**:
  - Orchestrates the full 2-act live hot-reload demonstration using native ADK `POST /run`:
    - **Step 1:** Verifies `GET /health` probe (HTTP 200).
    - **Step 2 (Act 1):** Sends `RunAgentRequest` to `POST /run` for compound prompt ("Compute 15 multiplied by 4, then add 10."). Uses `jq` to verify `functionCall` and `functionResponse` for `multiply` and `add` in the `list[Event]` trace, and asserts final answer `Result: 70`.
    - **Step 3 (Live Mutation):** Publishes `r2` revision of `ambient-privilege-audit-skill` and invalidates in-memory cache via `POST /admin/cache/invalidate`.
    - **Step 4 (Act 2):** Sends `RunAgentRequest` to `POST /run` for exponentiation prompt ("Compute 2 to the power of 8."). Uses `jq` to verify `functionCall` and `functionResponse` for newly unlocked tool `power`, and asserts final answer `256`.
    - **Step 5 (Uptime):** Verifies zero container restarts and continuous process uptime.
  - Supports `--dry-run` and `--mock-mode`.

---

## Verification Results

| Check / Test Command | Result | Notes |
|---|---|---|
| `bash ambient-agent-registry-demo/scripts/run_demo.sh --dry-run` | **PASS** | Validates dry-run output and JSON payloads |
| `bash ambient-agent-registry-demo/scripts/run_demo.sh --mock-mode` | **PASS** | Validates all 5 acts with mock ADK server |
| `bash ambient-agent-registry-demo/scripts/register_tool.sh --dry-run ambient-calculator http://localhost:8080/mcp` | **PASS** | Validates gcloud registration commands |
| `bash ambient-agent-registry-demo/scripts/publish_skill.sh --dry-run ambient-privilege-audit-skill r1 ambient-agent-registry-demo/demo/registry_packages/v1/calculator` | **PASS** | Validates package guardrails and gcloud revision commands |
| `FORBIDDEN_LITERALS="<TARGET_PROJECT_ID>" bash ambient-agent-registry-demo/scripts/check-no-literal-projects.sh` | **PASS** | Zero forbidden project literals and zero un-externalized models |
| `ruff check ambient-agent-registry-demo/demo ambient-agent-registry-demo/scripts` | **PASS** | Zero linting errors |
