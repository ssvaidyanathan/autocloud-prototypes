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
- **Injected Defect**: Unused imports (`F401`) and multiple statements on a single line (`E701`) in `app/main.py`.
- **Commit Message**: `refactor(orders): add order export and audit formatting`
- **Verification**: `ruff check app/`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `lint`, root cause pointing to `app/main.py` violations, structured annotations generated.

### Scenario 2: Broken Unit Test Assertion
- **Stage**: `test`
- **Trigger**: `demo/scenarios/trigger-test-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Regression in promotional discount calculation in `tests/test_main.py` (`test_order_discount_calculation_failure`).
- **Commit Message**: `feat(pricing): update order discount tier calculation`
- **Verification**: `pytest tests/ -v`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `test`, root cause identifies failing assertion function and expected vs actual values.

### Scenario 3: Container Build Asset Packaging Failure
- **Stage**: `build`
- **Trigger**: `demo/scenarios/trigger-build-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Missing configuration asset `COPY config/cache-warmup.json /app/config/cache-warmup.json` in `Dockerfile`.
- **Commit Message**: `chore(deps): configure cache warmup asset packaging`
- **Verification**: `docker build -t sample-pipeline-app:test .`
- **Diagnosed By**: `PipelineLogsSpecialist`
- **Expected Outcome**: Stage identified as `build`, root cause highlights missing build context file `config/cache-warmup.json`.

### Scenario 4: Runtime OOM / Deploy Container Crash
- **Stage**: `deploy`
- **Trigger**: `demo/scenarios/trigger-deploy-failure.sh [TARGET_DIR] [--commit]`
- **Injected Defect**: Sets `EXTRA_ENV_VARS="CACHE_PREWARM_BUFFER_MB=2048"` and limits container memory to `256Mi` in `scripts/deploy.sh`.
- **Commit Message**: `feat(cache): pre-warm order history cache on startup`
- **Verification**: `CI_SIMULATION=true ./scripts/deploy.sh`
- **Diagnosed By**: `RuntimeHealthSpecialist`
- **Expected Outcome**: Stage identified as `deploy`, root cause diagnoses exit code 137 (OOM), recommends remediation action `--memory 1Gi`.

---

## Reset Workflow

To restore the target repository to a pristine baseline state:

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

## Seeding a Standalone Customer Repository

To create a clean, standalone Git repository suitable for publishing to GitHub for live end-to-end evaluation:

```bash
./demo/seed-demo-repo.sh /tmp/gdi-sample-pipeline-app
```

The seeded directory contains exclusively the customer-facing microservice (`sample-pipeline-app/`) with zero scenario triggers or leaked evaluation answers.
