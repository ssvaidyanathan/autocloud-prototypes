# Interactive Demo Runbook: Multi-Stage CI/CD Failure Scenarios

This runbook provides step-by-step instructions for executing and observing the autonomous GitHub Actions Deploy Investigator across all four stages of a modern CI/CD pipeline: **lint**, **test**, **build**, and **deploy**.

---

## 1. Overview & Architecture

When a GitHub Actions workflow run fails, the autonomous diagnostic bridge and investigator agent automatically:
1. **Detect Failure**: Ingest inbound `workflow_run.completed` (status: `failure`) webhooks from GitHub.
2. **Open Incident**: Automatically create a ServiceNow incident in state **New (1)**.
3. **Dispatch Investigation**: Classify the failure stage (`lint`, `test`, `build`, `deploy`), assign specialized investigation tools, and transition the ServiceNow incident to **In Progress (2)**.
4. **Execute Grounded Diagnosis**: Read runner logs, correlate git diffs, inspect container/telemetry logs, segregate unrelated warnings, and formulate grounded causal remediation.
5. **Dual Write-Back**:
   - **ServiceNow**: Post structured investigation work notes and attach a standalone HTML investigation report.
   - **GitHub**: Post a neutral Check Run linking to the ServiceNow incident and a rich, collapsible PR/commit comment with root cause, decisive evidence quotes, causal remediation, and segregated unrelated observations.

```
+-------------------+      +-------------------+      +---------------------+
|  GitHub Actions   | ---> | Investigation     | ---> | Autonomous Agent    |
|  Pipeline Failure |      | Bridge (FastAPI)  |      | Specialist Tools    |
+-------------------+      +-------------------+      +---------------------+
                                     |                           |
                                     v                           v
                           +-------------------+      +---------------------+
                           | ServiceNow Ticket | <--- | Dual Write-Back:    |
                           | State: In Progress|      | Check Run & Comment |
                           +-------------------+      +---------------------+
```

---

## 2. Prerequisites & Setup

### A. GitHub Authentication & Access
- Ensure you have write/admin access to the sample pipeline repository: `https://github.com/ssvaidyanathan/gdi-sample-pipeline-app`.
- Ensure GitHub CLI (`gh`) is installed and authenticated:
  ```bash
  gh auth status
  # If not authenticated, run:
  gh auth login
  ```

### B. Clone Both Repositories Side-by-Side
The defect injection scripts are maintained in the prototype repository (`autocloud-prototypes`) to prevent leaking scenario hints or test harnesses into the target pipeline codebase.

Create a dedicated workspace directory and clone both repositories side by side:

```bash
mkdir -p ~/demo-workspace && cd ~/demo-workspace

# 1. Clone the prototype repository (contains scenario injection scripts)
git clone https://github.com/ssvaidyanathan/autocloud-prototypes.git
cd autocloud-prototypes
git checkout feat/gha-deploy-investigator   # PR branch until merged into main
cd ..

# 2. Clone the target demo pipeline repository
git clone https://github.com/ssvaidyanathan/gdi-sample-pipeline-app.git
cd gdi-sample-pipeline-app
```

Your directory structure should resemble:
```
demo-workspace/
├── autocloud-prototypes/
│   └── gha-deploy-investigator/
│       └── demo/
│           └── scenarios/
│               ├── trigger-lint-failure.sh
│               ├── trigger-test-failure.sh
│               ├── trigger-build-failure.sh
│               ├── trigger-deploy-failure.sh
│               └── reset-scenarios.sh
└── gdi-sample-pipeline-app/
    ├── app/
    │   ├── __init__.py
    │   ├── config.py
    │   └── main.py
    ├── tests/
    │   └── test_main.py
    ├── Dockerfile
    └── scripts/
        └── deploy.sh
```

### C. Services & Monitoring Links
- **Investigation Services**: Live `gdi-bridge` and `gdi-agent` services deployed in Cloud Run receive webhooks automatically upon workflow completion.
- **ServiceNow Console**: Access incidents at `https://<your-instance>.service-now.com` (Filter: `correlation_display=gha-deploy-investigator`).
- **GitHub Actions Console**: Track workflow runs at `https://github.com/ssvaidyanathan/gdi-sample-pipeline-app/actions`.

---

## 3. Scenario Execution Walkthrough

Each scenario follows a standardized operational cycle:
1. `cd gdi-sample-pipeline-app`
2. `git pull origin main` to ensure a pristine local branch.
3. Inject defect and commit: `../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-<scenario>-failure.sh . --commit && git push origin main`
4. Observe the pipeline failure and autonomous investigation on ServiceNow and GitHub.
5. Reset: `git revert HEAD --no-edit && git push origin main`
6. Confirm the pipeline completes with all jobs green before moving to the next scenario.

---

### Scenario 1: Lint & Code Style Violation

#### Injected Defect
Introduces an unused import (`F401`) and multiple statements on a single line (`E702`) into `app/main.py`.

#### Step 1: Inject & Push
From within `gdi-sample-pipeline-app`:
```bash
cd gdi-sample-pipeline-app
git pull origin main

# Inject lint violation, commit, and push
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-lint-failure.sh . --commit && git push origin main
```

#### Step 2: Pipeline Stage Failure & Timing
- **Failing Job/Step**: `lint` job -> `Run Ruff Lint` step.
- **Expected Duration**: Workflow fails in approximately 15–30 seconds.
- **Log Snippet**:
  ```
  app/main.py:1:8: F401 [*] `math` imported but never used
  app/main.py:5:35: E702 Multiple statements on one line (semicolon)
  Found 2 errors.
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Automatically created in state `New (1)` and updated to `In Progress (2)`.
- **Short Description**: `[CI/CD Failure] ssvaidyanathan/gdi-sample-pipeline-app (lint)`
- **Work Notes**: HTML summary detailing violations with file:line coordinates.
- **Attachment**: Downloadable HTML investigation report `ci_cd_investigation_report_INC00XXXXX.html`.

#### Step 4: Expected GitHub Write-Back
- **Check Run**: `Autocloud / ServiceNow Incident Link` (conclusion: `neutral`) linking to ServiceNow ticket.
- **Annotations**: Inline check run annotations on `app/main.py` lines 1 and 5.
- **Commit Comment**: Collapsible breakdown with root cause, log evidence, and suggested fix.

#### Step 5: What a Good Diagnosis Looks Like
- **Root Cause**: Explicitly cites the 2 linter violations (`F401` unused import `math` and `E702` multiple statements) in `app/main.py`.
- **Evidence Quote**: Exact runner log excerpt citing file paths, line numbers, and error codes.
- **Causal Remediation**:
  - Remove unused import `math` from `app/main.py`.
  - Split multi-statement line into separate statements or remove trailing semicolon.
- **Unrelated Observations**: Empty or notes any non-fatal pre-existing tooling warnings.

#### Step 6: Reset
```bash
cd gdi-sample-pipeline-app
git revert HEAD --no-edit && git push origin main
```
*Wait for GitHub Actions to complete green across all stages before proceeding.*

---

### Scenario 2: Broken Unit Test Assertion

#### Injected Defect
Appends a failing test `test_customer_tier_discount_calculation` in `tests/test_main.py` where an expected total does not match the actual calculation. **No application source code is modified in the git diff.**

#### Step 1: Inject & Push
From within `gdi-sample-pipeline-app`:
```bash
cd gdi-sample-pipeline-app
git pull origin main

# Inject test assertion defect, commit, and push
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-test-failure.sh . --commit && git push origin main
```

#### Step 2: Pipeline Stage Failure & Timing
- **Failing Job/Step**: `test` job -> `Run Pytest` step.
- **Expected Duration**: Workflow fails in approximately 20–40 seconds.
- **Log Snippet**:
  ```
  FAILED tests/test_main.py::test_customer_tier_discount_calculation - AssertionError: Order discount tier mismatch: expected $100.00, got $75.50
  1 failed, 7 passed in 0.85s
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Transitioned to `In Progress (2)`.
- **Work Notes**: Cites the failing test assertion and diff context confirming modifications are isolated to `tests/test_main.py`.
- **Description**: Contains separate `Recommended Remediation:` and `Unrelated Observations:` sections.

#### Step 4: Expected GitHub Write-Back
- **Check Run**: Completed with incident reference and confidence score.
- **Commit Comment**: Contains:
  - `<summary><strong>🔍 Probable Root Cause</strong></summary>`
  - `<summary><strong>🛠️ Recommended Remediation</strong></summary>` (targeting the test file)
  - `<summary><strong>⚠️ Unrelated Observations</strong></summary>` (segregated runtime warnings)

#### Step 5: What a Good Diagnosis Looks Like
- **Root Cause**: Identifies that `test_customer_tier_discount_calculation` in `tests/test_main.py` failed assertion comparing expected `$100.00` against `$75.50`.
- **Diff-Grounded Remediation Rule**: Because the commit diff touches **only test files**, the causal remediation strictly targets correcting the test logic or assertion, **never** recommending changes to application business logic.
- **Warning Segregation**: Non-fatal runtime warnings (e.g., deprecation warnings from test frameworks) appear strictly under **Unrelated Observations**, never in causal remediation.

#### Step 6: Reset
```bash
cd gdi-sample-pipeline-app
git revert HEAD --no-edit && git push origin main
```
*Wait for GitHub Actions to complete green across all stages before proceeding.*

---

### Scenario 3: Container Build Asset Packaging Failure

#### Injected Defect
Appends an invalid `COPY config/cache-warmup.json /app/config/cache-warmup.json` instruction to `Dockerfile` referencing a non-existent configuration file.

#### Step 1: Inject & Push
From within `gdi-sample-pipeline-app`:
```bash
cd gdi-sample-pipeline-app
git pull origin main

# Inject Dockerfile defect, commit, and push
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-build-failure.sh . --commit && git push origin main
```

#### Step 2: Pipeline Stage Failure & Timing
- **Failing Job/Step**: `build` job -> `Build and push container image` step.
- **Expected Duration**: Workflow fails in approximately 45–90 seconds.
- **Log Snippet**:
  ```
  ERROR: failed to solve: failed to compute cache key: failed to calculate checksum of ref ... "/config/cache-warmup.json": not found
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Transitioned to `In Progress (2)`.
- **Work Notes**: Diagnoses missing build asset in context.
- **Attachment**: Updated standalone investigation report.

#### Step 4: Expected GitHub Write-Back
- **Check Run**: Points to ServiceNow incident with high diagnostic confidence.
- **Commit Comment**: Clear build context breakdown highlighting the missing asset.

#### Step 5: What a Good Diagnosis Looks Like
- **Root Cause**: Highlights missing asset `config/cache-warmup.json` referenced by `COPY` command in `Dockerfile`.
- **Evidence Quote**: Direct quotes of Docker/buildx error message indicating the missing file path.
- **Causal Remediation**:
  - Add the missing `config/cache-warmup.json` asset to the repository build context, OR
  - Remove the invalid `COPY` instruction from `Dockerfile`.

#### Step 6: Reset
```bash
cd gdi-sample-pipeline-app
git revert HEAD --no-edit && git push origin main
```
*Wait for GitHub Actions to complete green across all stages before proceeding.*

---

### Scenario 4: Runtime OOM / Deploy Container Crash

#### Injected Defect
Alters `scripts/deploy.sh` to lower container memory to `256Mi` while configuring `EXTRA_ENV_VARS="CACHE_PREWARM_BUFFER_MB=2048"`. During startup, the application attempts to allocate memory exceeding the limit, triggering exit code 137 (OOM).

#### Step 1: Inject & Push
From within `gdi-sample-pipeline-app`:
```bash
cd gdi-sample-pipeline-app
git pull origin main

# Inject deploy OOM regression, commit, and push
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-deploy-failure.sh . --commit && git push origin main
```

#### Step 2: Pipeline Stage Failure & Timing
- **Failing Job/Step**: `deploy` job -> `Deploy to Cloud Run` step.
- **Expected Duration**: Workflow fails in approximately 2–4 minutes (container startup probe timeout).
- **Log Snippet**:
  ```
  ERROR: (gcloud.run.deploy) Container failed to start. Failed to start and then listen on the port defined by the PORT environment variable.
  Memory limit of 256 MiB exceeded with 584 MiB used.
  Container terminated with exit code 137.
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Transitioned to `In Progress (2)`.
- **Work Notes**: Correlates runtime container termination telemetry with modified memory flags in `scripts/deploy.sh`.
- **Attachment**: Full investigation report with telemetry log quotes.

#### Step 4: Expected GitHub Write-Back
- **Check Run**: Links commit to ServiceNow ticket.
- **Commit Comment**: Quotes exact decisive memory exhaustion log line and specifies the exact parameter in the diff causing the failure.

#### Step 5: What a Good Diagnosis Looks Like
- **Root Cause**: Quotes the decisive container termination log line (`Memory limit of 256 MiB exceeded with ... used` / `exit code 137 (OOM)`). Correlates directly with the diff reducing `MEMORY` to `256Mi` and injecting `CACHE_PREWARM_BUFFER_MB=2048`.
- **Causal Remediation**:
  - Revert memory limit reduction in `scripts/deploy.sh` (restore to `512Mi` or higher).
  - Remove or reduce `CACHE_PREWARM_BUFFER_MB` buffer allocation.
- **Unrelated Observations**: Any auxiliary deployment warnings placed in separate section.

#### Step 6: Reset
```bash
cd gdi-sample-pipeline-app
git revert HEAD --no-edit && git push origin main
```

#### Note on Declarative Deployment Invariant
`scripts/deploy.sh` enforces `--clear-env-vars` when `EXTRA_ENV_VARS` is empty. Reverting the commit automatically clears `CACHE_PREWARM_BUFFER_MB=2048` from the Cloud Run service, guaranteeing a clean return to baseline.

---

## 4. Reset & Cleanup Procedure

If you injected defects locally or need an immediate clean slate across all files:

```bash
cd gdi-sample-pipeline-app

# Automated local reset script:
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/reset-scenarios.sh .

# If you committed changes:
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/reset-scenarios.sh . --commit && git push origin main
```

The reset script idempotently:
1. Reverts any scenario commits if HEAD matches a known scenario signature.
2. Restores `app/main.py`, `tests/test_main.py`, `Dockerfile`, and `scripts/deploy.sh` to baseline.
3. Cleans any `.bak` or `.orig` temporary files.

---

## 5. Troubleshooting & FAQ

### Webhook not received by Bridge
- Verify GitHub webhook delivery logs under **Settings -> Webhooks -> Recent Deliveries**.
- Confirm the `X-Hub-Signature-256` secret matches `GITHUB_WEBHOOK_SECRET` on the bridge.
- Check bridge logs for HTTP 200/401 responses.

### ServiceNow Incident not updating
- Verify ServiceNow credentials and user permissions for `api/now/table/incident` and `api/now/attachment/file`.
- Check bridge logs for correlation ID query matches (`correlation_id=gdi:<repo>:<run_id>:<attempt>`).

### Check Run or Comment not appearing on GitHub
- Verify the GitHub App installation has `checks:write` and `pull_requests:write` (or `contents:write`) permissions.
- If using `GITHUB_TOKEN`, ensure the personal access token has `repo` scope.

---

*For scenario implementation details and tooling catalog, see [SCENARIOS.md](SCENARIOS.md).*
