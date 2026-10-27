# Alert Triage Agent for Google Cloud Platform

[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](../LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![Terraform: >=1.5.0](https://img.shields.io/badge/Terraform-%3E%3D1.5.0-623ce4.svg)](https://www.terraform.io/)

An autonomous Site Reliability Engineering (SRE) diagnostic and incident triage platform for Google Cloud Platform environments. When Google Cloud Monitoring fires alerts across multi-tier topologies, the Alert Triage Agent (ATA) ingests events via a thin, stateless webhook bridge, correlates related alerts into unified incident groups, orchestrates deterministic telemetry specialists across Cloud Run, Cloud SQL, Apigee X, and recent changes/deployments, synthesizes evidence-grounded root cause findings, and creates and updates ServiceNow incident records with structured executive summaries and standalone responsive HTML dossiers.

---

## Table of Contents

- [1. System Overview & Problem Statement](#1-system-overview--problem-statement)
- [2. Quick Start](#2-quick-start)
- [3. System Architecture & Request Lifecycle](#3-system-architecture--request-lifecycle)
  - [Topology as Single Source of Truth (TOPOLOGY.md)](#topology-as-single-source-of-truth-topologymd)
- [4. Component Breakdown](#4-component-breakdown)
- [5. Configuration](#5-configuration)
- [6. Customizing the Agent with Skills](#6-customizing-the-agent-with-skills)
- [7. Security Model & Production Guardrails](#7-security-model--production-guardrails)
- [8. Prototype Status & Known Limitations](#8-prototype-status--known-limitations)
- [9. Full Step-by-Step GCP Deployment Walkthrough](#9-full-step-by-step-gcp-deployment-walkthrough)
  - [Prerequisites & IAM Requirements](#prerequisites--iam-requirements)
  - [Step 0: Bootstrap GCS Remote State](#step-0-bootstrap-gcs-remote-state)
  - [Step 1: Foundation Module](#step-1-foundation-module)
  - [Step 2: Database Module](#step-2-database-module)
  - [Step 3: Applications Module](#step-3-applications-module)
  - [Step 4: Apigee API Proxies & Products](#step-4-apigee-api-proxies--products)
  - [Step 5: Observability Module](#step-5-observability-module)
  - [Step 6: Triage Tier Services Deployment](#step-6-triage-tier-services-deployment)
  - [Step 7: Secret Manager Configuration](#step-7-secret-manager-configuration)
  - [Step 8: End-to-End Verification](#step-8-end-to-end-verification)
- [10. Master Automated Deployment Script](#10-master-automated-deployment-script)
- [11. Terraform Remote State & GCS Backend](#11-terraform-remote-state--gcs-backend)
- [12. Enterprise Organization Policy Considerations](#12-enterprise-organization-policy-considerations)
- [13. Local Development & Quality Gates](#13-local-development--quality-gates)
- [14. Testing Guide & Post-Deploy Smoke Tests](#14-testing-guide--post-deploy-smoke-tests)
- [15. Teardown & Clean Up](#15-teardown--clean-up)
- [16. Repository Layout](#16-repository-layout)

---

## 1. System Overview & Problem Statement

### The Problem
During production outages in complex cloud environments, on-call engineers face an immediate onslaught of cascading alerts. A single infrastructure root cause—such as a database connection pool exhaustion or a bad configuration rollout—frequently triggers dozens of downstream failure symptoms across API gateways (Apigee), container services (Cloud Run), and backend datastores (Cloud SQL).

This causes severe operational bottlenecks:
- **Alert Fatigue & Ticket Storms**: Disconnected monitoring policies open duplicate, fragmented incident tickets across ticketing platforms like ServiceNow, obscuring the primary incident.
- **Manual Telemetry Correlation**: Engineers spend critical early triage minutes manually querying Cloud Logging streams, inspecting Cloud Monitoring latency metrics, cross-referencing recent Cloud Run revisions, and piecing together failure sequences under pressure.
- **Flapping & Premature Triage**: Transient anomalies or cascading failures trigger half-baked investigations before dependent services have settled, leading to erroneous diagnoses and responder confusion.

### The Solution
The **Alert Triage Agent (ATA)** automates early-stage alert ingestion, intelligent incident grouping, and telemetry investigation through an asynchronous, decoupled **thin-bridge architecture**:
1. **Stateless Webhook Ingestion**: Ingests Cloud Monitoring alert webhooks via HTTP Basic Auth at `ata-bridge` and immediately enqueues raw alert payloads to a Cloud Tasks queue (`ata-triage-queue`) with HTTP 202 `accepted`. The bridge is completely stateless with zero in-memory windowing, background loops, or alert suppression.
2. **Agent-Owned Grouping & Suppression**: The agent core (`ata-agent`) owns all incident grouping logic. It evaluates multi-factor correlation (topology DAG distance, causal failure chains, temporal proximity, and change alignment) against active Open Knowledge Files (OKF) stored in Google Cloud Storage with atomic generation preconditions (`if_generation_match`).
3. **Self-Scheduled Settle & Cool-Down Timers (Stale-Wake Pattern)**: The agent schedules delayed timer tasks on a dedicated queue (`ata-timer-queue`). When an alert arrives, the agent schedules a 120s settle timer. If subsequent related alerts arrive, the settle deadline extends. Upon waking at `/timer/wake`, the agent compares current time against the OKF `settle_deadline`; stale wakes are discarded without ticket creation.
4. **Parallel Read-Only Telemetry Specialists**: Upon settle deadline or max-wait expiry (300s), the agent fans out four deterministic specialists concurrently (`CloudRunSpecialist`, `ApigeeSpecialist`, `CloudSQLSpecialist`, `ChangeSpecialist`) querying live GCP APIs and FastMCP tool endpoints.
5. **Evidence-Grounded Root Cause Attribution**: Vertex AI Gemini reasoning (`gemini-3.8-flash`) is invoked strictly for gray-zone correlation scoring (35–65 points). The Synthesizer isolates the probable origin component, calculates blast radius, and computes an evidence-derived formulaic confidence score without hallucinations or fabricated data.
6. **Rich ServiceNow Delivery**: The agent instructs the bridge via authenticated endpoints (`POST /instructions/ticket/*`) to create or update ServiceNow incident records with structured 4-bullet summaries (Problem, Systems, Observations, Recommended Actions) and attach self-contained, responsive HTML diagnostic dossiers.

---

## 2. Quick Start

### Prerequisites
- **Google Cloud SDK (`gcloud >= 480.0.0`)** installed and authenticated:
  ```bash
  gcloud auth login
  gcloud auth application-default login
  ```
- **Terraform** (`>= 1.5.0`): `terraform version`
- **Docker** daemon active locally OR Google Cloud Build enabled in the target project.
- **Python 3.12+** with `pip` and virtual environment support (`uv` or `python3 -m venv`).
- **Target GCP Project with Billing Enabled**:
  ```bash
  export PROJECT_ID="your-target-project-id"
  export REGION="us-west1"
  export NAME_PREFIX="ata"
  export ENVIRONMENT="demo"
  gcloud config set project "${PROJECT_ID}"
  ```
- **Apigee X Organization & Environment**: Pre-existing Apigee organization with environment `prod` bound to an environment group hostname (e.g. `8.233.247.52.nip.io`).

### Fast Deployment Overview

Deployment is sequenced into automated vertical slices and tier automation (see [Section 9](#9-full-step-by-step-gcp-deployment-walkthrough) for the complete step-by-step walkthrough):

```bash
# 1. Clone repository and enter directory
git clone https://github.com/ssvaidyanathan/autocloud-prototypes.git
cd autocloud-prototypes/alert-triage-agent

# 2. Deploy Stage 1 core triage vertical slice
# (Bootstraps GCS remote state, applies foundation Terraform, deploys inventory API, ata-agent, and ata-bridge)
./scripts/deploy-stage1.sh

# 3. Deploy demo microservices, database, observability, and Apigee proxies
./scripts/deploy-demo-infra.sh

# 4. Deploy FastMCP tools server
(cd tools && ./scripts/deploy.sh)

# 5. Populate ServiceNow credentials in Secret Manager
./scripts/set-secrets.sh
```

### ServiceNow Integration Guide Pointer
The system integrates with ServiceNow via the Table and Attachment APIs:
- **ServiceNow Configuration**: Detailed configuration settings, field mappings, and assignment groups are documented in the [Configuration Guide (`CONFIGURATION.md`)](CONFIGURATION.md) and [Deployment Guide (`DEPLOY.md`)](DEPLOY.md).
- **Credentials & Instance URL**:
  - The ServiceNow instance URL is **configuration**, not a secret. It is configured via the Terraform variable `servicenow_instance_url` in `terraform.tfvars` or the environment variable `SERVICENOW_INSTANCE_URL`.
  - ServiceNow API user credentials (`ata-servicenow-username` and `ata-servicenow-password`) reside securely in Google Secret Manager, populated via `./scripts/set-secrets.sh`.

---

## 3. System Architecture & Request Lifecycle

```
[Cloud Monitoring Alerts]
            │ Webhook (HTTP Basic Auth)
            ▼
┌───────────────────────────┐             ┌───────────────────────────┐
│   Cloud Run: ata-bridge   │ ──────────> │     Cloud Tasks Queue     │
└─────────────┬─────────────┘             │     ata-triage-queue      │
              ▲       │                   └─────────────┬─────────────┘
              │       │ Instructions                    │
              │       │ (POST /instructions/ticket/*)   ▼
              │       │                   ┌───────────────────────────┐
              │       +────────────────── │   Cloud Run: ata-agent    │ <───┐
              │                           │   - Grouping & OKF Store  │     │
              │                           │   - Telemetry Specialists │     │
              │                           └─────────────┬─────────────┘     │
              ▼                                         │                   │
┌───────────────────────────┐                           │ Timers            │ Wake
│   External ServiceNow     │                           ▼                   │
│   Table & Attachment API  │             ┌───────────────────────────┐     │
└───────────────────────────┘             │     Cloud Tasks Queue     │ ────┘
                                          │     ata-timer-queue       │
                                          │     (delayed settle/cool) │
                                          └───────────────────────────┘
```

### Sequence Flow (Mermaid)

```mermaid
sequenceDiagram
    participant Monitoring as Cloud Monitoring
    participant Bridge as ata-bridge
    participant Agent as ata-agent
    participant OKF as GCS Incident Store (OKF)
    participant Telemetry as GCP telemetry APIs
    participant ServiceNow as ServiceNow

    Monitoring->>Bridge: Alert webhook (POST /webhook/monitoring)
    Bridge->>Agent: Dispatch via Cloud Tasks (POST /triage)
    Agent->>OKF: Correlate: create group (OPEN) or attach alert
    Note over Agent: Settle timer fires (POST /timer/wake)
    Agent->>Telemetry: Investigate (origin chosen from all group alerts via topology)
    Agent->>Bridge: POST /instructions/ticket/create
    Bridge->>ServiceNow: Create incident, attach HTML report
    Agent->>OKF: Record ticket (TICKETED)
    Note over Monitoring,Agent: Late alert: same webhook and /triage path
    Agent->>Bridge: Work note and title/priority refresh
    Bridge->>ServiceNow: Update incident
    Note over Agent: All alerts closed, 180s cool-down passed (RECOVERED)
    Agent->>Bridge: POST /instructions/ticket/work-note (recovery note)
    Bridge->>ServiceNow: "Verify and resolve" work note (ticket stays In Progress)
```

Paths not shown in the diagram:

- **Transport and auth**: Cloud Monitoring calls `ata-bridge` with HTTP Basic Auth, and the bridge answers 202 once the alert is enqueued on `ata-triage-queue`. Cloud Tasks calls the agent with an OIDC token. Delayed wakes come from `ata-timer-queue`. The agent calls the bridge's `/instructions/ticket/*` endpoints with a Google ID token. Correlation uses Gemini only for gray-zone scores (35-65). Group writes use GCS generation preconditions (`if_generation_match`). A new group gets a settle timer (120s) and a max-wait timer (300s). A wake before the deadline, or for a group that is no longer open, is ignored. Otherwise the group becomes `INVESTIGATING` and the specialists (Cloud Run, Apigee, Cloud SQL, Change) run in parallel. If every alert has already closed when the ticket is created, the group is set to `COOLING_DOWN` instead of `TICKETED`.
- **Filtering and duplicates**: `ata-bridge` drops non-`ata` alerts with HTTP 200 and does no deduplication. `ata-agent` suppresses repeat alerts for an already-active policy and resource by appending them to the existing incident group.
- **Timer extension**: Each alert that attaches to an open group resets the settle deadline to 120s after that alert's arrival, capped at the 300s max-wait deadline. A wake that arrives more than 2.0s before the group's current deadline is ignored.
- **Origin selection**: The investigation does not start from the first alert. It picks the origin from all of the group's alerts using [`topology/TOPOLOGY.md`](topology/TOPOLOGY.md). A service is explained by a degraded service it depends on, unless that service's onset is more than `ORIGIN_ONSET_TOLERANCE_SECONDS` (default 300) later than its own. Candidates are ranked: unexplained services first, then by how many other alerting services they explain, then by earliest onset, then by arrival order. Gateway-enforced rejections (HTTP 429, client auth) are never explained by a backend. Specialist evidence can still move the origin further upstream, for example to the database.
- **Low traffic**: On a latency incident, if the origin received fewer than `LOW_TRAFFIC_RPM` (default 5) requests per minute in the investigation window, the report says the latency may come from a few slow requests, such as a cold start, rather than a service-wide degradation. No statement is made when the request volume was not measured.
- **Late alerts**: An alert that attaches to an already-ticketed (or cooling-down) group is added to the ServiceNow ticket as a work note (`POST /instructions/ticket/work-note`). A cooling-down group returns to `TICKETED`. The ticket is then refreshed with `POST /instructions/ticket/update-fields`:
  - The title is rebuilt from the group, so its alert count matches the group.
  - The priority is raised when a group alert's `severity` label is higher than the priority already applied. It is never lowered automatically.
  - Each change is logged and comes with a work note naming the new alerts, the old and new title, and any escalation.

  An alert attached while the investigation ran, but not listed in the created ticket, also gets a work note once the ticket exists. An alert of a signal type the ticketed group does not have yet opens a new group instead, if its onset is more than `LATE_NEW_SIGNAL_MAX_ONSET_GAP_SECONDS` (default 600) after the group's first alert. A recent repeat (within 30 minutes) of a policy and resource already in the group is caught by the duplicate check instead: it is appended to the incident group only, with no work note.
- **Cool-down and resolution**: When every alert in a ticketed group has closed (or all are already closed when the ticket is created), the group enters `COOLING_DOWN` and a 180s cooling timer is scheduled. A new alert that attaches during cool-down returns the group to `TICKETED`. If none arrives before that timer fires, what happens depends on `AUTO_RESOLVE_ON_RECOVERY`:
  - `false` (default): the agent does **not** resolve the ticket. Monitoring recovery is not proof of a fix: an alert also closes when its service stops receiving traffic. The agent moves the group to the terminal `RECOVERED` state, then posts one work note (`POST /instructions/ticket/work-note`): "Monitoring alerts closed: all <N> alerts of this incident closed by <time> and none reopened during the 180s cool-down. An alert also closes when its service stops receiving traffic, so a close is not proof of a fix; the agent recorded no remediation. <traffic facts> A human must verify the remediation (...) before resolving this incident." The traffic facts come from Cloud Monitoring `run.googleapis.com/request_count` for each affected Cloud Run service, from `RECOVERY_TRAFFIC_LOOKBACK_SECONDS` before the last close until the note is written. They read either "recorded no requests ... (missing data)", or the request and HTTP 5xx counts, or "not checked" with the reason. The alert state itself carries no close reason. After the note is posted, `recovery_note_posted_at` is recorded; if posting fails, the group goes back to `COOLING_DOWN` so the timer retries the wake. The ServiceNow incident stays In Progress. Like `RESOLVED`, a `RECOVERED` group takes no new alerts, so a later alert opens a new group. A retried cooling wake posts no second note.
  - When alerts close in the demo: latency and 5xx alerts close after a scenario's traffic stops, because the policies treat missing data as not violating (Terraform `alert_evaluation_missing_data`, `EVALUATION_MISSING_DATA_INACTIVE`). Without that setting, a latency series without data waits for the policy's `auto_close`. S4's connections alert closes only after `reset-scenarios.sh` releases the leaked connections. Expected timing per scenario: [DEMO.md §1.2](DEMO.md#12-scenario-by-scenario-guide-s1-through-s5).
  - `true`: the agent calls `POST /instructions/ticket/resolve` and marks the group `RESOLVED`.

  An unticketed group with an explicit suppression reason has no ServiceNow incident, so it is marked `RESOLVED` whatever the flag says. An unticketed group without one is marked `DEGRADED` instead of resolved.
- **Investigation fallback**: If the multi-agent investigation raises an error, the agent falls back to a single-pass report (`generate_investigation_report`) on the topology-selected origin alert. That report is marked degraded, and the ticket is still created.

### Topology as Single Source of Truth (`TOPOLOGY.md`)

The system topology, service catalog, and dependency relationships are defined in [`alert-triage-agent/topology/TOPOLOGY.md`](topology/TOPOLOGY.md) as the single source of truth. The document is structured into two canonical Markdown tables:

- **Components Table**: Defines all monitored services, gateways, datastores, and aliases:
  ```markdown
  | Component | Type | Also known as | Notes |
  ```
- **Dependencies Table**: Encodes directional call paths and transport protocols between services:
  ```markdown
  | From | To | How |
  ```

This topology specification is ingested at runtime via the agent's `get_topology` tool and guided by the `topology` skill. The resulting directed acyclic graph (DAG) is leveraged across the incident triage lifecycle to:
1. **Calculate Topology Hop Distance**: Determines the shortest path between alerting resources in the multi-factor correlation scoring engine (0 hops = 35 pts, 1 hop = 28 pts, 2 hops = 17.5 pts, $\ge$ 3 hops / disjoint = 0 pts).
2. **Isolate Root Cause Origin**: Traverses upstream dependency edges from symptomatic downstream services back to the true failure origin.
3. **Compute Blast Radius**: Identifies all directly and transitively impacted downstream dependencies for inclusion in Open Knowledge Files and ServiceNow incident dossiers.

---

## 4. Component Breakdown

### 1. `ata-bridge` (`bridge/`)
- **Role**: Public-facing, lean ingestion and integration layer.
- **Technology**: Python 3.12+, FastAPI, Pydantic v2, Google Cloud Tasks client, Google Secret Manager client, HTTPX.
- **Responsibilities**:
  - Webhook authentication for Cloud Monitoring via HTTP Basic Auth (`webhook_basicauth` channel).
  - Scope validation: verifies project scoping and filters out foreign or non-`ata` alert notifications.
  - Asynchronous task enqueue: enqueues normalized `AlertEvent` payloads directly to Cloud Tasks (`ata-triage-queue`) returning HTTP 202 `accepted`.
  - ServiceNow instruction endpoints (`POST /instructions/ticket/*`): exposes authenticated routes (`create`, `work-note`, `update-report`, `update-fields`, `resolve`) invoked by `ata-agent`.
  - **Thin-Bridge Guarantee**: Completely stateless with zero in-memory windowing, background polling loops, or alert suppression.

### 2. `ata-agent` (`agent/`)
- **Role**: Private, autonomous SRE diagnostic and incident triage engine.
- **Technology**: Python 3.12+, FastAPI, Google Cloud Storage client, Google Cloud Tasks client, Google GenAI / Vertex AI (`gemini-3.8-flash`), Google Cloud Logging, Google Cloud Monitoring.
- **Responsibilities**:
  - **Grouping & Suppression (`agent/app/grouping/`)**: Evaluates incoming alerts against open incidents, enforces in-group duplicate suppression, and updates Open Knowledge Files (OKF) in GCS using atomic generation preconditions (`if_generation_match`).
  - **Delayed Timer Management (`agent/app/timers/`)**: Self-schedules delayed settle (120s), max-wait (300s), and cool-down (180s) wakeups on a dedicated Cloud Tasks queue (`ata-timer-queue`).
  - **Stale-Wake Handler**: On wakeup at `POST /timer/wake`, compares current timestamp against `okf.settle_deadline` (with a 2.0s tolerance). Stale wakes caused by subsequent alert extensions are ignored as no-ops without minting duplicate tickets.
  - **Multi-Factor Correlation (`agent/app/correlation/`)**: Evaluates candidate alerts using a 4-factor scoring matrix (Topology Distance, Causal Failure Chains, Temporal Proximity, Change Alignment). Calls Gemini strictly for gray-zone alerts (scores 35–65).
  - **Deterministic Specialists (`agent/app/specialists/`)**: Concurrently executes live read-only queries:
    - `CloudRunSpecialist`: Queries container restarts, CPU/memory saturation, request latency percentiles, and application error logs (filtering `/admin/*`). Recursively traces upstream DAG dependencies using the topology graph.
    - `ApigeeSpecialist`: Queries proxy access logs (`ata-apigee-access`), extracts status codes and client IPs, and analyzes SpikeArrest policy limits.
    - `CloudSQLSpecialist`: Queries backend database connection counts (warning threshold 20 / max 25 on `db-f1-micro`), CPU utilization, and PostgreSQL deadlock/lock wait logs.
    - `ChangeSpecialist`: Reads Cloud Run revisions and Apigee deployment history within a 2-hour window prior to alert onset from the deployed `ata-tools` service (`get_change_timeline` over MCP at `TOOLS_URL`, ID-token auth). If that read fails, the ticket says `change timeline unavailable: <reason>` and Change-Induced is `Not assessed`; there is no fallback. See CONFIGURATION.md §1.3.1.
  - **Topology-Guided Analysis**: Ingests system architecture and dependencies from [`topology/TOPOLOGY.md`](topology/TOPOLOGY.md) via the `get_topology` tool guided by the `topology` skill, computing shortest-path DAG distances and downstream blast radius.
  - **Evidence-Based Synthesizer**: Formulates structured 4-bullet executive summaries, calculates blast radius and formulaic confidence, and generates standalone HTML dossiers.

### 3. `ata-tools` (`tools/`)
- **Role**: FastMCP read-only telemetry and topology service.
- **Technology**: FastMCP, Starlette/Uvicorn, Google Cloud SDKs.
- **Responsibilities**:
  - Exposes structured MCP tools at `POST /mcp`: `list_open_alerts`, `get_topology`, `get_topology_graph`, `get_blast_radius`, `get_apigee_metrics`, `get_apigee_logs`, `get_proxy_bundle`, `get_change_timeline`.
  - Called at runtime by the agent's ChangeSpecialist (`get_change_timeline`); requires `roles/run.invoker` on `<prefix>-tools` for `<prefix>-agent-sa`.
  - Ingests and parses system architecture from `topology/TOPOLOGY.md` to construct in-memory DAG representations.
  - Strictly read-only IAM viewer boundary.
  - Optional OIDC ID token validation on tool endpoints (`ENFORCE_AUTH`/`VERIFY_OIDC`; audience and allowed callers derived from config).

### 4. `demo/` Microservices Stack
- **Role**: Realistic multi-tier e-commerce architecture for incident simulation and fault injection.
- **Components**:
  - `demo/ata-inventory-api`: Cloud Run service backed by Cloud SQL PostgreSQL with IAM database authentication (`pg8000`) and mock fallback. Houses `/admin/fault/*` endpoints.
  - `demo/ata-orders-api`: Cloud Run service calling `ata-inventory-api` using ambient Google ID tokens.
  - `demo/apigee/`: Apigee X API proxies (`ata-orders-v1`, `ata-inventory-v1`) with API key verification, SpikeArrest rate limiting, and Cloud Logging.
  - `demo/db/`: Cloud SQL PostgreSQL schema (`schema.sql`) and catalog seed data.
  - **Fault Exclusion Guardrail**: Fault injection endpoints (`/admin/fault/*`) are decoupled from operational telemetry and excluded from Cloud Logging analysis. The agent diagnoses failures strictly from authentic operational symptoms.

---

## 5. Configuration

Configuration is managed strictly through typed Pydantic models (`config.py`) loaded from environment variables and Terraform variables (`terraform.tfvars`), with **zero hardcoding** in application code:

### Key System Settings

| Setting | Environment Variable | Default Value | Description & Rationale |
| :--- | :--- | :--- | :--- |
| **GCP Project** | `PROJECT_ID` | `cloud-assist-fde-2` | Target Google Cloud project identifier. |
| **Resource Prefix** | `NAME_PREFIX` | `ata` | Namespace prefix for isolating resources (`${NAME_PREFIX}-${base_name}`). |
| **Compute Region** | `REGION` | `us-west1` | Target GCP region hosting Cloud Run, Cloud Tasks, and Cloud SQL. |
| **Environment** | `ENVIRONMENT` | `prod` | Runtime environment name read by the service. The deploy scripts do not pass it to Cloud Run, so the default applies. Not the `env` resource label, which comes from the shell `ENVIRONMENT` used by the deploy scripts and the Terraform `environment` variable (live value: `demo`; see [CONFIGURATION.md §7.1](CONFIGURATION.md#71-label-scheme--value-requirements)). |
| **Incidents Bucket** | `INCIDENTS_BUCKET` | `${PROJECT_ID}-${NAME_PREFIX}-incidents` | GCS bucket for persistent Open Knowledge File (OKF) storage. |
| **Incident Store Path** | `INCIDENT_STORE_PATH` | `/knowledge/incident-manager` | Local or mounted directory path for active OKF incident files. |
| **Fallback Store Path**| `FALLBACK_STORE_PATH` | `/tmp/knowledge/incident-manager` | Fallback path when primary mount is unavailable. |
| **Bridge Service URL** | `BRIDGE_URL` | `http://localhost:8080` | On `ata-agent`: URL of `ata-bridge` for instruction endpoints (`/instructions/ticket/*`). On `ata-bridge`: its own `status.url`, an accepted ID-token audience (required unless `IS_LOCAL=true`; see [CONFIGURATION.md §1.1](CONFIGURATION.md#11-bridge-service-ata-bridge)). |
| **Agent Service URL**  | `AGENT_URL` | `http://localhost:8080` | URL of `ata-agent` called by Cloud Tasks at `POST /triage`. |
| **Triage Queue Name**  | `TASKS_QUEUE` | `ata-triage-queue` | Cloud Tasks queue for serialized alert dispatches. |
| **Timer Queue Name**   | `TIMER_QUEUE_NAME` | `ata-timer-queue` | Dedicated Cloud Tasks queue for delayed settle/cool-down timers. |
| **Gemini Reasoning**   | `MODEL_NAME` | `gemini-3.8-flash` | Gemini model used strictly for gray-zone correlation (scores 35–65). |
| **ServiceNow URL**     | `SERVICENOW_INSTANCE_URL` | *None* | **Configuration (NOT a secret)**: Base URL of ServiceNow instance (e.g. `https://<instance>.service-now.com`). |
| **ServiceNow Caller**  | `SERVICENOW_CALLER` | `ATA Integration` | Caller ID populated on created ServiceNow incident records. |
| **Assignment Group**   | `SERVICENOW_ASSIGNMENT_GROUP` | `Cloud SRE Tier-2` | Assignment group for triage routing in ServiceNow. |
| **IAM Database User**  | `ATA_INVENTORY_DB_USER` | `${NAME_PREFIX}-inventory-sa@...` | IAM database user for Cloud SQL IAM authentication. |
| **Database Name**      | `ATA_INVENTORY_DB_NAME` | `inventory` | PostgreSQL database name on Cloud SQL. |
| **DB Connection Name** | `ATA_INVENTORY_INSTANCE_CONNECTION_NAME` | `${PROJECT_ID}:${REGION}:${NAME_PREFIX}-inventory-db` | Cloud SQL instance connection string. |
| **IAM DB Auth Flag**   | `ATA_INVENTORY_DB_IAM_AUTH` | `true` | Enables IAM database authentication via pg8000 connector. |
| **Mock Database Flag** | `ATA_INVENTORY_MOCK_DB` | `false` | Serves the in-memory catalog instead of Cloud SQL. |
| **Local Mode Flag**    | `IS_LOCAL` | `false` | Enables mock storage and local adapters for offline testing. |
| **Auto-Resolve on Recovery** | `AUTO_RESOLVE_ON_RECOVERY` | `false` | When all alerts close and the 180s cool-down passes: `false` leaves the ServiceNow ticket In Progress and adds a work note stating that the alerts closed (with the measured traffic around the close) and that a human must verify the remediation before resolving it. `true` resolves the ticket. See "Cool-down and resolution" under [Sequence Flow](#sequence-flow-mermaid). |

### Secret Manager Secret Names
Sensitive credentials reside exclusively in Google Secret Manager:
- `ata-webhook-token`: HTTP Basic Auth password for Cloud Monitoring alert webhook channel.
- `ata-servicenow-username`: API integration username for ServiceNow Table & Attachment APIs.
- `ata-servicenow-password`: Password for ServiceNow integration user.

> [!NOTE]
> **ServiceNow Instance URL is CONFIG, not a secret.** It is managed as the Terraform variable `servicenow_instance_url` in `terraform.tfvars` and passed as the environment variable `SERVICENOW_INSTANCE_URL` to `ata-bridge`. It is not stored in Secret Manager.

For complete variable lists and schema details, see the [Configuration Guide (`CONFIGURATION.md`)](CONFIGURATION.md).

---

## 6. Customizing the Agent with Skills

The repository includes domain knowledge and diagnostic runbooks located in [`skills/`](skills/):

```
skills/
├── alert-triage/                   # Settle window, stale-wake handling, and correlation scoring
│   └── SKILL.md
├── apigee-troubleshooting/         # Apigee X access log queries, SpikeArrest 429, and target timeouts
│   └── SKILL.md
├── change-correlation/             # Cloud Run revision and Apigee deployment correlation rules
│   └── SKILL.md
├── cloud-run-troubleshooting/      # Container lifecycle, CPU/memory saturation, and latency percentiles
│   └── SKILL.md
├── cloud-sql-troubleshooting/      # PostgreSQL connection pool exhaustion (max 25, warn 20) and locks
│   └── SKILL.md
├── report-style/                   # SRE 4-bullet executive summaries, confidence formulas, HTML dossiers
│   └── SKILL.md
└── topology/                       # Interprets topology/TOPOLOGY.md components and dependency tables
    └── SKILL.md
```

### Skills Directory Overview
- **`skills/alert-triage/SKILL.md`**: Defines incident grouping mechanics, settle deadline extension rules, stale-wake evaluation, and multi-factor scoring thresholds.
- **`skills/apigee-troubleshooting/SKILL.md`**: Documents Apigee access log parsing (`ata-apigee-access`), SpikeArrest policy limits, and gateway timeout patterns.
- **`skills/change-correlation/SKILL.md`**: Outlines revision-to-error timeline alignment rules within the 2-hour window prior to alert onset.
- **`skills/cloud-run-troubleshooting/SKILL.md`**: Details container restart diagnostic queries, memory limits, and upstream DAG inspection.
- **`skills/cloud-sql-troubleshooting/SKILL.md`**: Specifies Cloud SQL connection saturation thresholds (warning at 20 connections on `db-f1-micro`) and deadlock queries.
- **`skills/report-style/SKILL.md`**: Prescribes the structured 4-bullet SRE summary format (Problem, Systems, Observations, Recommended Actions) and evidence-grounded confidence rules.
- **`skills/topology/SKILL.md`**: Guides the parsing and interpretation of [`topology/TOPOLOGY.md`](topology/TOPOLOGY.md) (the single source of truth for system components and dependencies). Specifies how the agent uses the `get_topology` tool to parse the Components table (`| Component | Type | Also known as | Notes |`) and Dependencies table (`| From | To | How |`) to evaluate topological proximity, trace upstream failure paths, and project downstream blast radius.

### Operational Reality & Runtime Architecture
In the current prototype, the agent's diagnostic pipeline is executed via **deterministic Python specialist modules** (`agent/app/specialists/`) calling live GCP APIs, rather than dynamic LLM prompt/tool injection or an ADK `SkillToolset` at runtime:
- The Markdown skill files serve as **human and AI assistant architectural runbooks and design specifications** that establish the diagnostic queries, thresholds, and report formatting implemented in code.
- Gemini is invoked strictly for gray-zone correlation scoring (scores between 35 and 65).
- The active domain skill areas (`cloud-run-diagnostics`, `apigee-telemetry`, `cloud-sql-triage`, `report-style`, `topology`) are recorded in the `InvestigationReport` metadata contract (`skills_loaded`).

---

## 7. Security Model & Production Guardrails

The Alert Triage Agent enforces zero-trust security and defense-in-depth across all tiers:

- **Strictly Read-Only IAM Boundaries**:
  - `ata-agent-sa` and `ata-tools-sa` possess strictly read-only viewer roles (`roles/logging.viewer`, `roles/monitoring.viewer`, `roles/run.viewer`, `roles/cloudsql.viewer`).
  - Neither service account possesses any mutating privileges on production cloud infrastructure. Write permissions are strictly confined to the incident GCS bucket (`roles/storage.objectUser` on `gs://${PROJECT_ID}-${NAME_PREFIX}-incidents`).
- **Zero Secrets in Repository or Code**: All passwords and tokens reside in Google Secret Manager; the ServiceNow instance URL is configuration.
- **Mutual Authentication & OIDC Verification**:
  - Inbound alert webhooks require HTTP Basic Auth validation against Secret Manager credentials.
  - Cloud Tasks invokes `ata-agent` (`POST /triage`, `POST /timer/wake`) using Google-signed OIDC ID tokens validated by `verify_agent_caller`.
  - `ata-agent` calls `ata-bridge` (`POST /instructions/ticket/*`) using Google ID tokens verified against `ata-agent-sa`.
  - Apigee proxies invoke Cloud Run demo services using Google ID tokens.
- **IAM Database Authentication**: `ata-inventory-api` connects to Cloud SQL PostgreSQL using IAM database authentication via `pg8000` (zero static database passwords).
- **Atomic Concurrency Protection**: Open Knowledge File updates in GCS use atomic generation preconditions (`if_generation_match`) with exponential backoff and jitter, preventing race conditions and lock clobbering.
- **Evidence-Bound Synthesis**: Root cause findings cite concrete observed metrics, logs, and deployment revisions. Hardcoded confidence floats and fabricated candidate names are strictly prohibited.
- **Fault Injection Telemetry Exclusion**: All administrative fault endpoints (`/admin/fault/*`) are decoupled from operational telemetry and excluded from Cloud Logging analysis.

---

## 8. Prototype Status & Known Limitations

The Alert Triage Agent has been evaluated against live GCP infrastructure across 5 realistic failure scenarios (**S1 through S5**), as detailed in the [Demonstration Guide (`DEMO.md`)](DEMO.md):

### Scenario Evaluation Scorecard

| Scenario | Title | Target Infrastructure | Expected Incident Groups & Tickets | Root Cause Origin | Result |
| :---: | :--- | :--- | :---: | :--- | :---: |
| **S1** | **Backend 5xx Cascade** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | `ata-inventory-api` | **PASS** |
| **S2** | **Backend Latency** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | `ata-inventory-api` (Upstream origin via DAG) | **PASS** |
| **S3** | **Concurrent Disjoint Incidents** | Apigee X & Cloud Run | 2 Groups, 2 Tickets | `ata-orders-v1` and `ata-inventory-api` (Disjoint groups, multi-factor score < 35) | **PASS** |
| **S4** | **DB Connection Exhaustion** | Cloud SQL (`ata-inventory-db`) | 1 Group, 1 Ticket | `ata-inventory-db` (earliest onset, connection count > 20) | **PASS** |
| **S5** | **Bad Deploy / Config Failure** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | Specific Cloud Run revision (e.g. `ata-inventory-api-00062-6pj`) | **PASS** |

### Known Limitations
1. **Cloud SQL `db-f1-micro` Connection Limits**: The test tier uses `db-f1-micro` with `max_connections = 25`. The alert threshold is calibrated at 20 connections (~80% capacity). Under heavy production traffic, production instances (`db-custom-*`) require recalibration.
2. **Pre-Existing Apigee X Dependency**: Requires an existing Apigee X organization and environment group hostname (`8.233.247.52.nip.io`).
3. **Local Mock Mode vs Live GCP**: While `IS_LOCAL=true` provides in-memory mock adapters for offline unit tests, full end-to-end evaluation requires live GCP credentials and APIs.
4. **Serialized Triage Queue**: `ata-triage-queue` runs with `max_concurrent_dispatches = 1`, so alerts are processed one at a time. This keeps correlation and grouping deterministic and prevents one incident from being split across groups, at the cost of throughput during large alert bursts. Failed dispatches are retried for a bounded window (`triage_queue_max_retry_duration`). The window is measured from each alert's first delivery attempt, so after a long outage the alerts whose first attempt was more than one window before recovery are dropped. Alerts that arrived later are still delivered. See [CONFIGURATION.md §4](CONFIGURATION.md#4-cloud-tasks-queue-configuration).

---

## 9. Full Step-by-Step GCP Deployment Walkthrough

The following walkthrough follows the staged deployment sequence documented in [DEPLOY.md](DEPLOY.md).

### Prerequisites & IAM Requirements

Before deploying, ensure you have:
1. **Google Cloud SDK (`gcloud`)** authenticated:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
2. **Terraform CLI** (`>= 1.5.0`): `terraform version`
3. **Target Project Configuration**:
   ```bash
   export PROJECT_ID="your-target-project-id"
   export REGION="us-west1"
   export NAME_PREFIX="ata"
   export ENVIRONMENT="demo"
   gcloud config set project "${PROJECT_ID}"
   ```
4. **Deployer Permissions**: Your deploying account requires `roles/owner` or `roles/editor` plus `roles/resourcemanager.projectIamAdmin`.

---

### Step 0: Bootstrap GCS Remote State

Bootstrap the Google Cloud Storage backend for Terraform state storage with uniform bucket-level access and versioning:

```bash
cd alert-triage-agent
./scripts/bootstrap-tfstate.sh \
  --project-id "${PROJECT_ID}" \
  --region "${REGION}" \
  --name-prefix "${NAME_PREFIX}" \
  --env "${ENVIRONMENT}"
```

This ensures `gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate` exists with encryption and object versioning enabled.

---

### Step 1: Foundation Module

The foundation module provisions the GCS incidents bucket, creates 6 dedicated service accounts, configures Secret Manager placeholders, and provisions Cloud Tasks queues:

```bash
cd alert-triage-agent/infra/terraform/foundation
terraform init -reconfigure -backend-config=backend.hcl

terraform plan \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}" \
  -var="environment=${ENVIRONMENT}" \
  -out=tfplan-foundation

terraform apply tfplan-foundation
cd ../../..
```

**Resources Created**:
- GCS Bucket: `gs://${PROJECT_ID}-${NAME_PREFIX}-incidents` (versioning enabled).
- Cloud Tasks Queues: `ata-triage-queue` (max concurrent: 1, retried within `triage_queue_max_retry_duration`) and `ata-timer-queue` (max concurrent: 50, retried within `timer_queue_max_retry_duration`).
- Service Accounts: `ata-orders-sa`, `ata-inventory-sa`, `ata-apigee-sa`, `ata-bridge-sa`, `ata-agent-sa`, `ata-tools-sa`.
- Secret Placeholders: `ata-webhook-token`, `ata-servicenow-username`, `ata-servicenow-password`.

---

### Step 2: Database Module

Provisions a Cloud SQL PostgreSQL 15 instance with IAM database authentication enabled:

```bash
cd alert-triage-agent/infra/terraform/database
terraform init -reconfigure -backend-config=backend.hcl

terraform plan \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}" \
  -out=tfplan-database

terraform apply tfplan-database
cd ../../..
```

**Database Initialization**:
Apply the schema and seed mock inventory catalog items:
```bash
gcloud sql connect "${NAME_PREFIX}-inventory-db" --user=postgres --database=inventory < demo/db/schema.sql
```

---

### Step 3: Applications Module

Deploys internal demo microservices on Cloud Run with IAM service account identities:

```bash
cd alert-triage-agent/infra/terraform/applications
terraform init -reconfigure -backend-config=backend.hcl

terraform plan \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}" \
  -out=tfplan-apps

terraform apply tfplan-apps
cd ../../..
```

**Services Deployed**:
- `ata-inventory-api`: Backed by Cloud SQL IAM authentication and mock fallback.
- `ata-orders-api`: Securely invokes `ata-inventory-api` using ambient Google ID tokens.
- Apigee TargetServers: `ata-orders-target` and `ata-inventory-target`.

---

### Step 4: Apigee API Proxies & Products

Package and deploy the Apigee API proxies to environment `prod`, configure developer credentials, and obtain an API consumer key:

```bash
cd alert-triage-agent
./scripts/deploy-apigee-proxies.sh
```

**Outputs**:
- Proxies deployed: `ata-orders-v1` (`/ata/orders/v1`) and `ata-inventory-v1` (`/ata/inventory/v1`).
- Enforces API key verification, SpikeArrest rate limiting, and structured Cloud Logging.
- Consumer API key saved to `.ata-api-key`.

---

### Step 5: Observability Module

Deploys Cloud Monitoring alert policies and links them to the `ata-bridge` notification channel:

```bash
cd alert-triage-agent/infra/terraform/observability
terraform init -reconfigure -backend-config=backend.hcl

terraform plan \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}" \
  -out=tfplan-observability

terraform apply tfplan-observability
cd ../../..
```

**Alert Policies Configured**:
1. `ata-orders-5xx`: Cloud Run `ata-orders-api` 5xx rate > 5% over 60s.
2. `ata-orders-latency`: Cloud Run `ata-orders-api` p99 latency > 2000ms (60s windows, all revisions and response codes merged per service) sustained for the `latency_duration_secs` retest window (default 240s), so a single cold-start burst does not open an alert.
3. `ata-inventory-5xx`: Cloud Run `ata-inventory-api` 5xx rate > 5% over 60s.
4. `ata-inventory-latency`: Cloud Run `ata-inventory-api` p99 latency > 2000ms (60s windows, all revisions and response codes merged per service) sustained for the `latency_duration_secs` retest window (default 240s), so a single cold-start burst does not open an alert.
5. `ata-apigee-orders-5xx`: Apigee proxy `ata-orders-v1` HTTP 500 error count > 1.
6. `ata-apigee-orders-429`: Apigee proxy `ata-orders-v1` HTTP 429 SpikeArrest rate count > 1.
7. `ata-cloudsql-connections-critical`: Cloud SQL `ata-inventory-db` connection count > 20 backends.
8. `ata-cloudsql-cpu-high`: Cloud SQL `ata-inventory-db` CPU utilization > 85%.

Every policy closes an open incident whose condition has received no data for `alert_auto_close_secs` (Terraform variable, default 1800 s, at least 1800: Cloud Monitoring's 30-minute minimum).

---

### Step 6: Triage Tier Services Deployment

Deploy the custom FastMCP tools server, the multi-agent ADK runtime, and the bridge webhook service:

```bash
# 1. Deploy FastMCP tools server
cd alert-triage-agent/tools
./scripts/deploy.sh
cd ../..

# 2. Deploy multi-agent ADK runtime
cd alert-triage-agent/agent
./scripts/deploy.sh
cd ../..

# 3. Deploy bridge webhook service
cd alert-triage-agent/bridge
./scripts/deploy.sh
cd ../..
```

---

### Step 7: Secret Manager Configuration

Store ServiceNow credentials safely in Google Secret Manager without echoing passwords:

```bash
cd alert-triage-agent
./scripts/set-secrets.sh
```

When prompted:
- **ServiceNow Username**: API integration user (`ata-servicenow-username`).
- **ServiceNow Password**: Password for ServiceNow integration user (`ata-servicenow-password`).

---

### Step 8: End-to-End Verification

Verify the deployed infrastructure and services using automated verification scripts:

```bash
# 1. Verify demo microservices and Apigee routing
./scripts/verify-demo-infra.sh

# 2. Verify bridge webhook ingestion and GCS OKF generation
./scripts/verify-stage1.sh

# 3. Run full end-to-end scenario verification harness
./scripts/verify-scenarios.sh
```

---

## 10. Master Automated Deployment Script

Unlike monolithic prototypes that rely on a single script, deployment in the Alert Triage Agent is organized into **tier automation scripts** matching the system architecture:

### 1. Core Triage Vertical Slice (`deploy-stage1.sh`)
Automates the bootstrapping and deployment of the core triage engine:
```bash
./scripts/deploy-stage1.sh
```
This script sequentially:
1. Bootstraps the GCS Terraform remote state bucket (`scripts/bootstrap-tfstate.sh`).
2. Applies the foundation Terraform module (`infra/terraform/foundation`).
3. Builds and deploys the demo inventory API (`demo/ata-inventory-api/scripts/deploy.sh`).
4. Builds and deploys the agent service (`agent/scripts/deploy.sh`).
5. Builds and deploys the bridge service (`bridge/scripts/deploy.sh`).

### 2. Demo Infrastructure & Observability (`deploy-demo-infra.sh`)
Automates the deployment of the remaining microservices, database, and telemetry:
```bash
./scripts/deploy-demo-infra.sh
```
This script sequentially:
1. Applies the database Terraform module (`infra/terraform/database`).
2. Applies the applications Terraform module (`infra/terraform/applications`).
3. Applies the observability Terraform module (`infra/terraform/observability`).
4. Packages and deploys Apigee proxies (`scripts/deploy-apigee-proxies.sh`).

### 3. Full Deployment Execution Sequence
To deploy the entire stack from clean repository state:
```bash
# Set configuration
export PROJECT_ID="your-target-project-id"
export REGION="us-west1"
export NAME_PREFIX="ata"
export ENVIRONMENT="demo"

# Execute staged deployment sequence
./scripts/deploy-stage1.sh
./scripts/deploy-demo-infra.sh
(cd tools && ./scripts/deploy.sh)
./scripts/set-secrets.sh
./scripts/verify-stage1.sh
```

---

## 11. Terraform Remote State & GCS Backend

All four Terraform stacks in `infra/terraform/` define a partial Google Cloud Storage backend:

```hcl
terraform {
  backend "gcs" {}
}
```

- **Bootstrapping**: Initialized via `scripts/bootstrap-tfstate.sh` which provisions `gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate` with uniform bucket-level access, public access prevention, and object versioning.
- **Prefix Isolation**: Each stack isolates its state using distinct GCS prefixes specified via `backend.hcl`:
  - Foundation: `prefix = "${NAME_PREFIX}/foundation"`
  - Database: `prefix = "${NAME_PREFIX}/database"`
  - Applications: `prefix = "${NAME_PREFIX}/applications"`
  - Observability: `prefix = "${NAME_PREFIX}/observability"`
- **CI & Pre-deployment Validation**: Quality gates and local linters use `terraform init -backend=false && terraform validate`, validating syntax and structure without requiring cloud credentials or remote backend access.

---

## 12. Enterprise Organization Policy Considerations

In enterprise Google Cloud organizations, standard Organization Policies may require specific configuration adjustments:

1. **Domain Restricted Sharing (`constraints/iam.allowedPolicyMemberDomains`)**:
   - May prevent binding `allUsers` on public ingress services such as `ata-bridge`.
   - *Remediation*: Deploy an external Google Cloud Load Balancer with Cloud Armor in front of `ata-bridge` and set Cloud Run ingress to `internal-and-cloud-load-balancing`, or request a project-level policy exemption.
2. **Cloud Run Ingress Controls (`constraints/run.allowedIngress`)**:
   - `ata-bridge`: Requires `all` (or `internal-and-cloud-load-balancing`) to receive Cloud Monitoring webhook notifications.
   - `ata-agent`: Requires `internal` (`INGRESS_TRAFFIC_INTERNAL_ONLY`), accessible exclusively by Cloud Tasks and authorized internal service accounts within the project.
   - `ata-tools`: Requires `internal` (`INGRESS_TRAFFIC_INTERNAL_ONLY`), accessible exclusively by `ata-agent`.
3. **Public IP Restriction on Cloud SQL (`constraints/sql.restrictPublicIp`)**:
   - Enterprise policies frequently block public IP assignments on Cloud SQL.
   - The database module supports Private IP with Google Private Service Connect (PSC) or VPC peering, connecting securely via the Cloud SQL Auth Proxy with IAM database authentication.

---

## 13. Local Development & Quality Gates

### Local Environment Setup
Set up a Python virtual environment and install development dependencies:

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install component packages in editable mode with development dependencies
pip install -e "./bridge[dev]" -e "./agent[dev]" -e "./tools[dev]" pytest ruff mypy
```

### Offline & Mock Development Mode
Set `IS_LOCAL=true` to enable offline mock mode:
- `ata-bridge` uses mock webhook verification and records ticket operations locally.
- `ata-agent` reads and writes incident Open Knowledge Files (OKF) to local temporary storage (`/tmp/knowledge/incident-manager`) without requiring GCS bucket credentials.
- `ata-inventory-api` falls back to in-memory catalog data without requiring a live Cloud SQL connection (`ATA_INVENTORY_MOCK_DB=true`).

### Quality Gates & Presubmit Verification
All pull requests must pass the following checks cleanly:

```bash
# 1. Run full test suite using project virtualenv
pytest tests/ -v

# 2. Run core correlation and scenario test suites
pytest tests/test_scenarios_e2e.py
pytest tests/test_correlation_scoring.py
pytest tests/test_agent_grouping_and_timers.py
pytest tests/test_no_fabricated_data.py
pytest tests/test_container_import_layout.py

# 3. Linting and formatting checks
ruff check bridge agent tools
ruff format --check bridge agent tools

# 4. Terraform validation across all modules
for dir in foundation database applications observability; do
  (cd "infra/terraform/$dir" && terraform init -backend=false && terraform validate)
done
```

**CI:** [`.github/workflows/alert-triage-agent-ci.yml`](../.github/workflows/alert-triage-agent-ci.yml) runs on pull requests and pushes to `main` that touch `alert-triage-agent/**`. It has a read-only token, no secrets, and actions pinned to commit SHAs. It runs:
- the full `pytest tests` suite on Linux (bash 5);
- the script tests (`test_bash32_compat.py`, `test_macos_userland.py`, `test_run_all_*.py`, `test_deploy_scripts_env_vars.py`) on macOS with `/bin/bash` 3.2 and the BSD userland;
- ShellCheck on every script (fails on errors; warnings are listed);
- `terraform fmt -check -recursive` and `validate` for each stack (Terraform 1.9.8).

Ruff and mypy are not part of CI.

To run the scripts under bash 3.2 locally, put a bash 3.2 binary first on `PATH` (named `bash`) and set `ATA_TEST_BASH` to it, then run `pytest tests`.

---

## 14. Testing Guide & Post-Deploy Smoke Tests

The repository provides comprehensive automated testing tools across multiple layers:

### 1. Preflight Health Verification
Before triggering scenarios, run `scripts/scenarios/preflight.sh` to validate microservice health, admin endpoints, Apigee ingress, and alert policies:
```bash
./scripts/scenarios/preflight.sh
```

### 2. Failure Scenarios (S1–S5)
The system is evaluated against 5 realistic production failure scenarios:
- **Scenario S1: Backend 5xx Cascade**
  ```bash
  ./scripts/scenarios/s1_backend_5xx.sh
  ```
  Injects 85% HTTP 500 error rate into `ata-inventory-api`. Verifies 1 incident group and 1 ServiceNow ticket with origin `ata-inventory-api`.
- **Scenario S2: Backend Latency Degradation**
  ```bash
  ./scripts/scenarios/s2_backend_latency.sh
  ```
  Injects 6500ms latency delay into `ata-inventory-api`. Verifies 1 incident group and 1 ServiceNow ticket tracing root cause to inventory delay.
- **Scenario S3: Concurrent Disjoint Incidents**
  ```bash
  ./scripts/scenarios/s3_concurrent_disjoint.sh
  ```
  Simultaneously triggers Scenario S1 on `ata-inventory-api` and strict SpikeArrest rate limiting on `ata-orders-v1`. Evaluates multi-factor correlation score (<35 pts) and verifies **exactly 2 distinct incident groups and 2 distinct ServiceNow tickets**.
- **Scenario S4: Database Connection Pool Exhaustion**
  ```bash
  ./scripts/scenarios/s4_db_connection_pool.sh
  ```
  Simulates PostgreSQL connection leak exhausting backends (`>20` connections). Verifies 1 incident group and 1 ServiceNow ticket with origin `ata-inventory-db`.
- **Scenario S5: Bad Deploy / Config Failure**
  ```bash
  ./scripts/scenarios/s5_bad_deploy.sh
  ```
  Deploys a Cloud Run revision with invalid IAM database credentials. Verifies 1 incident group and 1 ServiceNow ticket citing the explicit failing revision ID.

### 3. Master Scenario Runner
Execute scenarios with logging and automated assertions:
```bash
# Run prioritized scenarios (S4, S1, S3, S5)
./scripts/run-all-scenarios.sh --priority

# Run full scenario suite (S1 through S5)
./scripts/run-all-scenarios.sh --all

# Run a single scenario
./scripts/run-all-scenarios.sh --scenario s3

# Dry-run validation mode (no network modification)
./scripts/run-all-scenarios.sh --dry-run
```

### 4. Resetting Between Scenarios
Reset injected faults, clear database sleep connections, restore Apigee proxies, wait for open alerts to close, then, at the same time, for a 3 min quiet period (Apigee's 1-3 min metric lag; restarts if an alert reopens) and for every incident group to reach a terminal state (`resolved`/`archived`/`degraded`, or `recovered` with `recovery_note_posted_at` set, so the agent's recovery note is posted first), and move active incidents to `archive/` once both are done (about 3 min after the last close, not 6). If a group is still active when `--timeout` (30 min; S4's reset can take up to ~27 min) expires, the reset fails and lists it; nothing is archived. For a group that will never become terminal, `--ignore-group <file>` leaves only that group out of the wait (risky: it is archived as it is). Admin calls use a fresh ID token each and fail the reset on any non-2xx response. Every request to the demo services (fault reset, warm-up `GET /health`, Apigee health, admin probes) runs before the alert wait and is timed; one slower than 2000 ms, with HTTP 5xx or without a response (e.g. a cold start) makes the wait watch for the alert it can open for `--alert-lag` (10 min) after it, so such an alert is waited for (closed, incident group terminal) and archived inside the reset instead of opening after it (stray ticket INC0010327). With the latency policies' old 60s retest window the alert still gets a ticket; only the Terraform `latency_duration_secs` change stops that. After `verify-demo-infra.sh` reported a slow request, run the reset with `--alert-window-from-start`. For S4, run the reset right after the scenario: the leaked connections keep `ata-cloudsql-connections-critical` open until the reset releases them:
```bash
# Standard reset (waits for open ata-* alerts to close, then for terminal incident groups and the 3m quiet period together).
# Fails if Cloud Monitoring cannot be queried. The alert check uses an ADC token by default
# (run: gcloud auth application-default login; or --monitoring-token-source gcloud).
# On macOS CERTIFICATE_VERIFY_FAILED: export SSL_CERT_FILE=/etc/ssl/cert.pem
./scripts/reset-scenarios.sh

# Quick dev reset (bypasses the alert, incident group and quiet period waits;
# risky: can archive a group whose recovery note is still pending)
./scripts/reset-scenarios.sh --no-wait

# A group that will never become terminal (e.g. stuck 'investigating'): skip only that group;
# the alert wait, the quiet period and the wait for every other group still run (risky)
./scripts/reset-scenarios.sh --ignore-group incident-<group-id>.md

# Right after verify-demo-infra.sh listed a slow request (cold start): also watch for its alert
./scripts/reset-scenarios.sh --alert-window-from-start
```

---

## 15. Teardown & Clean Up

To avoid ongoing GCP charges, destroy infrastructure in reverse order:

```bash
# 1. Reset all active scenario faults and clean storage
./scripts/reset-scenarios.sh

# 2. Teardown Observability alert policies
cd infra/terraform/observability
terraform destroy -auto-approve -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="name_prefix=${NAME_PREFIX}"
cd ../../..

# 3. Teardown Applications stack
cd infra/terraform/applications
terraform destroy -auto-approve -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="name_prefix=${NAME_PREFIX}"
cd ../../..

# 4. Teardown Database stack
cd infra/terraform/database
terraform destroy -auto-approve -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="name_prefix=${NAME_PREFIX}"
cd ../../..

# 5. Delete Cloud Run triage services
gcloud run services delete "${NAME_PREFIX}-bridge" --region="${REGION}" --project="${PROJECT_ID}" --quiet || true
gcloud run services delete "${NAME_PREFIX}-agent" --region="${REGION}" --project="${PROJECT_ID}" --quiet || true
gcloud run services delete "${NAME_PREFIX}-tools" --region="${REGION}" --project="${PROJECT_ID}" --quiet || true

# 6. Teardown Foundation stack (Cloud Tasks queues, Service Accounts, Secrets)
cd infra/terraform/foundation
terraform destroy -auto-approve -var="project_id=${PROJECT_ID}" -var="region=${REGION}" -var="name_prefix=${NAME_PREFIX}" -var="environment=${ENVIRONMENT}"
cd ../../..

# 7. Remove Cloud Storage Buckets
gcloud storage rm --recursive "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/**" || true
gcloud storage rm "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents" || true
gcloud storage rm --recursive "gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate/**" || true
gcloud storage rm "gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate" || true
```

---

## 16. Repository Layout

```
alert-triage-agent/
├── agent/                         # Autonomous SRE diagnostic and triage engine
│   ├── app/                       # Grouping, timers, correlation, specialists, synthesizer
│   │   ├── contracts/             # Pydantic data contracts (alert, incident, report)
│   │   ├── correlation/           # Multi-factor correlation scoring engine & DAG traversal
│   │   ├── grouping/              # GroupingManager, OKF store, and duplicate suppression
│   │   ├── specialists/           # Deterministic specialists (Cloud Run, Apigee, Cloud SQL, Change)
│   │   ├── timers/                # Cloud Tasks delayed timer scheduler (settle, max-wait, cool)
│   │   ├── bridge_client.py       # Client for Bridge Instruction API (/instructions/ticket/*)
│   │   ├── config.py              # Agent typed configuration (AgentConfig)
│   │   └── main.py                # FastAPI endpoints (/health, /triage, /timer/wake)
│   ├── Dockerfile                 # Container build specification for ata-agent
│   └── scripts/deploy.sh          # Cloud Run deployment script for ata-agent
├── bridge/                        # Stateless webhook ingestion & ServiceNow adapter
│   ├── app/                       # FastAPI application, auth, dispatcher, adapters
│   │   ├── adapters/              # ServiceNow Table & Attachment API adapter
│   │   ├── contracts/             # AlertEvent and instruction request contracts
│   │   ├── core/                  # TaskDispatcher for Cloud Tasks enqueue
│   │   ├── templates/             # HTML report rendering templates
│   │   ├── auth.py                # HTTP Basic Auth & Google OIDC verification
│   │   ├── config.py              # Bridge typed configuration (BridgeConfig)
│   │   └── main.py                # Webhook (/webhook/monitoring) & Instruction endpoints
│   ├── Dockerfile                 # Container build specification for ata-bridge
│   └── scripts/deploy.sh          # Cloud Run deployment script for ata-bridge
├── demo/                          # Realistic e-commerce microservices & fault injection
│   ├── apigee/                    # Apigee X API proxies (ata-orders-v1, ata-inventory-v1)
│   ├── ata-inventory-api/         # Cloud Run inventory service (Cloud SQL IAM DB auth)
│   ├── ata-orders-api/            # Cloud Run orders service (calls inventory via Google ID token)
│   └── db/schema.sql              # Cloud SQL PostgreSQL schema & catalog seed data
├── infra/terraform/               # Infrastructure as Code (Terraform)
│   ├── applications/              # Cloud Run demo microservices & Apigee TargetServers
│   ├── database/                  # Cloud SQL PostgreSQL 15 instance with IAM DB auth
│   ├── foundation/                # GCS incidents bucket, Cloud Tasks queues, IAM, Secrets
│   └── observability/             # Cloud Monitoring alert policies & notification channels
├── scripts/                       # Deployment, verification, and scenario scripts
│   ├── scenarios/                 # Realistic failure scenario scripts (S1 through S5)
│   │   ├── common.sh              # Shared logging, token resolution, and utility functions
│   │   ├── open_alerts.py         # Read-only Cloud Monitoring OPEN-alert query used by reset (CA bundle resolution)
│   │   ├── preflight.sh           # Pre-scenario health & endpoint verification
│   │   ├── s1_backend_5xx.sh      # Scenario 1: Backend 500 error spike
│   │   ├── s2_backend_latency.sh  # Scenario 2: Backend latency degradation
│   │   ├── s3_concurrent_disjoint.sh # Scenario 3: Concurrent disjoint incidents
│   │   ├── s4_db_connection_pool.sh  # Scenario 4: Database connection pool exhaustion
│   │   └── s5_bad_deploy.sh       # Scenario 5: Bad deploy / config failure
│   ├── bootstrap-tfstate.sh       # Bootstraps GCS remote state bucket
│   ├── deploy-apigee-proxies.sh   # Deploys Apigee X proxies, products, and developer app
│   ├── deploy-demo-infra.sh       # Staged deployer for database, apps, observability, Apigee
│   ├── deploy-stage1.sh           # Core vertical slice deployment (tfstate, foundation, apps)
│   ├── reset-scenarios.sh         # Clears faults, waits for alerts/quiet period, archives incident files
│   ├── run-all-scenarios.sh       # Master scenario driver (--all, --priority, --scenario)
│   ├── set-secrets.sh             # Interactive Secret Manager configuration for ServiceNow
│   ├── verify-demo-infra.sh       # Verifies demo microservices and Apigee routing
│   ├── verify-scenarios.sh        # Automated scenario verification test harness
│   └── verify-stage1.sh           # Verifies webhook ingestion, agent triage, and OKF generation
├── skills/                        # SRE diagnostic runbooks and architectural specifications
│   ├── alert-triage/              # Incident grouping, settle deadlines, and stale-wake rules
│   ├── apigee-troubleshooting/    # Apigee X proxy diagnostics and SpikeArrest patterns
│   ├── change-correlation/        # Cloud Run revision and deployment correlation rules
│   ├── cloud-run-troubleshooting/ # Container lifecycle, CPU/memory saturation, latency metrics
│   ├── cloud-sql-troubleshooting/ # Database connection exhaustion and lock contention
│   ├── report-style/              # SRE 4-bullet executive summaries and HTML dossier styling
│   └── topology/                  # Component catalog and dependency table parser guidance
├── tests/                         # Automated pytest test suites (30+ test modules)
├── tools/                         # FastMCP read-only telemetry and topology server
│   ├── app/                       # MCP server, tool definitions, bundle parser
│   ├── Dockerfile                 # Container build specification for ata-tools
│   └── scripts/deploy.sh          # Cloud Run deployment script for ata-tools
├── topology/                      # Topology DAG definitions and dependency relationships
│   ├── TOPOLOGY.md                # Single source of truth for components and dependencies
│   └── topology_graph.py          # In-memory DAG parser and blast radius calculator
├── AGENTS.md                      # Developer and AI assistant architecture reference
├── CONFIGURATION.md               # Complete configuration, environment variables, secrets reference
├── DEMO.md                        # Operator demonstration guide for scenarios S1 through S5
├── DEPLOY.md                      # Step-by-step production deployment guide
└── README.md                      # Primary project documentation
```

### In-Depth Documentation Links
- [Production Deployment Guide (`DEPLOY.md`)](DEPLOY.md)
- [Operator & Scenario Demonstration Guide (`DEMO.md`)](DEMO.md)
- [Configuration & Environment Reference (`CONFIGURATION.md`)](CONFIGURATION.md)
- [Developer & AI Assistant Guide (`AGENTS.md`)](AGENTS.md)
