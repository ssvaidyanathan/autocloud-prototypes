# Project Log: GitHub Actions Deployment Failure Investigator Prototype

## Entry: Phase 1 Minimal End-to-End Vertical Slice (The Core Reverse Flow)
- **Date**: 2026-10-16
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview & Objectives
Implemented Phase 1: Minimal End-to-End Vertical Slice under `gha-deploy-investigator/` inside `/scion-volumes/scratchpad/projects/gha-deploy-investigator/repo`. This establishes the core reverse flow:
1. Ingesting GitHub Actions `workflow_run` failure webhooks with HMAC SHA-256 signature verification.
2. Normalizing the failure event into the canonical `Incident` model.
3. Asynchronously investigating the failure using the read-only CI/CD log tools (`fetch_workflow_run_jobs`, `fetch_job_logs`).
4. Synthesizing diagnostic findings and root cause analysis into a canonical `Report`.
5. Delivering the result via OIDC-authenticated callback to the Bridge.
6. Opening a new ServiceNow incident ticket via Table API (`POST /api/now/table/incident`) with HTML work notes and attaching the standalone interactive HTML report via Attachment API (`POST /api/now/attachment/file`).
7. Posting a developer-facing Check Run link on GitHub (`POST /repos/{owner}/{repo}/check-runs`) directly referencing the created ServiceNow incident.

### 2. Deliverables Implemented
- **Sibling Prototype Scaffolding**:
  - `bridge/app/adapters/`: `base.py`, `github.py`, `servicenow.py`, `__init__.py`
  - `bridge/app/contracts/`: `incident.py`, `request.py`, `result.py`, `report.py`, `findings.py`, `__init__.py`
  - `bridge/app/core/`: `auth.py`, `dedup.py`, `tasks.py`, `__init__.py`
  - `bridge/app/templates/`: `renderer.py`, `__init__.py`
  - `bridge/app/config.py`, `bridge/app/main.py`, `bridge/pyproject.toml`
  - `bridge/tests/`: `conftest.py`, `fake_github_server.py`, `fake_ticket_server.py`, `test_adapters.py`, `test_contracts.py`, `test_github_adapter.py`, `test_servicenow_reverse.py`, `test_e2e_reverse.py`
  - `agent/app/contracts/`: 100% byte-identical to `bridge/app/contracts/`
  - `agent/app/tools/`: `base.py`, `registry.py`, `github_tools.py`, `__init__.py`
  - `agent/app/config.py`, `agent/app/dispatcher.py`, `agent/app/synthesizer.py`, `agent/app/callback.py`, `agent/app/main.py`, `agent/pyproject.toml`
  - `agent/tests/`: `conftest.py`, `test_dispatcher.py`, `test_github_tools.py`
  - `scripts/check-contracts.sh`: Contract synchronization verification script.

### 3. Key Invariants & Architectural Patterns
- **Contract Synchronization Invariant**: Canonical Pydantic v2 data models in `bridge/app/contracts/` and `agent/app/contracts/` are byte-identical. Verified by `scripts/check-contracts.sh` (exits 0).
- **Read-Only Tool Invariant**: All agent tools querying GitHub are decorated with `@readonly_tool` and registered in `ToolRegistry` with `is_mutation=False`. Verified by `test_readonly_tool_invariant` via `assert_all_readonly()`.
- **Query Caching**: `QueryCache` caches read queries within investigation sessions, avoiding redundant API calls.
- **Hermetic Test Infrastructure**: Fully offline test suite using `fake_github_server.py` and `fake_ticket_server.py`.

### 4. Verification & Test Results
- **Automated Test Suite**:
  - Command: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest -v`
  - Result: 22 passed in 0.31s (100% offline, zero network access).
- **Contract Synchronization Check**:
  - Command: `scripts/check-contracts.sh`
  - Result: SUCCESS: Contracts are byte-identical.
- **Linting & Code Quality**:
  - Command: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator`
  - Result: All checks passed!
