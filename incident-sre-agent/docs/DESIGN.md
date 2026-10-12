# Incident SRE Agent — Design (As-Built)

## 1. Overview
When an incident is created in ServiceNow or Jira Cloud, the ticketing system dispatches an automated webhook. The Incident SRE Agent running on Google Cloud investigates telemetry across Cloud Logging, Cloud Monitoring, Cloud Audit Logs, and Cloud Asset state in a strictly read-only posture and delivers its findings, probable root cause analysis, timeline, and remediation playbooks back to the incident ticket.

Key Operational Objectives:
- **Rapid Initial Triage**: Automated first-pass investigation completed within approximately 10 minutes of ticket creation.
- **Strictly Read-Only Posture**: The agent holds zero mutative permissions across GCP resources and ticketing platforms.
- **Ticketing Agnostic**: Pluggable adapter architecture supporting ServiceNow and Jira Cloud out of the box, with a generic HMAC adapter for automated testing and extensibility.
- **Runbook Extensibility via Skills**: Investigation logic, diagnostic playbooks, and service-specific knowledge are extended via open Agent Skills without modifying core agent code.
- **Reproducible Enterprise Deployment**: Parameterized infrastructure deployed via Terraform with configurable resource naming prefixes (`name_prefix`), standard resource labels, GCS remote state backend, and container images tagged with immutable git commit SHAs.

Scope: Target GCP project with application and infrastructure telemetry in Google Cloud Logging, Cloud Monitoring, Cloud Audit Logs, and Google Vertex AI.

---

## 2. Key Decisions

| Topic | Decision | Details |
|---|---|---|
| **LLM & Orchestration** | Gemini 3.8-flash on Vertex AI (`VERTEX_LOCATION=global`), Google Agent Development Kit (ADK) | Model parameterized via `MODEL_DISPATCHER`, `MODEL_SPECIALIST`, `MODEL_SYNTHESIZER` defaulting to `gemini-3.8-flash`. Served via `global` Vertex AI endpoint. |
| **Language & Tooling** | Python 3.12+, FastAPI, `uv`, `ruff`, `mypy`, `pytest` | Strict static typing, async ASGI web services, automated formatting and linting. |
| **Core Architecture** | 2 Cloud Run services (`incident-bridge` & `sre-agent`), 1 Cloud Tasks queue (`investigations`) | Decoupled webhook ingestion (`incident-bridge`) from asynchronous multi-agent telemetry reasoning (`sre-agent`). |
| **Resource Naming & Labels** | Configurable `name_prefix` (default `"isa"`, live prefix `"acme"`) | Uniformly composed as `${name_prefix}-${base_name}` with enterprise `common_labels` (`app`, `component`, `env`, `prefix`). |
| **State & Deduplication** | No external database; deterministic Cloud Tasks & in-memory `CallbackDedupRegistry` | Webhooks deduplicated via named Cloud Tasks (`inv-<sha256>`). Callbacks deduplicated at the bridge via thread-safe TTL `CallbackDedupRegistry`. |
| **Health Check Probing** | Single `/health` endpoint across all services | Avoids Google Frontend (GFE) Cloud Run edge-proxy 404 interception of `/healthz`. |
| **Ticketing Integration** | ServiceNow, Jira Cloud, and Generic HMAC adapters | ServiceNow HTML work notes (`[code]...[/code]`) and Jira ADF rich comments with standalone HTML report attachments. |
| **Security & Read-Only** | Strict read-only IAM viewer roles; zero secret payload inspection | `agent-sa` restricted to viewer roles and `aiplatform.user`. Bridge holds Secret Manager access; inspection of secret payloads is strictly prohibited. |
| **Skills Architecture** | Google GenAI ADK `SkillToolset` with de-overfit rules | Progressive disclosure of open Agent Skills (`SKILL.md`). De-overfit rules enforce generic reasoning without canned answers or scenario labels. |
| **Demo Infrastructure** | Cloud Run microservices with real GCP OIDC ID tokens | `shop-api` fetches real Google ID tokens (`fetch_id_token`) to invoke private IAM-only `inventory-svc`. |
| **Terraform Remote State** | GCS state backend bootstrapped via `scripts/bootstrap-tfstate.sh` | Dedicated state bucket (`<project>-<prefix>-tfstate`) with uniform access, public access prevention, and object versioning. |
| **Deployment Region** | Compute in `us-central1`; Vertex AI in `global` | Cloud Run, Cloud Tasks, and Artifact Registry hosted in `us-central1`; Gemini 3.x models accessed via Vertex AI `global`. |

---

## 3. System Architecture & Request Lifecycle

```
ServiceNow Flow (Incident created) ─────────┐           Jira Automation (Issue created) ─────────┐
  REST step, Bearer / Basic auth token      │             Send web request, custom JSON body     │
                                            ▼                                                    ▼
┌─────────────────────── Cloud Run: ${name_prefix}-incident-bridge (Public) ─────────────────────┐
│ GET /health (Single health probe endpoint)                                                     │
│ POST /webhook/{source}                                                                         │
│   1. adapter.verify (token / HMAC comparison in constant time)                                 │
│   2. adapter.to_incident -> canonical Incident model                                           │
│   3. Enqueue Cloud Task named inv-<sha256(source:external_id)> in ${name_prefix}-investigations │
│      (ALREADY_EXISTS = duplicate delivery -> HTTP 200 OK duplicate_ignored)                    │
│   4. adapter.post_ack (work note / comment + state -> In Progress)                             │
│   5. Return HTTP 202 Accepted                                                                  │
│ POST /callback/report                                                                          │
│   1. Authenticate caller (Google OIDC token signature, audience, caller SA == agent-sa)        │
│   2. CallbackDedupRegistry.check_and_mark(incident_id, status) (in-memory TTL deduplication)   │
│   3. adapter.post_report (HTML work note / ADF comment + standalone HTML report attachment)   │
└───────────────────────────────────────────────┬────────────────────────────────────────────────┘
        │ Cloud Tasks queue "${name_prefix}-investigations" (OIDC as tasks-invoker-sa, 15m deadline)
        │ Body: {incident, callback_url}
        ▼
┌─────────────────────── Cloud Run: ${name_prefix}-sre-agent (Private / IAM Auth) ───────────────┐
│ GET /health (Single health probe endpoint)                                                     │
│ POST /investigate                                                                              │
│   1. Authenticate caller (OIDC token audience matching service URL)                            │
│   2. ADK Dispatcher Pipeline (with SkillToolset):                                              │
│        Triage (LlmAgent) -> extracts service, symptoms, dynamic window [opened_at-60m, now]    │
│        ParallelAgent Specialists -> Logs, Metrics, Changes, Resources (concurrent read-only)   │
│        Review Step -> optional targeted follow-up query round (<= 1 round)                     │
│        Synthesizer (LlmAgent) -> correlates evidence into canonical Report schema              │
│   3. Outbound Callback: Mints Google ID token (audience = callback_url)                        │
│        Delivers InvestigationResult to callback_url with exponential retry on 5xx              │
│   4. Audit Logging: Emits full report as structured log entry (jsonPayload.type="sre_report")  │
└────────────────────────────────────────────────────────────────────────────────────────────────┘
```

### 3.1 Request Flow
1. **Webhook Ingestion**: An incident ticket created in ServiceNow or Jira triggers an automated webhook POST to `${name_prefix}-incident-bridge` at `/webhook/{source}`.
2. **Authentication & Normalization**: The bridge validates the webhook credentials using constant-time comparisons, parses the payload, and transforms it into the canonical `Incident` model.
3. **Deterministic Task Deduplication**: The bridge enqueues a Cloud Task with deterministic task ID `inv-<sha256(source:external_id)>` in the `${name_prefix}-investigations` queue. If an identical webhook arrives again, Cloud Tasks returns `ALREADY_EXISTS`, and the bridge responds with HTTP 200 OK (`duplicate_ignored`) without duplicate ticket updates.
4. **Immediate Acknowledgment**: The bridge invokes `adapter.post_ack` to post an initial progress note and transition the ticket to "In Progress", then returns HTTP 202 Accepted.
5. **Agent Invocation**: Cloud Tasks calls `${name_prefix}-sre-agent` at `/investigate` using a Google-signed OIDC token minted for `tasks-invoker-sa`.
6. **Multi-Agent Telemetry Reasoning**: The agent executes the ADK investigation pipeline within a 15-minute budget:
   - **Triage**: Inspects ticket text, identifies target services, and establishes the dynamic time window.
   - **Parallel Specialists**: Queries Cloud Logging, Cloud Monitoring, Cloud Audit Logs, and Cloud Run/Compute/SQL resources concurrently.
   - **Review**: Evaluates preliminary evidence and triggers at most 1 round of targeted follow-up queries.
   - **Synthesis**: Correlates multi-signal telemetry into the canonical `Report` contract with chronological timeline, grounded root cause hypothesis, and confidence scoring.
7. **Callback Delivery & Deduplication**: The agent obtains an OIDC token matching the bridge callback URL and POSTs the result. The bridge verifies the token and caller identity (`agent-sa`), checks `CallbackDedupRegistry` to prevent redundant ticket updates upon retries, renders platform-native findings (HTML work notes in ServiceNow, ADF rich comments in Jira), and attaches the full standalone HTML report.

### 3.2 Security Between Components
- **Bridge -> Agent**: Invocations travel via Cloud Tasks with Google OIDC ID tokens. Cloud Run requires authentication; only `${name_prefix}-tasks-invoker-sa` is granted `roles/run.invoker`.
- **Agent -> Bridge**: The agent mints an OIDC token via the Google Compute metadata server with audience set to the bridge callback URL. The bridge validates the signature, audience, and caller email (`${name_prefix}-agent-sa`).
- **Callback Host Allowlist**: Outbound agent callbacks are strictly constrained to hosts matching `ALLOWED_CALLBACK_HOSTS` (`*.run.app` by default).
- **Prohibition on Secret Inspection**: Neither the agent nor any operator tools inspect or output secret values. The agent holds zero Secret Manager permissions. Secret Manager containers are accessed exclusively by `bridge-sa` at runtime.

### 3.3 Reliability & Fault Tolerance
- **Inbound Deduplication**: Named Cloud Tasks preserve idempotency across duplicate webhook deliveries for up to 1 hour after execution.
- **Callback Deduplication**: The bridge enforces in-memory TTL deduplication (`CallbackDedupRegistry`) keyed on `(incident_id, status)` to prevent double-posting during agent retry attempts.
- **Graceful Partial Failure**: If an individual telemetry specialist encounters API errors or timeouts, the synthesizer produces a report noting the missing domain (`specialists_status`), down-calibrates confidence, and delivers available evidence rather than failing the run.
- **Audit Log Durability**: Before posting results, the agent writes the complete investigation report as a single structured log entry (`jsonPayload.type="sre_report"`), ensuring findings remain recoverable even if callback delivery fails.

---

## 4. Canonical Data Contracts
Both `bridge/app/contracts/` and `agent/app/contracts/` maintain byte-identical Pydantic v2 data models verified by `scripts/check-contracts.sh`:

- `Incident`: `incident_id`, `source`, `external_id`, `external_key`, `external_url`, `title`, `description`, `severity` (critical, high, medium, low), `service_hint`, `labels`, `opened_at`, `reporter`.
- `InvestigationRequest`: `incident`, `callback_url`, `requested_at`.
- `EvidenceItem`: `kind`, `source`, `excerpt`, `timestamp`, `console_url`, `details`.
- `Findings`: `summary`, `evidence`, `anomalies`, `queries_run`, `confidence`, `errors`.
- `TimelineItem`: `timestamp`, `event`, `source`, `is_anomaly`.
- `ProbableRootCause`: `text`, `confidence` (0.0 to 1.0).
- `RecentChangeItem`: `change_type`, `description`, `timestamp`, `author`, `console_url`.
- `SpecialistStatus`: `status` (`completed`, `partial`, `failed`, `skipped`), `error`.
- `Report`: `summary`, `impact`, `timeline`, `evidence_by_specialist`, `probable_root_cause`, `recent_changes`, `recommended_actions`, `specialists_status`, `skills_used`, `caveats`, `queries_run`, `metadata`.
- `InvestigationResult`: `investigation_id`, `incident_id`, `external_key`, `status` (`completed`, `failed`), `report`, `error`.

---

## 5. Incident Bridge Architecture (`bridge`)

### 5.1 Ticketing Adapters
Core bridge routing is completely decoupled from ticketing vendors through `bridge/app/adapters/base.py`:
- `verify(request)`: Validates inbound authentication headers.
- `to_incident(payload)`: Normalizes platform JSON into canonical `Incident`.
- `post_ack(ref)`: Acknowledges incident and transitions status.
- `post_report(ref, report)`: Formats and posts findings note and uploads HTML report attachment.
- `post_failure(ref, error)`: Posts notice of investigation failure.

### 5.2 ServiceNow Adapter
- **Inbound**: Flow Designer action sending `Authorization: Bearer <token>` or HTTP Basic auth.
- **Acknowledgment**: `PATCH /api/now/table/incident/{sys_id}` setting `state=2` (In Progress) and adding initial work note.
- **Reporting**: Formats findings as HTML wrapped in `[code]...[/code]` tags (respecting `glide.ui.security.allow_codetag`). Automatically splits large work notes across configurable character chunks (`SERVICENOW_CHUNK_SIZE`, default 12,000 chars). Uploads full HTML report via ServiceNow Attachment API.

### 5.3 Jira Cloud Adapter
- **Inbound**: Automation rule sending web request with `X-Webhook-Token` or Bearer header and custom JSON body utilizing quoted `.jsonEncode` smart values.
- **Acknowledgment**: Transitions issue to `In Progress` (`JIRA_IN_PROGRESS_TRANSITION`) and posts acknowledgment comment.
- **Reporting**: Formats findings in Atlassian Document Format (ADF) with color-coded severity panels, timeline bullet lists, and telemetry code blocks. Uploads full HTML report attachment via Jira Attachment API.

### 5.4 In-Memory Callback Deduplication Registry (`CallbackDedupRegistry`)
To prevent duplicate work notes or comments if the SRE Agent retries callback submission across transient network drops:
- Implemented as a thread-safe in-memory TTL cache in `bridge/app/core/dedup.py`.
- Tracks `(incident_id, status)` tuples with configurable TTL (default 600.0 seconds).
- Atomically checks and marks entries (`check_and_mark`), returning HTTP 200 OK (`{"status": "duplicate_ignored"}`) for duplicates.

---

## 6. SRE Agent Architecture (`agent`)

### 6.1 Google ADK Multi-Agent Orchestration
The SRE Agent uses Google Agent Development Kit (ADK) constructs to implement deterministic investigation control flow:
1. **Triage Agent** (`LlmAgent`): Parses incident ticket fields, determines candidate service names, identifies reported symptoms, calculates the dynamic investigation time window (`[opened_at - 60m, now]` clamped to 24 hours), and formulates an `InvestigationPlan`.
2. **Parallel Specialists** (`ParallelAgent`): Executes selected domain specialists concurrently:
   - **Logs Specialist**: Queries Cloud Logging using Cloud Run request log filters (`run.googleapis.com/requests`), extracts error groups, and summarizes HTTP status distributions.
   - **Metrics Specialist**: Queries Cloud Monitoring time series for request rates (`run.googleapis.com/request_count`), request latencies (`run.googleapis.com/request_latencies`), and resource utilization. Compares incident window against pre-incident baseline.
   - **Changes Specialist**: Inspects Cloud Audit Logs for administrative actions (`UpdateService`, `CreateRevision`), deployment authors, and container image updates.
   - **Resources Specialist**: Describes Cloud Run service specifications, CPU/memory limits, instance counts, and runtime configurations (with strict redaction of sensitive environment variable values).
3. **Review Evaluator**: Inspects specialist findings against the triage plan. If anomalies require targeted drill-down, executes at most one focused follow-up query round.
4. **Synthesizer Agent** (`LlmAgent`): Uses Vertex AI Gemini to correlate specialist evidence, establish chronological timelines, articulate root cause mechanisms, calibrate confidence scores, and populate actionable remediation playbooks.

### 6.2 Model Configuration & Global Vertex Endpoint
- **Default Model**: GA `gemini-3.8-flash` configured across all roles (`MODEL_DISPATCHER`, `MODEL_SPECIALIST`, `MODEL_SYNTHESIZER`).
- **Global Endpoint**: Initialized with `VERTEX_LOCATION=global`. Gemini 3.x models are served exclusively through the global Vertex AI endpoint, decoupled from regional compute (`GCP_REGION=us-central1`).

### 6.3 Progressive Disclosure Skills & De-Overfitting
- Open Agent Skills loaded via Google GenAI ADK `SkillToolset` from `agent/skills/<skill-name>/` mapped in `skills.yaml`.
- **Dynamic Skills Tracking**: The agent reports only skills that were actively consulted during the investigation.
- **Strict De-Overfit Rules**: Agent code and prompt templates are free of hardcoded service names, scenario identifiers, synthetic error signatures, or post-LLM template overrides. Generic SRE playbooks guide investigation reasoning across error spikes, latency degradation, configuration regressions, memory saturation, and dependency failures.

---

## 7. Read-Only Enforcement & Security Guardrails

1. **Principle of Least Privilege (IAM)**:
   - `${name_prefix}-agent-sa` is granted strictly read-only roles: `roles/logging.viewer`, `roles/monitoring.viewer`, `roles/run.viewer`, `roles/compute.viewer`, `roles/container.viewer`, `roles/cloudsql.viewer`, `roles/cloudbuild.builds.viewer`, and `roles/aiplatform.user`.
   - The agent possesses zero write, deploy, or administrative permissions.
2. **Tool Immutability**:
   - All tools are decorated with `@readonly_tool` in `agent/app/tools/registry.py`. Tool registrations and signatures are validated by unit tests asserting zero mutative API methods.
3. **Zero Secret Exposure**:
   - The agent service account has no Secret Manager access.
   - Operational security strictly prohibits inspecting, printing, or logging secret payloads. Environment variable inspection tools in `ResourcesSpecialist` redact all secret values, exposing only key names.
4. **Input Sanitization & Injection Defense**:
   - Unauthenticated or user-provided incident ticket text is treated as untrusted input. The agent executes zero shell commands, code interpreters, or arbitrary queries based on ticket text.

---

## 8. Infrastructure Architecture (Terraform)

### 8.1 Modular Layout & Naming Prefix
Infrastructure is partitioned into independent Terraform stacks with standard variable naming:
- `agent/infra/terraform`: Cloud Tasks queue, `agent-sa`, `tasks-invoker-sa`, Artifact Registry repository, and Cloud Run service.
- `bridge/infra/terraform`: `bridge-sa`, Artifact Registry repository, Secret Manager secret containers, and Cloud Run service.
- `demo/infra/terraform`: `shop-api-sa`, Artifact Registry repository, and Cloud Run services (`shop-api`, `inventory-svc`).

Every resource includes the configurable `name_prefix` (default `"isa"`), allowing multiple isolated environments (e.g. `acme-*`) within the same GCP project without naming collisions.

### 8.2 Common Resource Labels
All labelable resources apply standard enterprise labels:
```hcl
locals {
  common_labels = {
    app       = "incident-sre-agent"
    component = var.component
    env       = var.environment
    prefix    = var.name_prefix
  }
}
```

### 8.3 Remote State Bootstrap (`scripts/bootstrap-tfstate.sh`)
Terraform stacks utilize partial GCS backends (`backend "gcs" {}`). State storage is bootstrapped prior to deployment:
- **Bucket Naming**: `<project_id>-<name_prefix>-tfstate` (e.g. `example-project-id-acme-tfstate`).
- **Security Guardrails**: Uniform bucket-level access enabled, public access prevention enforced, and object versioning active for state disaster recovery.

---

## 9. Configuration & Secret Management
- **Hierarchical Configuration**: Loaded via `pydantic-settings` in `config.py` from environment variables and `.env` files.
- **Fail-Fast Production Validation**: In non-local environments, missing project IDs or invalid configuration parameters immediately halt application startup.
- **Secret Isolation**: Secret Manager provisions container resources in Terraform without secret values. Real credentials for ServiceNow, Jira, and HMAC keys are injected via `bridge/scripts/set-secrets.sh` and mounted as Cloud Run secret environment variables.

---

## 10. Demo Microservices Stack (`demo`)

The demo stack simulates a realistic e-commerce storefront:
- **`shop-api`**: FastAPI storefront exposing `/catalog`, `/checkout`, `/readyz`, `/health`, and fault-control endpoints.
- **`inventory-svc`**: FastAPI backend tracking stock reservations. Configured with `ingress = "INGRESS_TRAFFIC_ALL"` but strictly restricted by IAM to `roles/run.invoker` for `shop-api-sa`.
- **Authenticated Inter-Service Calls**: `shop-api` acquires authentic Google Cloud Run OIDC identity tokens using `id_token.fetch_id_token(Request(), audience=inventory_url)` to authenticate against `inventory-svc`.
- **Load Generator (`loadgen.py`)**: Asynchronous HTTP client providing continuous background traffic across catalog browsing, item inspection, and checkout flows.
- **Fault Scenarios**:
  - Scenario 1: Error Spike (Database connection pool exhaustion).
  - Scenario 2: Latency Spike (Application delay injection).
  - Scenario 3: Bad Release (Missing configuration; currently in-app fault switch, with real Cloud Run revision deployment recorded in backlog).
  - Scenario 4: Memory Leak (Progressive memory accumulation leading to Cloud Run kernel OOM termination).
  - Scenario 5: Dependency Failure (Downstream 403 Forbidden; currently in-app token suppression switch, with real IAM binding revocation recorded in backlog).

---

## 11. Verification & Testing Strategy
The platform is validated across a 5-tier testing hierarchy:
1. **Unit & Component Tests**: 309+ pytest tests verifying adapters, tools, specialists, synthesizer, deduplication, and configuration without cloud dependencies.
2. **Byte-Level Contract Parity**: `scripts/check-contracts.sh` presubmit gate enforcing exact data contract synchronization between `agent` and `bridge`.
3. **De-Overfit & Static Guards**: Automated AST guards (`check-no-demo-overfit.sh`, `check-no-rootcause-overrides.py`, `check-no-demo-log-hints.py`) preventing hardcoded scenario answers, post-LLM template overrides, and demo log confessions.
4. **Local Multi-Container E2E**: Docker Compose environment running `bridge`, `agent`, and a fake ticketing server verifying full webhook-to-report lifecycles.
5. **Post-Deployment Smoke Tests**: `scripts/smoke-test.sh` automating health check probes, authentication rejection, deduplication idempotency, and read-only probe investigations against live Cloud Run deployments.
