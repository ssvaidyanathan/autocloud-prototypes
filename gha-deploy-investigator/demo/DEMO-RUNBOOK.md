# Interactive Demo Runbook: Multi-Stage CI/CD Failure Scenarios

This runbook provides step-by-step instructions for executing and observing the autonomous GitHub Actions Deploy Investigator across all four stages of a modern CI/CD pipeline: **lint**, **test**, **build**, and **deploy**.

---

## 1. Overview & Architecture

When a GitHub Actions workflow run fails, the autonomous diagnostic bridge and investigator agent automatically:
1. **Detect Failure & Enqueue**: Ingest inbound `workflow_run.completed` (status: `failure`) webhooks from GitHub, verify HMAC-SHA256 signature, deduplicate via Cloud Tasks deterministic task naming, and dispatch an async task to the investigator agent (returning `202 Accepted`).
2. **Execute Grounded Diagnosis**: Classify the failure stage (`lint`, `test`, `build`, `deploy`), assign specialized investigation tools, read runner logs, correlate git diffs, inspect container/telemetry logs, segregate unrelated warnings, and formulate grounded causal remediation.
3. **Reverse Incident Creation & Dual Write-Back**:
   - **ServiceNow**: Upon completion callback, create an incident directly in state **In Progress (2)** with structured work notes and attach a standalone HTML investigation report.
   - **GitHub**: Post a neutral Check Run linking to the ServiceNow incident and a rich, collapsible PR/commit comment with root cause, decisive evidence quotes, causal remediation, and segregated unrelated observations.
4. **Auto-Fix PR** (when the bridge runs with `AUTOFIX_ENABLED=true`): Open one fix PR from `autofix/run-<run_id>`, record its link in the ServiceNow work notes, and post the fix PR's CI verification result back to the same incident. The incident stays In Progress for human review (see [Section 4](#4-reset--cleanup-procedure)).

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
5. Reset: close the auto-fix PR with its branch deleted (if one was opened), then `git revert` the scenario commit on `main` and push (see [Section 4](#4-reset--cleanup-procedure)). **Do not merge the fix PR.**
6. Confirm the pipeline on `main` completes with all jobs green before moving to the next scenario.

---

### Scenario 1: Lint & Code Style Violation

#### Injected Defect
Prepends a currency-formatting helper to `app/main.py` with an unused import (`F401`) and multiple statements on a single line (`E702`). Because the helper sits above the module's existing imports, Ruff also reports those imports as `E402` (module-level import not at top of file).

#### Step 1: Inject & Push
From within `gdi-sample-pipeline-app`:
```bash
cd gdi-sample-pipeline-app
git pull origin main

# Inject lint violation, commit, and push
../autocloud-prototypes/gha-deploy-investigator/demo/scenarios/trigger-lint-failure.sh . --commit && git push origin main
```

#### Step 2: Pipeline Stage Failure & Timing
- **Failing Job/Step**: `Code Quality & Lint` job -> `Run Ruff Lint` step.
- **Expected Duration**: Workflow fails in approximately 1–2 minutes.
- **Log Snippet** (abridged):
  ```
  app/main.py:1:8: F401 [*] `math` imported but never used
  app/main.py:5:35: E702 Multiple statements on one line (semicolon)
  app/main.py:7:1: E402 Module level import not at top of file
  ...
  Found 9 errors.
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Created in state `In Progress (2)`.
- **Short Description**: `[CI/CD Failure] ssvaidyanathan/gdi-sample-pipeline-app (lint)`
- **Work Notes**: HTML summary detailing violations with file:line coordinates.
- **Attachment**: Downloadable HTML investigation report `ci_cd_investigation_report_INC00XXXXX.html`.

#### Step 4: Expected GitHub Write-Back
- **Check Run**: `Autocloud / ServiceNow Incident Link` (conclusion: `neutral`) linking to ServiceNow ticket.
- **Annotations**: Inline check run annotations on the reported `app/main.py` violation lines.
- **Commit Comment**: Collapsible breakdown with root cause, log evidence, and suggested fix.

#### Step 5: What a Good Diagnosis Looks Like
- **Root Cause**: Explicitly cites the 2 linter violations (`F401` unused import `math` and `E702` multiple statements) in `app/main.py`.
- **Evidence Quote**: Exact runner log excerpt citing file paths, line numbers, and error codes.
- **Causal Remediation**:
  - Remove unused import `math` from `app/main.py`.
  - Split multi-statement line into separate statements or remove trailing semicolon.
- **Unrelated Observations**: Empty or notes any non-fatal pre-existing tooling warnings.

#### Expected Auto-Fix PR (`deterministic_linter`)
- The bridge's deterministic lint fixer opens one PR from `autofix/run-<run_id>`. The diff touches
  only `app/main.py`: the unused import is removed, the helper is moved below the imports, and the
  multi-statement line is split.
- ServiceNow gets an `[Auto-Fix Initiated] Pull Request #<n> ...` work note with the PR link.
- The PR's CI runs lint, tests and build, which all go green. `Deploy to Cloud Run` shows as
  skipped (it only runs on pushes to `main`).
- A PR comment and an `[Auto-Fix Verification] ... PASSED CI verification ... Ready for human review and merge.`
  work note follow. The incident stays In Progress.

#### Step 6: Reset
Close the fix PR without merging, then revert the scenario commit (see [Section 4](#4-reset--cleanup-procedure)):
```bash
cd gdi-sample-pipeline-app
gh pr list --head autofix/run-<run_id>    # find the fix PR, if one was opened
gh pr close <pr_number> --delete-branch   # skip if no fix PR was opened
git pull origin main
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
- **Failing Job/Step**: `Unit & Integration Tests` job -> `Run Pytest` step.
- **Expected Duration**: Workflow fails in approximately 20–40 seconds.
- **Log Snippet**:
  ```
  FAILED tests/test_main.py::test_customer_tier_discount_calculation - AssertionError: Order discount tier mismatch: expected $100.00, got $75.50
  1 failed, 5 passed in 0.85s
  ```

#### Step 3: Expected ServiceNow Write-Back
- **Incident State**: Transitioned to `In Progress (2)`.
- **Work Notes**: Cites the failing test assertion and diff context confirming modifications are isolated to `tests/test_main.py`.
- **Description**: Facts-only plain-text summary with repository, branch, short commit SHA, actor, failing stage, and workflow URL (no root cause, remediation, observations, or pointer line; all findings and remediation live strictly in Work Notes and the attachment).

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

#### Expected Auto-Fix PR (`llm_patch`)
- The agent's LLM patch opens one PR from `autofix/run-<run_id>`. The diff touches only
  `tests/test_main.py`.
- The seeded test compares hardcoded values with no application code behind them, so the only
  passing fix edits the test so the two values match (either the expected or the actual value).
  A reviewer should reject an auto-fix that edits test expectations; that is the human-review
  step this feature keeps.
- `[Auto-Fix Initiated]` work note with the PR link, then the PR's CI goes green, then a PR comment
  and an `[Auto-Fix Verification] ... PASSED` work note. The incident stays In Progress.

#### Step 6: Reset
Close the fix PR without merging, then revert the scenario commit (see [Section 4](#4-reset--cleanup-procedure)):
```bash
cd gdi-sample-pipeline-app
gh pr list --head autofix/run-<run_id>    # find the fix PR, if one was opened
gh pr close <pr_number> --delete-branch   # skip if no fix PR was opened
git pull origin main
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
- **Failing Job/Step**: `Container Build & Package` job -> `Build Container Image` step.
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

#### Expected Auto-Fix PR (`llm_patch`)
- The agent's LLM patch opens one PR from `autofix/run-<run_id>` touching `Dockerfile` and/or
  `config/`. The ideal fix removes the offending `COPY` line; the LLM may instead add a placeholder
  `config/cache-warmup.json`. Either can happen, because LLM output varies.
- `[Auto-Fix Initiated]` work note with the PR link, then the PR's CI goes green, then a PR comment
  and an `[Auto-Fix Verification] ... PASSED` work note.
- If the agent finds no safe fix, no PR is opened. The incident instead gets an
  `[Auto-Fix Hand-off] No automated fix PR opened: <reason>. Requires human attention.` work note.
  That is the designed no-fix path and counts as correct behaviour.
- Either way the incident stays In Progress.

#### Step 6: Reset
Close the fix PR without merging, then revert the scenario commit (see [Section 4](#4-reset--cleanup-procedure)):
```bash
cd gdi-sample-pipeline-app
gh pr list --head autofix/run-<run_id>    # find the fix PR, if one was opened
gh pr close <pr_number> --delete-branch   # skip if no fix PR was opened
git pull origin main
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

#### Expected Auto-Fix PR (`config_tuning`)
Deterministic fixers take precedence over the agent's LLM patch, so the bridge's memory-config
fixer opens one PR from `autofix/run-<run_id>` that **tunes the container memory limit allocation**
in `scripts/deploy.sh`. It doubles the declared default, capped at 2048Mi. Here that is a one-line
change: `MEMORY="${MEMORY:-256Mi}"` → `MEMORY="${MEMORY:-512Mi}"`. The fixer only changes the
numeric memory value. It does **not** remove `CACHE_PREWARM_BUFFER_MB=2048` from `EXTRA_ENV_VARS`.

- `[Auto-Fix Initiated]` work note with the PR link.
- The PR's CI runs lint, tests and build, which pass. `Deploy to Cloud Run` is **skipped**, because
  it only runs on pushes to `main`.
- A PR comment and a work note then report `[Auto-Fix Verification] ... verification INCONCLUSIVE:
  'Deploy to Cloud Run' did not run on the fix branch ...; requires human validation.`
  This must say **INCONCLUSIVE**, not PASSED: the job that failed never re-ran on the fix branch.
- The incident stays In Progress.

#### Step 6: Reset
Close the fix PR without merging, then revert the scenario commit (see [Section 4](#4-reset--cleanup-procedure)):
```bash
cd gdi-sample-pipeline-app
gh pr list --head autofix/run-<run_id>    # find the fix PR, if one was opened
gh pr close <pr_number> --delete-branch   # skip if no fix PR was opened
git pull origin main
git revert HEAD --no-edit && git push origin main
```
*Wait for GitHub Actions to complete green across all stages before proceeding.*

#### Note on Declarative Deployment Invariant
`scripts/deploy.sh` enforces `--clear-env-vars` when `EXTRA_ENV_VARS` is empty. Reverting the scenario commit (or running the clean-slate reset) empties `EXTRA_ENV_VARS`, which clears `CACHE_PREWARM_BUFFER_MB=2048` from the Cloud Run service and returns it to baseline. The auto-fix PR only raises the memory limit and does not clear that variable, which is another reason to reset by revert rather than by merging the fix PR.

---

## 4. Reset & Cleanup Procedure

With auto-fix ON, every scenario follows the same flow:

**trigger failure → incident + fix PR created → fix PR CI runs → verification note posted → CLOSE fix PR (branch deleted) → REVERT trigger commit on `main` → `main` green.**

**Do not merge the fix PRs.** A merged fix stays on `main`, so the next run of that scenario no
longer reproduces the failure. The fix PR exists to be reviewed, not to restore the demo baseline.

Prerequisite: the bridge must run with `AUTOFIX_ENABLED=true` and a GitHub App/token that can
create branches and pull requests on the demo repository. With the kill switch off
(`AUTOFIX_ENABLED=false`, the default) no fix PR or auto-fix notes are produced. Skip the
close step and just revert (step 3 of 4B).

### A. What to Expect from Auto-Fix

1. After the investigation completes, the bridge opens **one** PR from `autofix/run-<run_id>`
   into the failing branch (`main`). The PR link appears in:
   - the ServiceNow incident work notes (`[Auto-Fix Initiated] Pull Request #<n> created on branch autofix/run-<run_id> ... <PR link>`), and
   - the `Autocloud / ServiceNow Incident Link` Check Run on the failing commit.
2. CI runs on the fix PR. Those runs are verification only: the bridge posts the result as a PR
   comment and an `[Auto-Fix Verification]` work note on the original incident. It **never**
   opens a new incident, starts a new investigation, or makes another fix attempt for them.
   There is exactly **1 attempt** per incident.
3. The verification result is one of:
   - `PASSED`: every job that failed on `main` re-ran on the fix branch and passed (S1, S2, S3).
   - `INCONCLUSIVE`: a job that failed on `main` did not run on the fix branch. `Deploy to Cloud Run`
     only runs on pushes to `main`, so the S4 fix PR always ends here and needs human validation.
   - `FAILED`: the fix PR's CI failed. The note says it requires human attention, and no further
     automated fix is attempted.
4. If no safe fix can be proposed, no PR is opened. Instead the incident gets an
   `[Auto-Fix Hand-off] ... Requires human attention.` work note. This is the designed no-fix path.
5. Whatever the outcome, the incident stays **In Progress**. The bridge never resolves or closes it.

### B. Reset: Close the Fix PR, then Revert

Run this after the verification note (or hand-off note) has posted:

```bash
cd gdi-sample-pipeline-app
# 1. Find the fix PR (skip steps 1-2 if no fix PR was opened)
gh pr list --head autofix/run-<run_id>
# 2. Close it without merging and delete its branch
gh pr close <pr_number> --delete-branch
# 3. Revert the scenario commit on main and push
git pull origin main
git revert HEAD --no-edit && git push origin main
```

If another commit has landed on `main` after the scenario commit, `HEAD` is no longer the scenario
commit. Revert it by SHA instead: `git revert <scenario_commit_sha> --no-edit`.

Then wait for the pipeline on `main` to complete green before starting the next scenario.

### C. Local Clean Slate

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
