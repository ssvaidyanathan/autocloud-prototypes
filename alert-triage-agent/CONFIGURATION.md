# Alert Triage Agent: Configuration & Environment Reference

This document provides a comprehensive reference for all environment variables, Secret Manager secrets, IAM permissions, Cloud Tasks queue configurations, agent-owned grouping and timer parameters, and Apigee credentials used across the **Alert Triage Agent (ATA)** system.

---

## 1. Environment Variables by Component

### 1.1 Bridge Service (`ata-bridge`)

The bridge serves as a thin ingestion and integration layer: it validates Cloud Monitoring alert webhooks with HTTP Basic Auth, enqueues raw alert payloads directly into Cloud Tasks, and exposes authenticated ServiceNow instruction endpoints (`/instructions/ticket/*`).

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | `cloud-assist-fde-2` | Google Cloud project ID. |
| `NAME_PREFIX` | `ata` | Resource namespace prefix. |
| `REGION` | `us-west1` | GCP region for Cloud Tasks and Cloud Run. |
| `ENVIRONMENT` | `prod` | Deployment environment name. |
| `AGENT_URL` | `http://localhost:8080` | URL of the `ata-agent` Cloud Run service. |
| `TASKS_QUEUE` | `ata-triage-queue` | Cloud Tasks queue for serialized alert dispatches. |
| `TASKS_LOCATION` | `us-west1` | GCP region where the Cloud Tasks queue is hosted. |
| `TASKS_INVOKER_SA` | `ata-bridge-sa@...` | Service account used by Cloud Tasks to generate OIDC tokens. |
| `WEBHOOK_TOKEN` | *Loaded from Secret Manager* | Webhook authentication password for Cloud Monitoring HTTP Basic Auth notification channel (`webhook_basicauth`). |
| `SERVICENOW_INSTANCE_URL` | Configured via `servicenow_instance_url` or env | Target ServiceNow instance base URL (e.g. `https://<instance>.service-now.com`). Documented as CONFIG, not a secret. |
| `SERVICENOW_USERNAME` | *Loaded from Secret Manager* | API integration user for ServiceNow Table API. |
| `SERVICENOW_PASSWORD` | *Loaded from Secret Manager* | Password for ServiceNow integration user. |
| `SERVICENOW_CALLER` | `ATA Integration` | Caller ID / name populated on incident records. |
| `SERVICENOW_ASSIGNMENT_GROUP` | `Cloud SRE Tier-2` | Assignment group for triage routing. |
| `IS_LOCAL` | `false` | When `true`, enables mock storage and local adapters. |
| `ADAPTER_TIMEOUT_SECONDS` | `30.0` | HTTP timeout for outbound ServiceNow API calls. |

---

### 1.2 Multi-Agent ADK Runtime (`ata-agent`)

The agent owns all incident grouping, atomic Open Knowledge File (OKF) storage with GCS preconditions, delayed timer scheduling via Cloud Tasks, multi-specialist root cause investigations, and ServiceNow instruction execution.

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | `cloud-assist-fde-2` | Google Cloud project ID. |
| `NAME_PREFIX` | `ata` | Resource namespace prefix. |
| `REGION` | `us-west1` | GCP deployment region. |
| `INCIDENTS_BUCKET` | `cloud-assist-fde-2-ata-incidents` | Primary GCS incident storage bucket. |
| `INCIDENT_STORE_PATH` | `/knowledge/incident-manager` | Local or mounted filesystem path for incident files. |
| `FALLBACK_STORE_PATH` | `/tmp/knowledge/incident-manager` | Fallback path when primary mount is read-only or absent. |
| `BRIDGE_URL` | `http://localhost:8080` | URL of `ata-bridge` for instruction endpoints (`/instructions/ticket/*`). |
| `MODEL_NAME` | `gemini-3.8-flash` | Gemini model used for investigation synthesis and gray-zone correlation. |
| `TIMER_QUEUE` | `ata-timer-queue` | Dedicated Cloud Tasks queue for delayed timer wakes. |
| `TIMER_QUEUE_LOCATION` | `us-west1` | GCP region where the timer queue is hosted. |
| `SERVICE_ACCOUNT_EMAIL`| `ata-agent-sa@...` | Service account used by the agent to schedule delayed tasks with OIDC tokens. |
| `IS_LOCAL` | `false` | Enables local offline mock mode. |

---

### 1.3 FastMCP Tools Server (`ata-tools`)

The FastMCP server exposes read-only tools to the ADK agent for topology, Apigee metrics, proxy bundles, and logs.

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8080` | HTTP server listening port. |
| `PROJECT_ID` | `cloud-assist-fde-2` | Google Cloud project ID. |
| `APIGEE_ORG` | `cloud-assist-fde-2` | Apigee organization identifier. |
| `REGION` | `us-west1` | Target GCP region. |
| `ENFORCE_AUTH` | `false` | When `true`, enforces OIDC token verification on tool endpoints. |
| `VERIFY_OIDC` | `false` | Validates Google OIDC ID token signature and audience. |
| `EXPECTED_AUDIENCE` | `None` | Cloud Run audience URL for incoming OIDC tokens. |
| `ALLOWED_SERVICE_ACCOUNTS` | `ata-agent-sa@...` | Comma-separated list of caller service accounts allowed. |

---

### 1.4 Demo Applications

Configuration for demo workloads (`ata-inventory-api` and `ata-orders-api`).

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8080` | HTTP listening port. |
| `PROJECT_ID` | `cloud-assist-fde-2` | Google Cloud project ID. |
| `REGION` | `us-west1` | GCP deployment region. |
| `DB_USER` | `ata-inventory-sa@...` | IAM database user for Cloud SQL IAM authentication. |
| `DB_NAME` | `inventory` | PostgreSQL database name. |
| `INSTANCE_CONNECTION_NAME` | `cloud-assist-fde-2:us-west1:ata-inventory-db` | Cloud SQL instance connection name. |
| `DB_IAM_AUTH` | `true` | Enables IAM database authentication via pg8000 connector. |
| `MOCK_DB` | `false` | Fallback to in-memory catalog when database is unreachable. |
| `INVENTORY_SERVICE_URL` | *Cloud Run URL* | Internal Cloud Run URL of inventory service called by orders API. |

---

## 2. Secret Manager Secrets Reference

Sensitive credentials are stored in Google Secret Manager under the prefix `ata-`:

| Secret Name | Purpose | Authorized Accessors | Management Script |
| :--- | :--- | :--- | :--- |
| `ata-webhook-token` | HTTP Basic Auth password for Cloud Monitoring alert webhook channel. | `ata-bridge-sa` | Terraform (`random_password.webhook_token`) |
| `ata-servicenow-username` | Integration account username for ServiceNow Table & Attachment APIs. | `ata-bridge-sa` | `scripts/set-secrets.sh` |
| `ata-servicenow-password` | Integration account password for ServiceNow. | `ata-bridge-sa` | `scripts/set-secrets.sh` |

> [!NOTE]
> **ServiceNow Instance URL is CONFIG, not a secret.** It is managed as the Terraform variable `servicenow_instance_url` in `terraform.tfvars` and passed as the environment variable `SERVICENOW_INSTANCE_URL` to `ata-bridge`. It is not stored in Secret Manager.

*Security Guardrail:* Secrets are resolved dynamically at runtime by `ata-bridge`. When running in local development mode (`IS_LOCAL=true`), mock values are used to prevent external credential leakage.

---

## 3. IAM Roles & Least Privilege Matrix

The system assigns least-privilege IAM roles to 6 dedicated service accounts:

```
+--------------------+---------------------------------------+-----------------------------------------------+
| Service Account    | Assigned IAM Roles                    | Security Boundary                             |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-orders-sa      | roles/run.invoker (on inventory API)  | Cannot access Cloud SQL directly.             |
|                    | roles/logging.logWriter               |                                               |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-inventory-sa   | roles/cloudsql.client                 | IAM database user in Cloud SQL; no DDL or     |
|                    | roles/cloudsql.instanceUser           | admin permissions.                            |
|                    | roles/logging.logWriter               |                                               |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-apigee-sa      | roles/run.invoker (on orders API)     | Attached to Apigee environment prod; cannot   |
|                    | roles/logging.logWriter               | access database or bridge.                    |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-bridge-sa      | roles/cloudtasks.enqueuer             | Sole component with write access to external  |
|                    | roles/run.invoker (on agent service)  | ticketing systems.                            |
|                    | roles/secretmanager.secretAccessor    |                                               |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-agent-sa       | roles/storage.objectUser (incidents)  | STRICTLY READ-ONLY ON PRODUCTION INFRA:       |
|                    | roles/logging.viewer                  | Zero write privileges on GCP production       |
|                    | roles/monitoring.viewer               | resources. Write access strictly confined to  |
|                    | roles/run.viewer                      | the GCS incident store.                       |
|                    | roles/cloudsql.viewer                 |                                               |
|                    | roles/run.invoker (on ata-tools)      |                                               |
+--------------------+---------------------------------------+-----------------------------------------------+
| ata-tools-sa       | roles/monitoring.viewer               | STRICTLY READ-ONLY:                           |
|                    | roles/logging.viewer                  | Read-only access to Apigee bundles, metrics,  |
|                    | roles/apigee.readOnlyAdmin            | and topology graphs.                          |
+--------------------+---------------------------------------+-----------------------------------------------+
```

### 3.1. Read-Only Ticket Verification Endpoint (`GET /instructions/ticket/view/{group_id or sys_id}`)

To verify ServiceNow ticket lifecycle state without distributing or exposing ServiceNow credentials:
- **Endpoint:** `GET /instructions/ticket/view/{group_id or sys_id}`
- **Authentication:** Google-signed OIDC ID token (`Bearer <id_token>`) from an allowlisted caller identity:
  - `ata-agent-sa@{project_id}.iam.gserviceaccount.com`
  - Configurable verifier SA specified by the `VERIFIER_SA` environment variable on `ata-bridge`.
- **Response Fields:** Ticket metadata only:
  - `number`: ServiceNow incident number (e.g. `INC0010482`)
  - `state`: Current lifecycle state (`1` New, `2` In Progress, `6` Resolved, `7` Closed)
  - `short_description`: Triage incident summary
  - `caller`: Caller identity / display name
  - `work_notes`: Journal entries with text and timestamps
  - `attachments`: Attached artifact metadata (file names and sizes in bytes)
- **Security Rule (No Direct ServiceNow Credentials):**
  - Incident verification **NEVER** uses ServiceNow credentials directly.
  - Verifiers, downstream operators, and automated audit tools never receive access to `ata-servicenow-username` or `ata-servicenow-password`.
  - All verification queries flow strictly through the authenticated bridge verification endpoint, which acts as a secure boundary and returns sanitized ticket fields.

---

## 4. Cloud Tasks Queue Configuration

Alert processing and delayed timer evaluations utilize dedicated Cloud Tasks queues to guarantee isolation and prevent head-of-line blocking:

```hcl
# Ingestion dispatch queue (serialized triage delivery)
resource "google_cloud_tasks_queue" "triage_queue" {
  name     = "ata-triage-queue"
  location = "us-west1"

  rate_limits {
    max_concurrent_dispatches = 10
    max_dispatches_per_second = 10
  }

  retry_config {
    max_attempts = 5
    min_backoff  = "5s"
    max_backoff  = "60s"
  }
}

# Dedicated delayed timer queue (settle, max-wait, and cooling wakes)
resource "google_cloud_tasks_queue" "timer_queue" {
  name     = "ata-timer-queue"
  location = "us-west1"

  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 50
  }

  retry_config {
    max_attempts = 5
    min_backoff  = "2s"
    max_backoff  = "30s"
  }
}
```

- **Dedicated `ata-timer-queue`:** Prevents delayed timers from being head-of-line blocked behind high-volume alert webhook bursts.
- **OIDC Dispatch Authentication:** Cloud Tasks generates Google-signed ID tokens for both raw alert dispatch (`/triage`) and timer wakeups (`/timer/wake`), validated via `verify_agent_caller`.

---

## 5. Agent-Owned Grouping, Timers & Correlation Parameters

The agent owns all incident grouping, suppression, delayed timer evaluation, and ServiceNow ticket lifecycle:

### 5.1 Windowing & Deadline Parameters (Stale-Wake Pattern)

```
First Alert Arrives
      |
      +---> 120s Quiet Settle Window (Rescheduled when a related alert attaches)
      |
      +---> 300s Max Wait Timeout (Hard upper bound from group creation)
      |
      +---> 180s Cool-Down Window (Started after all member alerts close)
```

- **Settle Duration (`120s`):** When an alert arrives, the agent schedules a delayed task for `now + 120s`. If subsequent related alerts arrive, `settle_deadline` in the OKF is extended and a new delayed task is scheduled. When the earlier task wakes at `/timer/wake`, the agent compares `now` against `okf.settle_deadline`; if stale (`now < settle_deadline - 2.0s`), it is ignored as a no-op without creating a ticket.
- **Max Wait Duration (`300s`):** Hard cap from initial group creation forcing investigation if continuous cascading alerts keep extending the settle window.
- **Cool-Down Linger (`180s`):** When all member alerts close, the group transitions to `cooling_down` and a 180s timer is scheduled. If an alert re-triggers within 180s, the group reactivates to `ticketed` and appends a work note. If 180s elapses uninterrupted, the ticket is resolved in ServiceNow and the OKF is archived.

### 5.2 Multi-Factor Correlation Scoring Matrix

Incoming alerts are scored against existing incident groups using 4 weighted factors:

$$\text{Total Score} = \text{Topology (35)} + \text{Causal (30)} + \text{Temporal (20)} + \text{Change (15)}$$

| Factor | Weight | Evaluation Criteria | Scoring Rules |
| :--- | :---: | :--- | :--- |
| **Topology Distance** | **35 pts** | Shortest path distance in directed DAG. | • 0 hops (same resource): 35 pts<br/>• 1 hop (direct neighbor): 28 pts<br/>• 2 hops: 17.5 pts<br/>• $\ge$ 3 hops / disjoint: 0 pts |
| **Causal Failure Chain** | **30 pts** | Compatibility of failure types. | • Direct propagation (DB $\rightarrow$ 5xx $\rightarrow$ 504): 30 pts<br/>• Plausible (general 5xx): 15 pts<br/>• Incompatible (SpikeArrest 429 vs DB): 0 pts |
| **Temporal Proximity** | **20 pts** | Delta between alert and group anchor time. | • Delta $\le$ 2 min: 20 pts<br/>• Linear decay up to 10 min: $20 \times (1 - \frac{\Delta}{10})$ pts<br/>• Delta $>$ 10 min: 0 pts |
| **Change Alignment** | **15 pts** | Proximity to a recent deployment or revision. | • Deployment within 30 min of onset: 15 pts<br/>• No recent deployment: 0 pts |

### 5.3 Correlation Decision Thresholds

- **Score $\ge$ 65 (Auto-Attach):** High confidence that the alert belongs to the active incident. The alert is attached to the existing group and its blast radius is updated.
- **Score $<$ 35 (Auto-New Group):** Strong evidence that the alert is an independent incident (e.g. Scenario S3 concurrent disjoint). A new incident group and ServiceNow ticket are initialized.
- **35 $\le$ Score $<$ 65 (Gray Zone):** Ambiguous boundary. The structured evidence is passed to Gemini (`gemini-3.8-flash`) to make the final determination with written engineering rationale.

---

## 6. Apigee Product & Developer App Credentials

Client traffic reaching the demo microservices routes through Apigee X environment `prod`:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Ingress Hostname** | `8.233.247.52.nip.io` | External DNS envgroup hostname. |
| **API Product** | `ata-product` | Bundles `ata-orders-v1` and `ata-inventory-v1`. |
| **Developer Account** | `ata-dev@example.com` | Registered Apigee developer identity. |
| **Developer App** | `ata-demo-app` | Registered application holding API credentials. |
| **Credential Key File** | `.ata-api-key` | Stores consumer API key (gitignored). |
| **Authentication Header** | `x-api-key: <KEY>` | Header enforced on protected proxy endpoints. |
| **Query Parameter Auth** | `?apikey=<KEY>` | Query parameter fallback for API key validation. |

---

## 7. Labels & Metadata

To ensure operational clarity, multi-tenant safety, and deterministic triage routing across Google Cloud Platform and ServiceNow, all Alert Triage Agent (ATA) resources conform to a unified label and metadata scheme.

### 7.1 Label Scheme & Value Requirements

All GCP resource labels adhere to Google Cloud's labeling rules: lowercase alphanumeric characters, underscores (`_`), and dashes (`-`), with a maximum length of 63 characters (`^[a-z0-9_-]{1,63}$`).

| Label Key | Allowed Values / Pattern | Purpose |
| :--- | :--- | :--- |
| `app` | `alert-triage-agent` | Namespace identifier identifying all ATA project resources. |
| `component` | Component ID matching `TOPOLOGY.md` (`ata-orders-api`, `ata-inventory-api`, `ata-inventory-db`, `ata-orders-v1`, `ata-inventory-v1`) or system parts (`ata-agent`, `ata-bridge`, `ata-tools`). | Topology mapping identifier. |
| `role` | `demo-workload` \| `triage-system` | Distinguishes monitored workloads from triage control-plane components. |
| `tier` | `edge` \| `service` \| `data` \| `platform` | Architectural tier in the topology hierarchy. |
| `env` | `demo` \| `staging` \| `prod` | Deployment environment (default: `demo`). |
| `managed-by` | `terraform` \| `script` | Identifies provisioning authority. |
| `owner` | Configurable variable (default: `sre-prototypes`) | Owning engineering team or proto lead. |

### 7.2 Alert Policy User Labels

All 8 Cloud Monitoring alert policies defined in `infra/terraform/observability/main.tf` attach `user_labels`:

| Policy Name | Component | Tier | Signal | Severity |
| :--- | :--- | :--- | :--- | :--- |
| `ata-orders-5xx` | `ata-orders-api` | `service` | `5xx` | `p1` |
| `ata-orders-latency` | `ata-orders-api` | `service` | `latency` | `p2` |
| `ata-inventory-5xx` | `ata-inventory-api` | `service` | `5xx` | `p1` |
| `ata-inventory-latency` | `ata-inventory-api` | `service` | `latency` | `p2` |
| `ata-cloudsql-cpu-high` | `ata-inventory-db` | `data` | `cpu` | `p2` |
| `ata-cloudsql-connections-critical` | `ata-inventory-db` | `data` | `connections` | `p1` |
| `ata-apigee-orders-429` | `ata-orders-v1` | `edge` | `rate-limit` | `p2` |
| `ata-apigee-orders-5xx` | `ata-orders-v1` | `edge` | `5xx` | `p1` |

### 7.3 Resource Labels & Unsupported Resource Types

1. **Labeled Resources:**
   - **Cloud Run Services:** `ata-orders-api`, `ata-inventory-api`, `ata-agent`, `ata-bridge`, `ata-tools` via Terraform `labels` or `gcloud run deploy --labels`.
   - **Cloud SQL Instances:** `ata-inventory-db` via Terraform `user_labels`.
   - **Cloud Storage Buckets:** State bucket `gs://cloud-assist-fde-2-ata-tfstate/` and incidents bucket `gs://cloud-assist-fde-2-ata-incidents/`.
   - **Artifact Registry & Secrets:** `docker_repo` and Secret Manager secrets (`ata-webhook-token`, placeholder secrets).
2. **Unsupported Resource Types:**
   - **Apigee X Proxies:** Apigee API proxies do not support GCP resource labels. Instead, metadata is recorded in the `<Description>` tag of the proxy XML definition (`apiproxy/<proxy-name>.xml`), formatted as `[app=alert-triage-agent component=... tier=edge role=demo-workload env=demo managed-by=script owner=sre-prototypes]`.
   - **Cloud Tasks Queues:** Cloud Tasks queues do not support GCP resource labels.

### 7.4 Agent Ingestion & Investigation Usage

- **`component` Precedence & Validation:**
  When Cloud Monitoring triggers an incident webhook, the incident payload carries `incident.policy_user_labels['component']`. The bridge remains lean and does not validate components or read topology, passing `policy_user_labels` through to the agent unchanged. In the agent, `AlertEvent.from_monitoring_payload` validates the `component` label against `TOPOLOGY.md`. If valid, it takes precedence as the component identifier. If unrecognized, the agent logs a WARNING and falls back to canonicalizing `resource.labels` with an audit log (never rejecting the alert). If `policy_user_labels` does not define `component`, the agent falls back to canonicalizing `resource.labels`.
- **`signal` Hint for Correlation & Root Cause:**
  The `signal` label (`5xx`, `latency`, `rate-limit`, `connections`, `cpu`) serves as a directional hypothesis for correlation scoring and root cause analysis. For instance, a `rate-limit` signal at the edge (`ata-orders-v1`) indicates client throttling at the edge and prevents false causal linking to backend database connection pool exhaustion. Crucially, the `signal` label is treated as supporting evidence only—it never supersedes empirical telemetry inspection.
- **Incident Storage & Reporting:**
  Labels are persisted in the Open Knowledge File (OKF) incident record and rendered in both the triage summary report and the bridge HTML notification table (`Labels` column with badges).

