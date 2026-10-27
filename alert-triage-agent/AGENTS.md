# AGENTS.md — Developer & AI Assistant Guide for `alert-triage-agent`

## 1. Purpose & Scope

`alert-triage-agent` is an autonomous Site Reliability Engineering (SRE) diagnostic and incident triage system for Google Cloud Platform environments. It ingests Cloud Monitoring alerts, performs automated multi-agent telemetry investigations across Cloud Run, Cloud SQL, Apigee, and recent deployments, correlates root cause mechanisms from live GCP evidence, and updates ServiceNow incident records with structured HTML work notes and attachments.

The system consists of three cooperating services:
1. **`ata-bridge`**: Lean ingestion and ServiceNow adapter layer: validates Cloud Monitoring alert webhooks with HTTP Basic Auth, enqueues raw alerts directly to Cloud Tasks, and exposes authenticated ServiceNow instruction endpoints (`/instructions/ticket/*`). Completely stateless with zero background loops or alert suppression.
2. **`ata-agent`**: Owns the entire triage lifecycle: evaluates alert correlation, manages incident groups and atomic Open Knowledge Files (OKF) in GCS with generation preconditions, schedules delayed timers on `ata-timer-queue`, evaluates stale-wake deadlines at `/timer/wake`, orchestrates deterministic telemetry specialists concurrently against live GCP APIs, synthesizes evidence-based root cause reports, and calls Bridge Instruction API.
3. **`ata-tools`**: FastMCP read-only telemetry and topology service exposing structured inspection endpoints for GCP resources, topology graph traversal, and blast radius calculation.

---

## 2. Real Production Pipeline Architecture

The end-to-end incident investigation lifecycle flows through deterministic, auditable stages:

```
[Cloud Monitoring Webhook]
          │
          ▼
1. Webhook Ingestion (`bridge/app/main.py: POST /webhook/monitoring`)
   └── HTTP Basic Auth verification; direct dispatch to Cloud Tasks; returns HTTP 202
          │
          ▼
2. Cloud Tasks Dispatch (`ata-triage-queue`)
   └── Target: `agent/app/main.py: POST /triage`
          │
          ▼
3. Agent Grouping & Suppression (`agent/app/grouping/manager.py`)
   ├── In-group duplicate suppression (idempotent 200)
   ├── OKF generation precondition match (`if_generation_match`)
   ├── CorrelationEngine evaluation (DAG distance, policy affinity, temporal window)
   └── Delayed timer scheduling via `agent/app/timers/scheduler.py` (ata-timer-queue)
          │
          ▼
4. Delayed Timer Evaluation (`agent/app/main.py: POST /timer/wake`)
   ├── Stale-wake check: compare now vs okf.settle_deadline (2.0s tolerance)
   ├── Stale wakes ignored without ticket minting
   └── Valid settle / max-wait wakes transition OKF to INVESTIGATING
          │
          ▼
5. Deterministic Specialist Fan-Out (`agent/app/specialists/synthesizer.py`)
   ├── CloudRunSpecialist (`agent/app/specialists/cloud_run.py`)
   ├── ApigeeSpecialist (`agent/app/specialists/apigee.py`)
   ├── CloudSQLSpecialist (`agent/app/specialists/cloud_sql.py`)
   └── ChangeSpecialist (`agent/app/specialists/change.py`)
          │
          ▼
6. Evidence-Based Synthesizer (`agent/app/specialists/synthesizer.py`)
   ├── Probable origin isolation & blast radius calculation via topology DAG
   ├── Root cause synthesis grounded strictly in observed evidence (no "N+1" phrasing)
   ├── Formulaic confidence calculation (evidence-derived, zero constant literals)
   └── 4-bullet executive summary and standalone HTML report generation
          │
          ▼
7. Bridge Instruction API (`agent/app/bridge_client.py` -> `bridge/app/main.py`)
   ├── `POST /instructions/ticket/create` (query correlation_id before create)
   ├── `POST /instructions/ticket/work-note` (late alert / status updates)
   └── `POST /instructions/ticket/resolve` (cool-down ticket closing, only when `AUTO_RESOLVE_ON_RECOVERY=true`; by default a verify-and-resolve work note is posted instead)
          │
          ▼
8. ServiceNow Incident Update (`bridge/app/adapters/servicenow.py`)
   ├── Incident creation / update via ServiceNow Table API
   ├── Formatted work notes appended to incident journal
   └── Rendered HTML investigation report attached
```

### Pipeline Details

1. **Webhook Ingestion & Authorization**:
   - `ata-bridge` exposes `POST /webhook/monitoring` to receive alert notifications from Google Cloud Monitoring via HTTP Basic Auth (`webhook_basicauth` notification channel).
   - Validates alert scope (filtering non-`ata` alerts) and normalizes payloads into `AlertEvent` contracts.

2. **Thin Bridge Dispatch**:
   - `TaskDispatcher` in `bridge/app/core/dispatcher.py` enqueues raw alert payloads directly to Cloud Tasks (`ata-triage-queue`) with HTTP 202 `accepted`.
   - The bridge is completely stateless with zero in-memory windowing, background loops, or alert suppression.

3. **Agent-Owned Grouping & Suppression**:
   - `ata-agent` receives raw alert dispatches from Cloud Tasks at `POST /triage`.
   - `GroupingManager` (`agent/app/grouping/manager.py`) and suppression checks (`agent/app/grouping/suppression.py`) manage incident grouping, in-group duplicate suppression, and atomic Open Knowledge File (OKF) storage in GCS using generation preconditions (`if_generation_match`).
   - The agent schedules delayed timers on `ata-timer-queue` via `agent/app/timers/scheduler.py` for settle, max-wait, and cool-down windows.

4. **Delayed Timer Evaluation (Stale-Wake Pattern)**:
   - When delayed tasks wake at `POST /timer/wake`, the agent compares current timestamp against `okf.settle_deadline`.
   - If subsequent cascading alerts extended the settle window, the wake is recognized as stale and ignored without creating a ticket.
   - On valid settle or max-wait expiry, the agent transitions the incident group to `INVESTIGATING` and initiates multi-specialist telemetry investigations.

5. **Correlation Engine**:
   - `CorrelationEngine` in `agent/app/correlation/engine.py` evaluates candidate alerts against open incidents.
   - Deterministic rule-based scoring checks topology DAG relationships, service boundaries, and temporal proximity.
   - Vertex AI Gemini is invoked **only** for ambiguous gray-zone alerts (scoring between 0.35 and 0.65). Confident matches or clear mismatches are resolved deterministically without LLM overhead.

6. **Deterministic Specialists Fan-Out**:
   - `SynthesizerAgent` runs four deterministic specialists concurrently using `asyncio.gather`:
     - **`CloudRunSpecialist`** ([`agent/app/specialists/cloud_run.py`](agent/app/specialists/cloud_run.py)): Queries Cloud Logging and Cloud Monitoring for container restarts, CPU/memory saturation, request latency percentiles, and application error logs. Strictly filters out administrative fault injection endpoints (such as `/admin/*`). Recursively inspects upstream dependencies identified in the topology DAG.
     - **`ApigeeSpecialist`** ([`agent/app/specialists/apigee.py`](agent/app/specialists/apigee.py)): Queries Apigee access logs in Cloud Logging (`ata-apigee-access`), extracts client IPs and HTTP status codes, pulls proxy response count/latency time series, and analyzes SpikeArrest policy limits.
     - **`CloudSQLSpecialist`** ([`agent/app/specialists/cloud_sql.py`](agent/app/specialists/cloud_sql.py)): Queries Cloud Monitoring for database CPU utilization, memory usage, connection pool saturation, and PostgreSQL database log entries for deadlocks or lock wait timeouts.
     - **`ChangeSpecialist`** ([`agent/app/specialists/change.py`](agent/app/specialists/change.py)): Queries live Cloud Run revision history and Apigee deployment history within the 2-hour window prior to alert onset, correlating release timestamps with error curve initiation.

7. **Evidence-Based Synthesizer**:
   - `SynthesizerAgent` ([`agent/app/specialists/synthesizer.py`](agent/app/specialists/synthesizer.py)) aggregates evidence dossiers across all specialists.
   - Isolates the probable origin service using telemetry metric onset times and topology DAG hierarchy.
   - Computes formulaic confidence derived from concrete evidence factors (e.g., deployment proximity, metric spikes, error log matches).
   - Produces a 4-bullet executive summary and comprehensive HTML diagnostic report, and updates the incident record in the incident store.

8. **Bridge Instruction API & ServiceNow Update**:
   - The agent directs ServiceNow ticket operations by calling authenticated instruction endpoints on `ata-bridge` (`POST /instructions/ticket/*`):
     - `POST /instructions/ticket/create`: Idempotently creates ServiceNow incident records via Table API with correlation tracking.
     - `POST /instructions/ticket/work-note`: Appends timestamped diagnostic notes to incident journal.
     - `POST /instructions/ticket/update-report`: Updates incident description and attaches rendered HTML diagnostic report.
     - `POST /instructions/ticket/update-fields`: Updates the short description (and urgency/impact when a late alert escalates the priority) with a work note, when alerts join an already-ticketed group.
     - `POST /instructions/ticket/resolve`: Resolves incident upon completion of cool-down period.
   - `ServiceNowAdapter` in `bridge/app/adapters/servicenow.py` handles communication with the external ServiceNow Table and Attachment APIs.

---

## 3. Where Settings Live

All configuration in `alert-triage-agent` is managed through typed Pydantic models and infrastructure-as-code:

### Application Environment Variables
- **Agent Configuration**: Defined in typed Pydantic model `AgentConfig` in [`agent/app/config.py`](agent/app/config.py):
  - `PROJECT_ID`: Target Google Cloud project identifier.
  - `REGION`: GCP region (e.g., `us-west1`).
  - `ENVIRONMENT`: Runtime environment name (default `prod`; not set by the deploy scripts). Distinct from the `env` resource label, which the deploy scripts and the Terraform `environment` variable set (live value: `demo`).
  - `INCIDENTS_BUCKET`: Cloud Storage bucket for persistent incident records.
  - `INCIDENT_STORE_PATH`: Local mount or directory for incident files (default: `/knowledge/incident-manager`).
  - `BRIDGE_URL`: Base URL of the `ata-bridge` service for instruction endpoints (`/instructions/ticket/*`).
  - `MODEL_NAME`: Vertex AI Gemini model identifier used for gray-zone correlation (default: `gemini-3.8-flash`).
  - `IS_LOCAL`: Boolean flag indicating local execution mode.
- **Bridge Configuration**: Defined in typed Pydantic model `BridgeConfig` in [`bridge/app/config.py`](bridge/app/config.py):
  - `SERVICENOW_INSTANCE_URL`: Target ServiceNow instance base URL (CONFIG, not a secret; configured via Terraform variable `servicenow_instance_url` or environment variable `SERVICENOW_INSTANCE_URL`).
  - `SERVICENOW_USERNAME` / `SERVICENOW_PASSWORD`: ServiceNow API credentials.
  - `AGENT_URL`: URL of the `ata-agent` service.
  - `PROJECT_NUMBER` and `BRIDGE_URL`: required unless `IS_LOCAL=true`. They give the accepted ID-token audiences on `/instructions/*` (the deterministic Cloud Run URL and the service's own `status.url`).
  - `VERIFIER_SA`: comma-separated extra callers allowed on `/instructions/*` (no operator account is built in).
  - `WEBHOOK_TOKEN`: Shared secret password for webhook HTTP Basic Auth verification.
  - `TASKS_QUEUE`: GCP Cloud Tasks queue name for alert dispatch.

### Infrastructure as Code (Terraform)
- Infrastructure resources, IAM roles, service accounts, Cloud Storage buckets, Cloud Tasks queues, and Cloud Run service definitions are configured in [`infra/terraform/`](infra/terraform/).

---

## 4. Environment & Verification Commands

All changes must pass automated verification cleanly before committing.

### Running Automated Tests

```bash
# Execute the full automated test suite using the project virtualenv:
pytest tests/

# Run specific functional test suites:
pytest tests/test_scenarios_e2e.py
pytest tests/test_correlation_scoring.py
pytest tests/test_container_import_layout.py
pytest tests/test_no_fabricated_data.py
```

---

## 5. Non-Negotiable Quality Guardrails

1. **Deterministic Live GCP Queries**:
   - All diagnostic specialist queries must execute real read-only GCP API calls (Cloud Logging, Cloud Monitoring, Cloud Run Admin, Cloud SQL Admin) or FastMCP tool endpoints.
2. **Strict Read-Only Boundary**:
   - Specialists must never perform mutating operations on cloud infrastructure or services.
3. **Evidence-Based Grounding**:
   - Root cause findings, confidence ratings, and summaries must be directly derived from observed specialist evidence.
   - Never fabricate mock data, synthetic error metrics, or hardcoded candidate names in production code.
4. **Formulaic Confidence**:
   - Confidence scores must be dynamically computed from observed evidence (e.g., proximity of deployment, severity of metric spikes). Hardcoded constant float confidence values are prohibited.
5. **Fault Injection Log Filtering**:
   - Administrative and fault injection endpoints (such as `/admin/*`) must be strictly filtered out of application log analysis.
6. **Zero Forbidden Terms**:
   - Zero occurrences of the forbidden word (p-h-a-s-e).
7. **No Phantom Configurations**:
   - The system uses direct, deterministic Python modules. Configuration is loaded via typed Pydantic models from environment variables. There are no YAML-based agent configurations or LLM dispatcher frameworks.
