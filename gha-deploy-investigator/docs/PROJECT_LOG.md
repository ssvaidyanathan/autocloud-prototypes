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

## Entry: Phase 1 Round 1 Code Review Findings Resolution
- **Date**: 2026-10-16
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview
Resolved all 8 findings (3 blocking, 5 non-blocking) identified by reviewer `gha-rev-1` in `reviews/phase1-round1-review.md` following review of commit `e35e0db`.

### 2. Resolution Details per Finding
1. **Finding 1 (Blocking) - AST Guard Violation in `agent/app/synthesizer.py`**:
   - Fixed fallback text construction in `ProbableRootCause(text=...)` to begin with the mandatory fallback label:
     `Automated fallback (LLM unavailable): Pipeline step '{step_name}' in job '{job_name}' failed: {primary_error}`.
   - Verified via `scripts/check-no-rootcause-overrides.py` and incident-sre-agent's AST check script, confirming 0 violations.
2. **Finding 2 (Blocking) - Dead / Bypassed Test Assertion in `bridge/tests/test_adapters.py`**:
   - Removed bypassed `if False else None` assignment and `assert res is None`.
   - Constructed a valid `InvestigationResult` with `sample_incident` and `sample_report`, called `IncidentRef.from_result(res)`, and verified all fields (`id`, `source`, `external_id`, `external_key`) match `sample_incident`.
3. **Finding 3 (Blocking) - Dynamic OIDC Bearer Token in `agent/app/callback.py`**:
   - Replaced hardcoded mock token with dynamic OIDC token acquisition mirroring `incident-sre-agent/agent/app/callback.py`.
   - In local/dev environments (`APP_ENV` / `ENVIRONMENT` in `("local", "dev", "test", ...)` or `config.is_local`), falls back to `mock-oidc-token-for-testing`.
   - In production environments, queries the Google Cloud metadata server (`http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}`) with `Metadata-Flavor: Google`, raising `RuntimeError` if unavailable.
   - Added unit test in `agent/tests/test_dispatcher.py` verifying dynamic token acquisition, custom token providers, production metadata failure, and local fallback behavior.
4. **Finding 4 (Non-Blocking) - Dead Helper Function in `agent/app/tools/github_tools.py`**:
   - Integrated `_build_client(client, config)` into `fetch_workflow_run_jobs` and `fetch_job_logs`, eliminating duplicated client instantiation and cleanup logic.
5. **Finding 5 (Non-Blocking) - Custom `cast` in `agent/app/tools/registry.py`**:
   - Removed redundant custom `def cast` re-implementation and imported standard `cast` from `typing`.
6. **Finding 6 (Non-Blocking) - Defensive Repo Validation in `bridge/app/adapters/github.py`**:
   - Added guard checking `if not incident.github_head_sha or not repo_str or "/" not in repo_str: logger.warning(...); return` in `post_report_link`, preventing malformed Check Run URL calls.
7. **Finding 7 (Non-Blocking) - Non-Failure Webhook Filtering in `bridge/app/main.py`**:
   - Updated `_process_webhook` to catch `ValueError` from `adapter.to_incident` when non-failure events (e.g. conclusion != "failure") are ingested.
   - Returns HTTP 200 with `{"status": "ignored", "reason": str(exc)}`.
   - Added end-to-end test `test_non_failure_webhook_ignored_with_http_200` in `bridge/tests/test_e2e_reverse.py`.
8. **Finding 8 (Non-Blocking) - HTML Slicing in `chunk_servicenow_work_note`**:
   - Pre-truncated raw summary text and evidence item excerpts before HTML formatting in `chunk_servicenow_work_note`, ensuring rendered HTML stays within ServiceNow character limits without slicing mid-tag.

### 3. Verification Results
- **Contract Sync Check**: `bash scripts/check-contracts.sh` -> 0 diff, byte-identical.
- **Pytest Suite**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest -v` -> 24 passed (100% pass).
- **Ruff Linter**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator` -> 0 errors ("All checks passed!").
- **AST Guard Check**: `python3 scripts/check-no-rootcause-overrides.py agent/app/synthesizer.py agent/app/dispatcher.py` -> 0 violations.

## Entry: Phase 2 Multi-Stage Failure Coverage & Specialized Diagnostics
- **Date**: 2026-10-16
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview & Objectives
Implemented Phase 2: Multi-Stage Failure Coverage & Specialized Diagnostics per `design.md` Section 3.3 and Section 6. Expanded the single-stage prototype into an intelligent multi-stage diagnostic system that classifies failure stages, delegates investigation to specialized diagnostic agents, correlates multi-source telemetry (CI logs, commit diffs, Cloud Run revisions, Cloud Logging stderr), and writes comprehensive diagnostics back to developers via GitHub Check Runs and PR/Commit comments with links to ServiceNow.

### 2. Deliverables Implemented
1. **GitHub App Auth & REST Client (`agent/app/github/`)**:
   - `auth.py`: `GitHubAppAuth` generating RS256 JWT signed with App Private Key (PEM) or falling back to local test tokens in `APP_ENV == "local"`. Handles installation token exchange (`POST /app/installations/{installation_id}/access_tokens`) and caches access tokens with expiration TTL.
   - `client.py`: `GitHubRestClient` read-only client with methods: `get_workflow_run_jobs`, `get_job_logs`, `get_commit_diff`, and `get_file_content`.
2. **Specialized Diagnostic Specialists (`agent/app/specialists/`)**:
   - `base.py`: `BaseSpecialist` interface defining `diagnose(...)` returning `DiagnosticResult` (stage, specialist name, summary, probable cause, confidence, evidence items, remediation actions, suggested fix, annotations).
   - `pipeline_logs.py` (`PipelineLogsSpecialist`):
     - **Lint / Static Analysis**: Regex parses `ruff`, `flake8`, `mypy`, `eslint` outputs; extracts file, line number, rule, message; correlates with git commit diffs (`get_commit_diff`).
     - **Unit / Integration Tests**: Parses `pytest` and `jest` failures, extracting failing test name, assertion error message, and bottom-frame traceback.
     - **Build & Packaging**: Parses Docker build errors (`COPY failed`, `RUN non-zero code`) and package conflicts (`ResolutionImpossible`).
   - `runtime_health.py` (`RuntimeHealthSpecialist`):
     - Diagnoses Cloud Run deployment failures by querying revision condition status (`describe_cloud_run_revision`), detecting container termination exit codes (e.g. exit code 137 OOM) and probe timeouts.
     - Correlates with container stderr logs via Cloud Logging tool (`query_cloud_logging`).
   - `iac_config.py` (`IaCConfigSpecialist`):
     - Analyzes Terraform plan/apply execution errors (`TERRAFORM_ERROR_PATTERN`) and YAML manifest errors.
3. **Read-Only Tools Expansion (`agent/app/tools/`)**:
   - `github_tools.py`: Added `@readonly_tool` implementations for `get_commit_diff(repo, head_sha)` and `get_file_content(repo, path, ref)`.
   - `runtime_tools.py`: Added `@readonly_tool` implementations for `describe_cloud_run_revision(service_name, revision_name, region)` and `query_cloud_logging(filter_query, limit)`.
4. **CI/CD Runbooks & Progressive Disclosure (`agent/skills/`)**:
   - `skills.yaml` mapping diagnostic stages and specialists to runbooks.
   - Markdown runbooks in `skills/`: `lint-troubleshooting/SKILL.md`, `test-failure-triage/SKILL.md`, `container-build-troubleshooting/SKILL.md`, `cloudrun-deploy-troubleshooting/SKILL.md`.
5. **Multi-Stage Classifier & Dispatcher Routing (`agent/app/dispatcher.py` & `synthesizer.py`)**:
   - `classify_stage`: Classifies failing steps into `deploy`, `iac_config`, `lint`, `test`, `build`, or `pipeline_logs`.
   - `InvestigationDispatcher`: Routes failing jobs/steps to matching specialists, collects diagnostics, and supplies specialist findings and annotations to `synthesizer.py`.
   - `synthesizer.py`: Generates canonical `Report` with `ProbableRootCause` complying with AST static guard (`Automated fallback (LLM unavailable): ...`).
6. **Developer-Facing Write-Back Enhancement (`bridge/app/adapters/github.py`)**:
   - In `post_report_link`, posts Check Run annotations (culprit file, line, rule, title, message) and posts PR / Commit comments with collapsible `<details>` Markdown sections containing:
     - Probable root cause callout
     - Failing stage and error excerpt
     - Direct hyperlink to created ServiceNow ticket (`INC...`)
7. **Hermetic Test Infrastructure Expansion**:
   - `bridge/tests/fake_github_server.py`: Added handlers for installation tokens, commit diffs, file contents, PR comments, and commit comments.
   - `agent/tests/test_specialists.py`: 6 tests covering all specialists across lint, pytest, Docker build, Cloud Run exit 137 OOM, Cloud Logging stderr correlation, and Terraform errors.
   - `agent/tests/test_github_app.py`: 4 tests for `GitHubAppAuth` and `GitHubRestClient`.
   - `agent/tests/test_dispatcher.py`: Multi-stage tests verifying end-to-end diagnosis across all 4 stages (lint, test, build, deploy).
   - `bridge/tests/test_github_adapter.py`: Added tests for Check Run annotations and PR comment posting.

### 3. Key Invariants & Architectural Patterns
- **Strict Read-Only Guarantee**: All tools in `github_tools.py` and `runtime_tools.py` are registered as `@readonly_tool`. Verified by `test_readonly_tool_invariant`.
- **Contract Byte-Identity**: Verified by `scripts/check-contracts.sh` (0 differences).
- **AST Root Cause Guard**: Verified zero post-LLM template overrides via `scripts/check-no-rootcause-overrides.py`.
- **100% Offline Hermetic Tests**: Zero network calls, completely isolated within fake test servers.

### 4. Verification & Test Results
- **Contract Synchronization Check**:
  - Command: `bash gha-deploy-investigator/scripts/check-contracts.sh`
  - Result: `SUCCESS: Contracts are byte-identical.`
- **AST Root Cause Guard**:
  - Command: `python3 incident-sre-agent/scripts/check-no-rootcause-overrides.py gha-deploy-investigator/agent/app/synthesizer.py gha-deploy-investigator/agent/app/dispatcher.py`
  - Result: `[PASS] Zero post-LLM root cause template overrides or regex classifiers found.`
- **Automated Pytest Suite**:
  - Command: `PYTHONPATH=gha-deploy-investigator/agent:gha-deploy-investigator/bridge /scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest gha-deploy-investigator/agent/tests gha-deploy-investigator/bridge/tests`
  - Result: `39 passed in 0.87s` (21 agent tests, 18 bridge tests).
- **Ruff Linter**:
  - Command: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator`
  - Result: `All checks passed!`

## Entry: Phase 2 Round 1 Code Review Findings Resolution
- **Date**: 2026-10-16
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview
Resolved all 6 findings (3 blocking, 3 non-blocking) identified by reviewer `gha-rev-3` in `reviews/phase2-round1-review.md` following review of commit `5622913`.

### 2. Resolution Details per Finding
1. **Finding 1 (Blocking) - Fatal Pydantic `ValidationError` in `synthesizer.py`**:
   - Removed forbidden `source="ci_logs"` argument from the fallback `EvidenceItem(...)` instantiation in `agent/app/synthesizer.py`.
   - Added unit test `test_synthesize_report_fallback_path` in `agent/tests/test_dispatcher.py` verifying fallback report synthesis both when `diagnostic_result=None` and when `evidence_items=[]`, asserting report generation succeeds without `ValidationError`.
2. **Finding 2 (Blocking) - GitHub App JWT Lifetime Exceeds GitHub API 10-Minute Maximum**:
   - In `agent/app/github/auth.py`, updated `generate_jwt` to calculate `iat = now - 60` and `exp = iat + min(expiration_seconds, 600)`, guaranteeing that `exp - iat <= 600` (<= 10 minutes) under all circumstances.
   - Updated `test_github_app_auth_rs256_generation` in `agent/tests/test_github_app.py` verifying `exp - iat == 300 <= 600`, and that requesting 1200s is capped at 600s.
3. **Finding 3 (Blocking) - Unanchored Substring Matching in `classify_stage`**:
   - In `agent/app/dispatcher.py`, replaced unanchored `in` substring checks with compiled regex patterns using word boundaries `\b`:
     - `STAGE_DEPLOY_RE`: `r"\b(deploy|deployments?|rollout|release|cloudrun|gcloud)\b"`
     - `STAGE_IAC_RE`: `r"\b(terraform|iac|manifest|manifests|tf)\b"`
     - `STAGE_LINT_RE` & `STAGE_CHECK_RE`: `r"\b(lint|linters?|ruff|flake8|mypy|eslint|fmt|syntax)\b"` and `r"\bcheck\b"`
     - `STAGE_TEST_RE`: `r"\b(tests?|pytest|jest|unit|integration|spec)\b"`
     - `STAGE_BUILD_RE`: `r"\b(build|builds?|docker|image|images?|container|containers?|package|compile|push)\b"`
   - Added unit test `test_classify_stage_tricky_cases` in `agent/tests/test_dispatcher.py` validating that `"Build platform image"` classifies as `build` (not `iac_config`), `"Platform Tests"` classifies as `test`, `"Checkout code"` classifies as `pipeline_logs` (not `lint`), and explicit stages classify accurately.
4. **Finding 4 (Non-Blocking) - Canonical Skill Mapping in `synthesizer.py`**:
   - Defined `STAGE_TO_SKILL` mapping in `synthesizer.py` aligning with the actual runbook folders in `agent/skills/`:
     - `lint` -> `lint-troubleshooting`
     - `test` -> `test-failure-triage`
     - `build` -> `container-build-troubleshooting`
     - `deploy` -> `cloudrun-deploy-troubleshooting`
     - `iac_config` -> `cloudrun-deploy-troubleshooting`
   - Populated `Report.skills_used` via `[STAGE_TO_SKILL.get(stage_label, "container-build-troubleshooting")]`.
5. **Finding 5 (Non-Blocking) - Unused `ESLINT_PATTERN` in `pipeline_logs.py`**:
   - Removed unused regex definition `ESLINT_PATTERN` from `agent/app/specialists/pipeline_logs.py`.
6. **Finding 6 (Non-Blocking) - Path Sanitization for Check Run Annotations**:
   - In `agent/app/specialists/pipeline_logs.py` and `bridge/app/adapters/github.py`, sanitized file paths via `re.sub(r"^\.?/", "", path)` stripping leading `./` or `/` to prevent GitHub API HTTP 422 Unprocessable Entity responses.
   - Added unit test `test_post_report_link_annotation_path_sanitization` in `bridge/tests/test_github_adapter.py`.

### 3. Verification Results
- **Contract Synchronization Check**:
  - Command: `bash gha-deploy-investigator/scripts/check-contracts.sh`
  - Result: `SUCCESS: Contracts are byte-identical.`
- **AST Root Cause Guard**:
  - Command: `python3 incident-sre-agent/scripts/check-no-rootcause-overrides.py gha-deploy-investigator/agent/app/synthesizer.py gha-deploy-investigator/agent/app/dispatcher.py`
  - Result: `[PASS] Zero post-LLM root cause template overrides or regex classifiers found.`
- **Automated Pytest Suite**:
  - Command: `PYTHONPATH=gha-deploy-investigator/agent:gha-deploy-investigator/bridge /scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest gha-deploy-investigator/agent/tests gha-deploy-investigator/bridge/tests -v`
  - Result: `42 passed in 0.99s` (23 agent tests, 19 bridge tests).
- **Ruff Linter**:
  - Command: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator`
  - Result: `All checks passed!`

## Entry: Phase 3 Dedicated Demo App & Multi-Stage Pipeline
- **Date**: 2026-10-16
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview & Objectives
Implemented Phase 3: Dedicated Demo App with Multi-Stage GitHub Actions Pipeline per `design.md` Section 3.5 & Section 6, and Amendments 6 & 7. Created a self-contained, production-grade FastAPI order processing microservice under `demo/sample-pipeline-app/` with a 4-stage GitHub Actions CI/CD pipeline, reproducible deterministic fault injection scripts, a standalone repository seeding script for Phase 5 live deployment, and automated integration tests verifying fault injection lifecycles and autonomous agent diagnosis.

### 2. Deliverables Implemented
1. **Dedicated Demo Microservice (`demo/sample-pipeline-app/`)**:
   - `app/config.py`: Application configuration model with failure simulation flags (`DEMO_CRASH_ON_STARTUP`, `DEMO_ALLOCATE_OOM`).
   - `app/main.py`: Production-grade FastAPI service with lifespan handler and endpoints:
     - `GET /`: Service metadata and environment.
     - `GET /health`: Readiness and liveness probe.
     - `GET /api/v1/orders`: List confirmed customer orders.
     - `POST /api/v1/orders`: Create new customer orders with itemized pricing.
     - `GET /api/v1/orders/{order_id}`: Retrieve existing order by ID (with 404 handling).
   - `tests/test_main.py`: Unit test suite verifying endpoints and business logic.
   - `Dockerfile`: Multi-stage build (builder and runtime stages).
   - `pyproject.toml`: Dependency declarations, package configuration, and ruff settings.
   - `scripts/deploy.sh`: Executable Cloud Run deployment script with revision tagging and dry-run/simulation mode.
   - `README.md`: Comprehensive documentation covering architecture, local development, pipeline stages, and fault injection runbook.
2. **Multi-Stage GitHub Actions Workflow (`.github/workflows/pipeline.yml`)**:
   - Sequenced 4-job pipeline triggered on push and pull requests:
     1. `lint`: Runs `ruff check app/`
     2. `unit-test`: Needs `lint`, runs `pytest tests/ -v`
     3. `build-container`: Needs `unit-test`, runs `docker build -t sample-pipeline-app:${{ github.sha }} .`
     4. `deploy-cloudrun`: Needs `build-container`, deploys to Cloud Run with revision tagging.
3. **Reproducible Fault Injection Scenarios (`scenarios/`)**:
   - `trigger-lint-failure.sh`: Injects unused imports (`F401`) and multiple statements on a single line (`E701`) into `app/main.py`.
   - `trigger-test-failure.sh`: Appends failing assertion `test_order_discount_calculation_failure` into `tests/test_main.py`.
   - `trigger-build-failure.sh`: Appends invalid `COPY missing_asset_configuration.json` instruction to `Dockerfile`.
   - `trigger-deploy-failure.sh`: Injects `DEMO_ALLOCATE_OOM=true` and memory constraint `128Mi` into `scripts/deploy.sh`.
   - `reset-scenarios.sh`: Reverts all injected code and configuration modifications back to pristine clean state.
4. **Standalone Repository Seed Script (`seed-demo-repo.sh`)**:
   - Script `seed-demo-repo.sh` (provided at both `demo/` and `demo/sample-pipeline-app/`):
     - Takes a target directory (defaults to `/tmp/gdi-sample-pipeline-app`).
     - Copies clean source files (excluding caches and temporary backups).
     - Initializes a standalone git repository with an initial pristine commit on branch `main`.
     - Displays instructions for pushing to `ssvaidyanathan/gdi-sample-pipeline-app` for Phase 5 live verification.
5. **Automated Unit & Integration Tests (`demo/tests/`)**:
   - `demo/tests/conftest.py`: Isolated module loader fixture avoiding package namespace collisions with `bridge.app` or `agent.app`.
   - `demo/tests/test_demo_scenarios.py`:
     - `test_pristine_demo_app_functionality`: Verifies all REST endpoints of the demo microservice.
     - `test_scenario_1_lint_failure_lifecycle`: Tests lint failure injection, ruff failure verification, `PipelineLogsSpecialist` diagnosis, and clean reset.
     - `test_scenario_2_test_failure_lifecycle`: Tests test failure injection, pytest failure verification, `PipelineLogsSpecialist` diagnosis, and clean reset.
     - `test_scenario_3_build_failure_lifecycle`: Tests build failure injection, Dockerfile modification verification, `PipelineLogsSpecialist` diagnosis, and clean reset.
     - `test_scenario_4_deploy_failure_lifecycle`: Tests deploy failure injection, simulation exit 137, `RuntimeHealthSpecialist` OOM diagnosis, and clean reset.
     - `test_stage_classification_for_demo_pipeline_jobs`: Verifies accurate stage classification for all 4 demo workflow jobs.
     - `test_seed_demo_repo_script`: Tests standalone repository seeding, git initialization, commit creation, and artifact hygiene.

### 3. Key Invariants & Architectural Patterns
- **Contract Byte-Identity**: Verified by `scripts/check-contracts.sh` (0 differences).
- **Strict Read-Only Guarantee**: All agent tools remain `@readonly_tool`.
- **AST Root Cause Guard**: Zero post-LLM template overrides verified by `check-no-rootcause-overrides.py`.
- **100% Offline Hermetic Execution**: Zero external network requests during test execution.

### 4. Verification & Test Results
- **Contract Synchronization Check**:
  - Command: `bash gha-deploy-investigator/scripts/check-contracts.sh`
  - Result: `SUCCESS: Contracts are byte-identical.`
- **AST Root Cause Guard**:
  - Command: `python3 incident-sre-agent/scripts/check-no-rootcause-overrides.py gha-deploy-investigator/agent/app/synthesizer.py gha-deploy-investigator/agent/app/dispatcher.py`
  - Result: `[PASS] Zero post-LLM root cause template overrides or regex classifiers found.`
- **Automated Pytest Suite**:
  - Command: `PYTHONPATH=gha-deploy-investigator/agent:gha-deploy-investigator/bridge:gha-deploy-investigator/demo/sample-pipeline-app /scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest gha-deploy-investigator/agent/tests gha-deploy-investigator/bridge/tests gha-deploy-investigator/demo/tests -v`
  - Result: `49 passed in 2.91s` (23 agent tests, 19 bridge tests, 7 demo tests).
- **Demo Microservice Pytest Suite**:
  - Command: `PYTHONPATH=. pytest tests/ -v` (inside `demo/sample-pipeline-app`)
  - Result: `5 passed in 0.04s`.
- **Ruff Linter**:
  - Command: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator`


## 2026-10-16 - Phase 3 Architectural Refinement: Fault Tooling Placement & Realistic Scenarios
- **Author**: `gha-dev-1`
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview & Objectives
Incorporated architectural refinements for Phase 3 demo tooling and scenarios requested by EM:
1. **Fault Tooling Placement (No Leaked Answers in Seeded Repo)**: Relocated all scenario scripts from `sample-pipeline-app/scenarios/` to `demo/scenarios/` (strictly outside the customer-style demo app directory) and removed `demo/sample-pipeline-app/seed-demo-repo.sh` to prevent evaluation overfitting and maintain a pristine customer repository.
2. **Realistic, Production-Grade Fault Names (No `DEMO_*` identifiers)**: Replaced all synthetic demo identifiers (`DEMO_ALLOCATE_OOM`, `DEMO_CRASH_ON_STARTUP`) with realistic domain configuration: `cache_prewarm_buffer_mb` / `CACHE_PREWARM_BUFFER_MB=2048` (exceeding container memory limits, inducing exit code 137).
3. **Clean File Operations & `.gitignore`**: Ensured scripts do not leave `.bak` or `.orig` artifacts around. Added `*.bak` and `*.orig` to both root and demo `.gitignore`.
4. **Scenario Workflow with Git Commit Support**: Added target directory parameter and optional `--commit` flag to all scenario triggers and `reset-scenarios.sh` producing realistic conventional commits and clean git revert restoration. Documented workflow in `demo/SCENARIOS.md` and updated `demo/sample-pipeline-app/README.md`.

### 2. Deliverables Implemented & Updated
1. `demo/scenarios/`:
   - `trigger-lint-failure.sh`: Injects unused imports (`F401`) and multiple statements on single line (`E701`) into `app/main.py`. Supports `--commit` with message `refactor(orders): add order export and audit formatting`.
   - `trigger-test-failure.sh`: Injects broken business logic assertion into `tests/test_main.py`. Supports `--commit` with message `feat(pricing): update order discount tier calculation`.
   - `trigger-build-failure.sh`: Injects missing asset COPY instruction into `Dockerfile`. Supports `--commit` with message `chore(deps): configure cache warmup asset packaging`.
   - `trigger-deploy-failure.sh`: Injects `CACHE_PREWARM_BUFFER_MB=2048` and memory limit `256Mi` into `scripts/deploy.sh`. Supports `--commit` with message `feat(cache): pre-warm order history cache on startup`.
   - `reset-scenarios.sh`: Idempotently reverts git commits (when `--commit` passed) and restores files to clean baseline state without leaving `.bak` files.
2. `demo/sample-pipeline-app/`:
   - Removed `scenarios/` and `seed-demo-repo.sh` from the application directory.
   - Added clean `.gitignore` (ignoring `.pytest_cache`, `.ruff_cache`, `*.bak`, `*.orig`, etc.).
   - Updated `app/config.py`: Replaced `crash_on_startup` and `allocate_oom` with `cache_prewarm_buffer_mb` and `strict_schema_validation`.
   - Updated `app/main.py`: Lifespan prewarms cache buffer and simulates cgroups OOM killer when allocation exceeds container threshold (`> 512MB`).
   - Updated `scripts/deploy.sh`: Inspects `CACHE_PREWARM_BUFFER_MB` and simulates Cloud Run container termination (exit code 137) when exceeding limits.
   - Updated `README.md`: Customer-facing documentation referencing external `demo/SCENARIOS.md`.
3. `demo/seed-demo-repo.sh`:
   - Excludes caches, backups, and ensures only `sample-pipeline-app/` is seeded (no scenario triggers or answers leaked).
4. `demo/SCENARIOS.md`:
   - Comprehensive catalog and runbook for triggering, testing, and resetting scenarios.
5. `demo/tests/test_demo_scenarios.py`:
   - Updated to execute scenarios from `demo/scenarios/`.
   - Added `test_scenario_commit_and_revert_workflow` verifying `--commit` and `reset-scenarios.sh --commit`.
   - Updated `test_seed_demo_repo_script` verifying `scenarios/` and `seed-demo-repo.sh` are NOT leaked into seeded repo.

### 3. Verification & Test Results
- **Contracts Synchronization**:
  - `bash gha-deploy-investigator/scripts/check-contracts.sh` -> `SUCCESS: Contracts are byte-identical.`
- **AST Root Cause Guard**:
  - `python3 incident-sre-agent/scripts/check-no-rootcause-overrides.py ...` -> `[PASS] Zero post-LLM root cause template overrides found.`
- **Zero Forbidden Identifiers**:
  - `grep -rn "DEMO_" gha-deploy-investigator/demo/sample-pipeline-app/ gha-deploy-investigator/agent/ gha-deploy-investigator/bridge/` -> `0 occurrences.`
- **Full Offline Pytest Suite**:
  - `PYTHONPATH=gha-deploy-investigator/agent:gha-deploy-investigator/bridge:gha-deploy-investigator/demo/sample-pipeline-app pytest ...` -> `50 passed in 3.13s` (100% offline).
- **Internal Demo App Pytest Suite**:
  - `pytest tests -v` (inside `demo/sample-pipeline-app`) -> `5 passed in 0.04s`.
- **Ruff Linter**:
  - `ruff check gha-deploy-investigator` -> `All checks passed!`


## 2026-10-16 - User Requirement: Zero Leaked Hints in Demo Repo & Zero Hardcoded Agent Answers
- **Author**: `gha-dev-1`
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Requirements Addressed
Per direct requirement from human user via `gha-em`:
- Complete elimination of any reference to failure injection, scenarios, testing hints, or investigator agent from `sample-pipeline-app/`.
- Ensure `sample-pipeline-app/` looks 100% like a genuine customer production service.
- Verification that zero hardcoded scenario answers or customer app strings exist in the agent repository.

### 2. Actions & Refinements
1. **Pristine Customer Production Service (`sample-pipeline-app/`)**:
   - `README.md`: Completely rewritten as a realistic customer service document describing the Order Processing Service, API endpoints, setup, and running instructions with zero mentions of demo, injection, scenarios, or agent evaluation.
   - `app/main.py`: Updated FastAPI service metadata (`title="Order Processing Service"`, `description="High-throughput asynchronous customer order processing service"`), removed demo comments.
   - `pyproject.toml`: Updated package description and authorship to genuine production metadata.
   - `scripts/deploy.sh`: Removed internal simulation logic and `CACHE_PREWARM_BUFFER_MB` error branching entirely; now a 100% genuine customer production Cloud Run deployment script.
2. **Scenario Injections (`demo/scenarios/`)**:
   - `trigger-lint-failure.sh`: Injects genuine currency formatting helper with style and unused import violations; zero `# INJECTED` comments.
   - `trigger-test-failure.sh`: Injects realistic `test_customer_tier_discount_calculation` with calculation regression; zero hints or `_failure` function names.
   - `trigger-build-failure.sh`: Injects realistic configuration asset packaging COPY line.
   - `trigger-deploy-failure.sh`: Realistic memory constraint and cache buffer configuration.
   - `reset-scenarios.sh`: Idempotent restoration and commit revert.
3. **Agent Repo Cleanliness**:
   - Verified with regex/grep sweeps: zero hardcoded code, zero hardcoded scenario answers, and zero references to `sample-pipeline-app` in `agent/` and `bridge/`.

### 3. Verification
- Contracts check: `SUCCESS: Contracts are byte-identical.`
- AST static guard: `Zero post-LLM root cause template overrides found.`
- Pytest suite: `50 passed in 3.25s` (offline).


## 2026-10-17 - Phase 3 Round 1 Review Findings Resolution
- **Author**: `gha-dev-1`
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Reviewer**: `gha-rev-5`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Findings Resolved
Resolved all 5 review findings from `phase3-round1-review.md`:
1. **Finding 1 (Blocking) - Reset Leading Blank Lines in `app/main.py`**:
   - Stripped the 14 accumulated leading blank lines from `demo/sample-pipeline-app/app/main.py`.
   - In `demo/scenarios/reset-scenarios.sh`, updated the python cleanup block to write `f.write(''.join(clean).lstrip())` to prevent future blank line accumulation.
2. **Finding 2 (Blocking) - Integration Tests Mutating Tracked Files**:
   - Added `isolated_demo_app` fixture in `demo/tests/conftest.py` using `tmp_path` to copy `sample-pipeline-app` to an isolated sandbox.
   - Added `scenarios_dir` fixture in `demo/tests/conftest.py`.
   - Updated all 4 lifecycle tests in `demo/tests/test_demo_scenarios.py` to run against `isolated_demo_app` instead of tracked files.
   - Verified running `pytest` leaves the active git work tree completely clean (`git status` shows 0 modified files).
3. **Finding 3 (Non-Blocking) - Git Worktree Robustness in `reset-scenarios.sh`**:
   - Replaced `[[ -d "${APP_DIR}/.git" ]]` with `git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1` to support worktrees where `.git` is a file.
4. **Finding 4 (Non-Blocking) - Stale Simulation Echo in `trigger-deploy-failure.sh`**:
   - Updated echo message in `trigger-deploy-failure.sh` from `CI_SIMULATION=true ./scripts/deploy.sh` to `./scripts/deploy.sh`.
5. **Finding 5 (Non-Blocking) - Simplified `git revert` Command**:
   - Simplified git revert in `reset-scenarios.sh` to `git -C "${APP_DIR}" revert HEAD --no-edit 2>/dev/null || true`.

### 2. Verification
- Contracts check: `scripts/check-contracts.sh` -> byte-identical.
- AST static guard: `check-no-rootcause-overrides.py` -> 0 violations.
- Ruff linter: `ruff check gha-deploy-investigator` -> All checks passed.
- Full pytest suite: `50 passed in 3.24s` (offline).
- Git work tree clean invariant: `git status` clean after test execution.








## 2026-10-17 - Phase 4: Guardrails, Infrastructure, Documentation & Packaging
- **Author**: `gha-dev-1` (Developer Agent)
- **Role**: Developer
- **Supervisor**: `gha-em`
- **Branch**: `feat/gha-deploy-investigator`
- **Status**: Completed

### 1. Overview & Objectives
Implemented Phase 4: Guardrails, Infrastructure, Documentation & Packaging per `design.md` Section 6 and Amendments 5, 6, and 7:
1. **CI Guardrail Scripts**: Ported and adapted all required guardrails in `gha-deploy-investigator/scripts/`:
   - `scripts/check-contracts.sh`: Enforces byte-identical synchronization between `bridge/app/contracts/` and `agent/app/contracts/`.
   - `scripts/check-no-literal-projects.sh`: Enforces zero forbidden environment-specific identifiers (via `FORBIDDEN_LITERALS`), zero un-externalized Gemini model literals in code, valid CI workflow badge URLs, and invokes markdown link verification.
   - `scripts/check-markdown-links.py`: Verifies all relative links across repository markdown files resolve to existing files.
   - `scripts/check-no-rootcause-overrides.py`: Enforces AST static checks on `synthesizer.py` and `dispatcher.py` to prevent post-LLM root cause template overrides or regex classifiers outside labeled automated fallback paths.
   - `scripts/check-no-demo-overfit.sh`: Enforces that no scenario numbers, demo identifiers, or canned answers exist in `agent/app/`.
   - `scripts/copy-secrets.sh`: Helper script to replicate Secret Manager secrets at deploy time from existing `incident-sre-agent` secrets to `gdi-` secrets without committing secret values or echoing them to stdout.
2. **Dedicated Terraform Stacks**:
   - `bridge/infra/terraform/`: Self-contained, parameterized Terraform manifests (`main.tf`, `variables.tf`, `outputs.tf`, `terraform.tfvars.example`) defining `gdi-bridge` Cloud Run service, `gdi-tasks-queue` Cloud Tasks queue, Secret Manager secrets (`gdi-github-webhook-secret`, `gdi-servicenow-password`, `gdi-servicenow-webhook-token`), and IAM bindings. Parameterized with `variable "project_id"`, `variable "region"`, `variable "environment"`. Zero hardcoded project IDs or secrets!
   - `agent/infra/terraform/`: Parameterized Terraform manifests defining `gdi-agent` Cloud Run service with internal ingress (`INGRESS_TRAFFIC_INTERNAL_ONLY`), service accounts (`gdi-agent-sa`, `gdi-tasks-invoker-sa`), IAM roles (`roles/aiplatform.user`, `roles/logging.viewer`, `roles/run.viewer`, `roles/run.invoker`), and Secret Manager secret (`gdi-github-app-private-key`).
3. **Comprehensive Documentation**:
   - `gha-deploy-investigator/README.md`: Project overview, capabilities, architecture diagram, local testing, scenario catalog, configuration, deployment, and documentation index.
   - `gha-deploy-investigator/docs/ARCHITECTURE.md`: Full system architecture detailing inbound webhook verification, canonical normalization, Cloud Tasks dispatch, multi-stage diagnostic engine, reverse ServiceNow incident flow, and asynchronous GitHub Check Run / PR write-back.
   - `gha-deploy-investigator/docs/CONFIGURATION.md`: Complete environment variable reference, personal GitHub App setup guide and App Manifest YAML (for user-owned personal GitHub App for `ssvaidyanathan` per Amendment 7), and ServiceNow integration settings.
   - `gha-deploy-investigator/docs/DEPLOY.md`: Step-by-step deployment guide using Terraform, Secret Manager, and `copy-secrets.sh`.
   - `gha-deploy-investigator/demo/SCENARIOS.md`: Updated runbook detailing all 4 stages, external fault triggers, commit & revert workflow, and seeding to standalone repo `ssvaidyanathan/gdi-sample-pipeline-app`.
   - Root `README.md`: Updated to list `gha-deploy-investigator` alongside `incident-sre-agent`.
   - `.github/workflows/gha-deploy-investigator-ci.yml`: Added GitHub Actions CI workflow covering contracts check, no-literals check, de-overfit guards, ruff linter, unit & component tests, and Terraform formatting & validation.

### 2. Verification Gates
- **Contracts Sync Check**: `bash scripts/check-contracts.sh` -> SUCCESS: Contracts are byte-identical.
- **De-overfit Guard Check**: `bash scripts/check-no-demo-overfit.sh` -> SUCCESS: De-overfit guard check passed cleanly.
- **Root Cause AST Check**: `python3 scripts/check-no-rootcause-overrides.py` -> SUCCESS: Root cause guard check passed cleanly.
- **No Literal Projects & Link Check**: `bash scripts/check-no-literal-projects.sh` -> SUCCESS: All literal, repository URL, and link checks passed cleanly (verified 11 markdown links, zero forbidden literals).
- **Offline Hermetic Test Suite**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/pytest` -> 50 passed in 3.08s (100% offline).
- **Ruff Linter**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/.venv/bin/ruff check gha-deploy-investigator` -> All checks passed!
- **Working Tree Cleanliness**: Verified `git status` clean after test suite runs.
