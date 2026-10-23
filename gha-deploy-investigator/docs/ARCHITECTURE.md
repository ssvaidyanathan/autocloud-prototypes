# System Architecture: GitHub Actions Deployment Failure Investigator

The **GitHub Actions Deployment Failure Investigator (`gha-deploy-investigator`)** is an autonomous SRE and CI/CD diagnostic engine that investigates failed GitHub Actions workflow runs, pinpoints root causes across all pipeline stages, creates enterprise ServiceNow incident records, and writes diagnostic findings and ticket links directly back to GitHub Check Runs and Pull Requests.

---

## 1. High-Level Architecture

```
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                       GitHub Platform                                           │
│  - GitHub Actions Workflow Run (lint, test, build, deploy failure)                              │
│  - GitHub Webhook Event (`workflow_job.completed` / `workflow_run.completed`)                   │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ 1. POST /webhook/github (HMAC SHA-256)
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ gdi-bridge (Public Ingress Cloud Run Service)                                                   │
│  • Validates X-Hub-Signature-256 using Secret Manager GITHUB_WEBHOOK_SECRET                     │
│  • Deduplicates events using deterministic Cloud Tasks task IDs (HTTP 200 duplicate_ignored)    │
│  • Normalizes GitHub Actions payload into canonical Incident model                              │
│  • Enqueues asynchronous task to Cloud Tasks queue                                              │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ 2. OIDC Authenticated Cloud Tasks Task
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ gdi-agent (Internal Ingress Cloud Run Service)                                                  │
│  • Dispatcher triages incident & identifies failed pipeline stage                               │
│  • Parallel Diagnostic Specialists inspect telemetry:                                           │
│    ├─ PipelineLogsSpecialist: parses GitHub Actions step logs, extracts errors & stack traces   │
│    ├─ RuntimeHealthSpecialist: correlates Cloud Run revision crashes & container OOMs           │
│    └─ IaCConfigSpecialist: analyzes Terraform and build configuration                           │
│  • RootCauseSynthesizer: LLM-driven root cause reasoning & structured DiagnosticReport          │
│  • Delivers completed investigation back to Bridge                                              │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ 3. POST /callback/report (investigation findings)
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ gdi-bridge (Callback Handler)                                                                   │
│  • Formats executive summary and standalone HTML investigation dossier                          │
│  • Creates ServiceNow Incident (POST /api/now/table/incident) with state "In Progress"          │
│  • Attaches full HTML report (POST /api/now/attachment/file)                                    │
│  • Updates incident work notes with root cause hypothesis, evidence, and suggested fix          │
│  • Dual write-back to GitHub Check Run & PR/commit comment with ServiceNow reference            │
└───────────────────────────────────────────────┬─────────────────────────────────────────────────┘
                                                │ 4. REST Table & Attachment API
                                                ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────┐
│ ServiceNow Enterprise Platform (Incident Ticket INC0019482 Created & Enriched)                 │
└─────────────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 2. Ingestion & Bridge Architecture (`gdi-bridge`)

### 2.1 Webhook Authentication & Deduplication
- **Inbound Endpoint**: `POST /webhook/github`.
- **Signature Verification**: Every request must provide a valid `X-Hub-Signature-256` header calculated as HMAC-SHA256 of the raw body payload with `GITHUB_WEBHOOK_SECRET`. Unsigned or tampered requests are rejected immediately with HTTP 401.
- **Idempotency Guard**: Webhook deliveries are deduplicated using deterministic Cloud Tasks task naming (`inv-<hash of run_id[:attempt]>`). Deliveries already enqueued within the deduplication window return HTTP 200 `{"status": "duplicate_ignored"}` with zero downstream Cloud Tasks tasks generated.

### 2.2 Normalization to Canonical Incident Contract
The bridge normalizes heterogeneous GitHub webhook events (`workflow_job`, `workflow_run`, `check_run`) into a byte-identical canonical `Incident` model:
- `id`: Formatted as `gha-<owner>-<repo>-<run_id>-<job_id>`.
- `title`: `GitHub Actions Failure: <workflow_name> / <job_name> (<stage>)`.
- `description`: Contains repository, commit SHA, branch/ref, failing step name, and workflow URLs.
- `severity`: Standardized severity level based on failure conclusion and branch criticality.
- `tags`: Includes stage classification tag (`stage:lint`, `stage:test`, `stage:build`, `stage:deploy`).

### 2.3 Asynchronous Queueing via Cloud Tasks
Rather than executing long-running telemetry queries during the HTTP webhook handshake, `gdi-bridge` enqueues an asynchronous HTTP task to `gdi-tasks-queue`.
- **Target URL**: Private `gdi-agent` service endpoint (`POST /investigate`).
- **Authorization**: Google Cloud Tasks OIDC token with audience matching `gdi-agent` Cloud Run service.

---

## 3. Autonomous Diagnostic Engine (`gdi-agent`)

The agent is deployed with `INGRESS_TRAFFIC_INTERNAL_ONLY` and cannot be reached directly from the public internet.

### 3.1 Stage Triage & Specialist Dispatch
Upon receiving an investigation request, the `Dispatcher` inspects the incident context and dispatches specialized diagnostic sub-agents:

| Stage | Responsible Specialist | Telemetry Sources | Diagnostic Focus |
|---|---|---|---|
| **Lint / Static Analysis** | `PipelineLogsSpecialist` | GitHub Actions step logs | Syntax errors, unused imports, formatting rules (ruff, flake8, black, eslint) |
| **Unit / Integration Test** | `PipelineLogsSpecialist` | Test execution logs, XML/JSON reports | Failing test suite, assertion mismatch, traceback, exit codes |
| **Build & Packaging** | `PipelineLogsSpecialist` | Container build logs | Dockerfile syntax, missing context files, dependency resolution errors |
| **Deploy & Runtime** | `RuntimeHealthSpecialist` | Cloud Run APIs, Cloud Logging, Cloud Monitoring | Container startup crash, memory exhaustion (OOM exit 137), health check timeouts |

### 3.2 Read-Only Repository & GitHub App Authentication
- When inspecting code changes, `IaCConfigSpecialist` uses a GitHub App installation token generated from the App private key (`GITHUB_APP_PRIVATE_KEY`) and App ID (`GITHUB_APP_ID`).
- All tools are decorated with `@readonly_tool` and verified via `assert_all_readonly()`. The agent cannot mutate source repositories, commit code, or alter branch protections.

### 3.3 Root Cause Synthesis & Guardrails
The `RootCauseSynthesizer` correlates findings across specialists and generates a structured `DiagnosticReport`:
- `probable_root_cause`: Synthesized explanation with confidence rating (`HIGH`, `MEDIUM`, `LOW`).
- `evidence`: Specific log lines, container exit codes, and commit diff snippets.
- `suggested_action`: Actionable, copy-pasteable remediation commands.
- **AST Static Guard**: The codebase is protected by `scripts/check-no-rootcause-overrides.py`, enforcing that post-LLM logic cannot overwrite root causes or artificially inflate confidence outside labeled automated fallback paths.

### 3.4 Declarative Agent Instructions & ADK Hybrid Model
System instructions, agent descriptions, multi-turn drill-down protocols, and synthesis prompt templates are externalized from Python code into [`agent/app/agent.yaml`](../agent/app/agent.yaml):
- **Hybrid Pattern**: Declarative YAML configures agent hierarchies, model references (`${GEMINI_MODEL:-default}`), schemas, and instructions, while Python retains runtime client dependency injection, tool callbacks (`SkillTracker`), and FastAPI routes.
- **Typed Loader & Startup Fail-Fast**: The loader in [`agent/app/agent_config.py`](../agent/app/agent_config.py) validates the schema using typed Pydantic models. If `agent.yaml` is missing or malformed, the service fails fast at startup with zero silent defaults.
- **AI Coding Assistant Guide**: Operational conventions, guardrails, and test commands are documented in [`AGENTS.md`](../AGENTS.md).

---

## 4. Reverse ServiceNow & GitHub Write-Back Flow

### 4.1 Callback Handling in Bridge
Once the investigation is synthesized, `gdi-agent` posts the findings back to `gdi-bridge` (`POST /callback/report`).

### 4.2 ServiceNow Incident Creation
In reverse of standard ITSM workflows where a ServiceNow ticket triggers an investigation, here the CI/CD failure creates the enterprise ServiceNow ticket:
1. `POST /api/now/table/incident`:
   - `short_description`: `CI/CD Failure: <repo> - <job_name> (<stage>)`
   - `description`: Facts-only plain-text summary with repository, branch, short commit SHA, GitHub actor, failing stage, and workflow run URL (no root cause, remediation, observations, or pointer line; all findings and remediation live strictly in work notes and the attachment).
   - `state`: `2` (In Progress) or configured default.
   - `work_notes`: Complete diagnostic report with root cause, evidence, and remediation.
2. `POST /api/now/attachment/file`:
   - Generates a standalone, styled HTML investigation dossier (`investigation-report.html`) and attaches it directly to the created incident ticket.

### 4.3 Asynchronous GitHub Write-Back
To keep developers in their primary workflow surface, the system performs dual write-back to GitHub:
1. **GitHub Check Run**:
   - Creates or updates a GitHub Check Run (`POST /repos/{owner}/{repo}/check-runs`).
   - Title: `CI/CD Failure Investigation: <stage> failure diagnosed`.
   - Summary: Includes probable root cause, suggested remediation, and direct link to the created ServiceNow ticket:
     ```markdown
     ServiceNow Incident: [INC0019482](https://instance.service-now.com/nav_to.do?uri=incident.do?sysparm_query=number=INC0019482)
     ```
2. **Pull Request Comment**:
   - If the failed run was triggered by a pull request, comments directly on the PR with the executive summary and ServiceNow reference.

---

## 5. Security & Isolation Model

- **Least Privilege IAM**:
  - `gdi-bridge-sa`: Cloud Tasks enqueuer, Secret Manager secret accessor.
  - `gdi-agent-sa`: Vertex AI user (`roles/aiplatform.user`), Cloud Logging viewer (`roles/logging.viewer`), Cloud Run viewer (`roles/run.viewer`).
  - `gdi-tasks-invoker-sa`: Cloud Run invoker (`roles/run.invoker`) targeting only `gdi-agent`.
- **Zero Ingestion Secrets in Code**: Webhook HMAC secrets, ServiceNow basic auth credentials, and GitHub App RSA private keys are stored in Secret Manager and injected at container runtime.
- **Hermetic Contract Synchronization**: The data transfer contracts between Bridge and Agent are byte-identical and validated continuously by `scripts/check-contracts.sh`.
