# Project Log: Phase 5 - Demo Tooling & End-to-End Validation

**Date**: 2026-10-20  
**Phase**: Phase 5: Demo Tooling & End-to-End Validation  
**Agent**: `ard-dev-p5`  
**Branch**: `feat/agent-registry-demo`  

## 1. Summary of Changes

Implemented Phase 5: Demo Tooling, Automated Scripts, End-to-End Integration Tests, and Comprehensive Documentation for the Agent Registry Demo prototype:

1. **`demo/registry_packages/`**:
   - `demo/registry_packages/v1/calculator/`:
     - `SKILL.md`: Frontmatter declaring `name: calculator`, `allowed-tools: add multiply`, and `metadata.adk_additional_tools: [add, multiply]`. Directives mandate standard formatting: `"Result: <value> (Computed via <tools_used>)"`.
     - `references/arithmetic_rules.md`: Domain reference guide for arithmetic operations.
     - Confirmed zero executable `scripts/` directory.
   - `demo/registry_packages/v2/calculator/`:
     - `SKILL.md`: Frontmatter declaring `name: calculator`, `allowed-tools: add multiply power`, and `metadata.adk_additional_tools: [add, multiply, power]`. Directives mandate emoji formatting: `"🧮 [CALCULATOR-V2] Computed value: **<value>**"`.
     - `references/arithmetic_rules.md`: Domain reference guide including exponentiation conventions and boundary conditions.
     - Confirmed zero executable `scripts/` directory.

2. **`scripts/publish_skill.sh`**:
   - Validates that target source directory contains `SKILL.md` and strictly enforces prohibition of forbidden `scripts/` directories.
   - Packages source directory into an in-memory/temporary ZIP archive using standard Python `zipfile`.
   - Generates and executes `gcloud alpha agent-registry skills revisions create` and `skills update` commands to promote `defaultRevision`.
   - Supports both space-separated and `=` flag formats (`--project`, `--location`, `--skill-name`, `--revision-id`, `--source-dir`, `--set-default`, `--dry-run`).
   - Supports `--dry-run` mode for safe pre-flight validation.

3. **`scripts/mock_server.py`**:
   - Lightweight ephemeral mock server hosting FastAPI `app` with `GCPSkillRegistryClient` mock transport.
   - Powers `--mock-mode` in `run_demo.sh` to allow 100% offline local verification without GCP credentials.

4. **`scripts/run_demo.sh`**:
   - Automated live demonstration orchestrator featuring color-coded terminal output and automated assertions.
   - Executes 5-step lifecycle:
     1. Health & readiness probe (`GET /health`).
     2. Act 1 execution: `POST /chat` with arithmetic prompt (`"Compute 15 multiplied by 4, then add 10"`), asserting `multiply` and `add` invocation and Revision 1 formatting.
     3. Live Registry Mutation: publishes Revision 2 and triggers atomic cache invalidation via `POST /admin/cache/invalidate`.
     4. Act 2 execution: `POST /chat` with exponentiation prompt (`"Compute 2 to the power of 8"`), asserting `power` invocation and Revision 2 emoji formatting.
     5. Zero container restarts verification: validates process PID continuity and continuous HTTP 200 health.
   - Full support for `--agent-url=URL`, `--mock-mode` (local offline verification), and `--dry-run` (execution planning).

5. **`tests/test_e2e_hot_reload.py`**:
   - Comprehensive end-to-end integration test suite:
     - Package structure validation verifying `SKILL.md` frontmatter and zero `scripts/` directories.
     - End-to-end hot reload lifecycle test against `TestClient` using real packages from disk.
     - Security guardrail tests confirming `SkillsLoader` rejects malicious ZIP packages containing `scripts/`.
     - Dynamic tool access control test asserting undeclared tools (`power` in Revision 1) are blocked before invocation.
     - CLI execution tests verifying `publish_skill.sh` (`--dry-run` and rejection of `scripts/`) and `run_demo.sh` (`--dry-run` and `--mock-mode`).

6. **`agent-registry-demo/README.md`**:
   - Comprehensive technical documentation matching the high standard of `incident-sre-agent`.
   - High-level architecture overview and ASCII diagram.
   - Mermaid sequence diagram illustrating the complete request lifecycle and live mutation flow.
   - Rationale for pure in-memory ZIP packaging and zero disk persistence.
   - FastMCP remote tool model and IAM service-to-service security boundaries.
   - Step-by-step Terraform deployment instructions and outputs mapping.
   - Detailed walkthrough of Act 1, live registry update, and Act 2 scenarios with JSON payloads and log evidence.
   - Automated demo execution guide (`--mock-mode`, `--dry-run`, live URL).

## 2. Verification Results

- **Automated Tests**:
  - `pytest tests/`: **65 / 65 passed (100% pass rate)**.
- **Code Linter**:
  - `ruff check .`: **0 errors**.
- **Script Executions**:
  - `./scripts/publish_skill.sh --dry-run`: Passed.
  - `./scripts/publish_skill.sh` with forbidden `scripts/`: Rejected with security violation.
  - `./scripts/run_demo.sh --dry-run`: Passed.
  - `./scripts/run_demo.sh --mock-mode`: All 5 live steps and assertions passed with zero container restarts.
