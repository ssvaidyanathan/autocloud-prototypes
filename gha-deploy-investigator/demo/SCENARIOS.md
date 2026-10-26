# Multi-Stage CI/CD Failure Scenarios

This directory contains deterministic failure injection and reset tooling for testing the autonomous `gha-deploy-investigator` agent across all four stages of a production GitHub Actions pipeline.

## Architectural Design & Invariants

1. **Tooling Placement (Outside Seeded App)**:
   - All scenario triggers and reset scripts reside in `demo/scenarios/`, strictly **outside** `demo/sample-pipeline-app/`.
   - The seeded customer-style repo `sample-pipeline-app/` remains a pristine, realistic microservice without any embedded fault injection scripts or demo triggers. This guarantees the autonomous agent does not inspect leaked trigger scripts or commit messages that could cause evaluation overfitting.

2. **Realistic Production Defect Markers**:
   - No artificial `DEMO_*` identifiers or blatant synthetic keys.
   - Faults use realistic domain configurations and code regressions (e.g. `CACHE_PREWARM_BUFFER_MB=2048`, unused imports, discount calculation regressions, missing container packaging assets).

3. **Clean File Operations**:
   - Trigger scripts perform direct file transformations without leaving `.bak` or `.orig` artifacts.
   - `reset-scenarios.sh` cleans any leftover artifacts and verifies `.gitignore` compliance.

4. **Standalone & Git Commit Workflow**:
   - Each script accepts an optional target directory argument (defaulting to `demo/sample-pipeline-app`).
   - Each script supports an optional `--commit` flag that generates a realistic commit with conventional semantic commit messages.

---

## Scenario Catalog

### Scenario 1: Lint & Code Style Violation
- **Stage**: `lint`
- **Trigger**: `demo/scenarios/trigger-lint-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Unused imports (`F401`) and multiple statements on a single line (`E702`) in `app/main.py`. The helper is prepended above the existing imports, so Ruff also reports them as `E402`.
- **Commit Message**: `refactor(orders): add order export and currency formatting helper`
- **Verification**: `ruff check app/`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `lint`, root cause pointing to `app/main.py` violations, structured annotations generated.
- **Expected Auto-Fix** (with `AUTOFIX_ENABLED=true`): `deterministic_linter` PR touching only `app/main.py`; fix PR CI green; verification `PASSED`.

### Scenario 2: Broken Unit Test Assertion
- **Stage**: `test`
- **Trigger**: `demo/scenarios/trigger-test-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Regression in promotional discount calculation in `tests/test_main.py` (`test_customer_tier_discount_calculation`).
- **Commit Message**: `feat(pricing): update order discount tier calculation`
- **Verification**: `pytest tests/ -v`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `test`, root cause identifies failing assertion function and expected vs actual values.
- **Expected Auto-Fix** (with `AUTOFIX_ENABLED=true`): `llm_patch` PR editing only `tests/test_main.py` so the asserted values match; fix PR CI green; verification `PASSED`. A reviewer should reject a fix that edits test expectations.

### Scenario 3: Container Build Asset Packaging Failure
- **Stage**: `build`
- **Trigger**: `demo/scenarios/trigger-build-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Missing configuration asset `COPY config/cache-warmup.json /app/config/cache-warmup.json` in `Dockerfile`.
- **Commit Message**: `chore(deps): configure cache warmup asset packaging`
- **Verification**: `docker build -t sample-pipeline-app:test .`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `build`, root cause highlights missing build context file `config/cache-warmup.json`.
- **Expected Auto-Fix** (with `AUTOFIX_ENABLED=true`): `llm_patch` PR touching `Dockerfile` and/or `config/`; fix PR CI green; verification `PASSED`. If no safe fix is found, no PR is opened and the incident gets an `[Auto-Fix Hand-off]` work note instead.

### Scenario 4: Runtime OOM / Deploy Container Crash
- **Stage**: `deploy`
- **Trigger**: `demo/scenarios/trigger-deploy-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Sets `EXTRA_ENV_VARS="CACHE_PREWARM_BUFFER_MB=2048"` and limits container memory to `256Mi` in `scripts/deploy.sh`.
- **Commit Message**: `feat(cache): pre-warm order history cache on startup`
- **Verification**: `./scripts/deploy.sh` (Cloud Run container startup OOM exit 137)
- **Diagnosed By**: `RuntimeHealthSpecialist`
- **Expected Outcome**: Stage identified as `deploy`, root cause diagnoses exit code 137 (OOM), recommends remediation action `--memory 1Gi`.
- **Expected Auto-Fix** (with `AUTOFIX_ENABLED=true`): `config_tuning` PR changing one line in `scripts/deploy.sh` (`MEMORY` 256Mi → 512Mi); lint, tests and build pass but `Deploy to Cloud Run` is skipped on the fix branch, so verification is `INCONCLUSIVE` (requires human validation), not `PASSED`.

In every scenario the bridge makes exactly 1 fix attempt, records the fix PR link in the ServiceNow work notes, and leaves the incident In Progress for human review. The fix PR's own CI run never creates a new incident or a second PR.

### Declarative Environment Behavior (`--clear-env-vars`)
In Google Cloud Run, `gcloud run deploy` commands preserve previously configured environment variables across service revisions unless explicitly cleared or overwritten. If an injection script or prior release configured temporary environment variables (such as `EXTRA_ENV_VARS="CACHE_PREWARM_BUFFER_MB=2048"` in Scenario 4), a subsequent deployment or revert commit that omits `EXTRA_ENV_VARS` would otherwise retain the stale variable in the newly created revision.

To enforce strictly declarative deployments:
- `scripts/deploy.sh` checks if `EXTRA_ENV_VARS` is populated:
  - When non-empty: passes `--set-env-vars "${EXTRA_ENV_VARS}"`.
  - When empty: passes `--clear-env-vars` to clear any prior revision overrides.
- This ensures the deployed Cloud Run service environment strictly mirrors the repository state, preventing leaked or persisted environment variables during rollbacks and resets.

---

## Reset Workflow

For live runs with auto-fix enabled, reset by closing the fix PR with its branch deleted, then
`git revert` the scenario commit on `main`, push, and wait for `main` to go green. Do **not** merge
fix PRs: a merged fix stays on `main` and the scenario no longer reproduces. See
[DEMO-RUNBOOK.md, Section 4](DEMO-RUNBOOK.md#4-reset--cleanup-procedure).

To restore a target working tree to a pristine baseline state with the reset script:

```bash
# Basic reset on working tree
./demo/scenarios/reset-scenarios.sh [TARGET_DIR]

# Reset with git revert when scenarios were committed with --commit
./demo/scenarios/reset-scenarios.sh [TARGET_DIR] --commit
```

The reset script idempotently:
1. Reverts any scenario commits if in a standalone git repository.
2. Removes all injected defect lines from `app/main.py`, `tests/test_main.py`, `Dockerfile`, and `scripts/deploy.sh`.
3. Cleans any `.bak` or `.orig` temporary files.

---

## Seeding Standalone Repository: `ssvaidyanathan/gdi-sample-pipeline-app`

To seed the standalone customer demo repository (`ssvaidyanathan/gdi-sample-pipeline-app`) for live end-to-end evaluation:

```bash
# 1. Seed demo app to temporary directory or dedicated worktree
./demo/seed-demo-repo.sh /tmp/gdi-sample-pipeline-app

# 2. Push to GitHub remote (conducted by deployment administrator)
cd /tmp/gdi-sample-pipeline-app
git remote add origin https://github.com/ssvaidyanathan/gdi-sample-pipeline-app.git
git branch -M main
# git push -u origin main
```

The seeded repository contains exclusively the customer-facing microservice (`sample-pipeline-app/`) with its complete multi-stage CI/CD pipeline (`.github/workflows/pipeline.yml`), Docker packaging, tests, and deployment scripts, with zero fault injection tooling or leaked evaluation answers.

