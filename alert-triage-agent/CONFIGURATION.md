# Alert Triage Agent: Configuration & Environment Reference

Resource names are shown with their prefix as `<prefix>` or `${NAME_PREFIX}`; with the live stack's prefix `ata` they read `ata-*` (there is no default prefix). Where a table names `ata-bridge`, `ata-agent` or `ata-tools`, read `<prefix>-bridge`, `<prefix>-agent`, `<prefix>-tools`.

**Defaults.** The bridge, agent and tools have no project, region or prefix defaults: `PROJECT_ID`, `REGION` and `NAME_PREFIX` are required (`PROJECT_ID`/`REGION` only unless `IS_LOCAL=true`), startup fails without them, and the stack's resource names derive from them. The deploy scripts always pass them.

This document provides a comprehensive reference for all environment variables, Secret Manager secrets, IAM permissions, Cloud Tasks queue configurations, agent-owned grouping and timer parameters, and Apigee credentials used across the **Alert Triage Agent (ATA)** system.

---

## 1. Environment Variables by Component

### 1.1 Bridge Service (`ata-bridge`)

The bridge serves as a thin ingestion and integration layer: it validates Cloud Monitoring alert webhooks with HTTP Basic Auth, enqueues raw alert payloads directly into Cloud Tasks, and exposes authenticated ServiceNow instruction endpoints (`/instructions/ticket/*`).

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | *None (required unless `IS_LOCAL=true`)* | Google Cloud project ID. Set by `deploy.sh`. |
| `PROJECT_NUMBER` | *None (required unless `IS_LOCAL=true`)* | Numeric GCP project number. The bridge accepts `https://<NAME_PREFIX>-bridge-<PROJECT_NUMBER>.<REGION>.run.app` as an ID-token audience on `/instructions/*`. Without it a non-local bridge fails at startup, and auth returns HTTP 500. `bridge/scripts/deploy.sh` sets it from `gcloud projects describe` unless it is already in the environment. |
| `BRIDGE_URL` | *None (required unless `IS_LOCAL=true`)* | The bridge's own Cloud Run URL, i.e. the service's `status.url` (e.g. `https://<NAME_PREFIX>-bridge-<hash>-<region code>.a.run.app`), as an https URL without a path. `ata-agent` mints its ID tokens for the bridge URL it calls (the agent's `BRIDGE_URL`), so the bridge accepts this value as an ID-token audience on `/instructions/*`, next to the `PROJECT_NUMBER` URL. (The request's Host-derived base URL is not accepted.) Without it a non-local bridge fails at startup, and auth returns HTTP 500. `bridge/scripts/deploy.sh` sets it from the service's `status.url`; on a first deploy it starts with the deterministic URL and sets `status.url` right after the deploy. |
| `NAME_PREFIX` | *None (required)* | Stack prefix (`^[a-z][a-z0-9]{1,9}$`, the Terraform rule). Set by `deploy.sh`. |
| `REGION` | *None (required unless `IS_LOCAL=true`)* | GCP region for Cloud Tasks and Cloud Run. Set by `deploy.sh`. |
| `ENVIRONMENT` | `prod` | Runtime environment name read by the service. The deploy scripts do not set it, so the default applies. Not the `env` resource label (see [§7.1](#71-label-scheme--value-requirements)). |
| `AGENT_URL` | `http://localhost:8080` | URL of the `<prefix>-agent` Cloud Run service. Set by `deploy.sh`. |
| `TASKS_QUEUE` | `${NAME_PREFIX}-triage-queue` | Cloud Tasks queue for serialized alert dispatches. |
| `TASKS_LOCATION` | `${REGION}` | GCP region where the Cloud Tasks queue is hosted. |
| `TASKS_INVOKER_SA` | `${NAME_PREFIX}-bridge-sa@${PROJECT_ID}.iam.gserviceaccount.com` | Service account in the OIDC token of the triage tasks. |
| `WEBHOOK_TOKEN` | *Loaded from Secret Manager* | Webhook authentication password for Cloud Monitoring HTTP Basic Auth notification channel (`webhook_basicauth`). |
| `SERVICENOW_INSTANCE_URL` | *Set by `deploy.sh`* (required) | Target ServiceNow instance base URL (e.g. `https://<instance>.service-now.com`). Configuration, not a secret. `bridge/scripts/deploy.sh` takes it from the environment, else keeps the value already on the service (both logged); it fails if neither exists. `deploy-stage1.sh` requires it and checks that it starts with `https://`. |
| `SERVICENOW_USERNAME` | *Loaded from Secret Manager* | API integration user for ServiceNow Table API. |
| `SERVICENOW_PASSWORD` | *Loaded from Secret Manager* | Password for ServiceNow integration user. |
| `SERVICENOW_CALLER` | `ATA Integration` | Caller ID / name populated on incident records. |
| `SERVICENOW_ASSIGNMENT_GROUP` | `Cloud SRE Tier-2` | Assignment group for triage routing. |
| `IS_LOCAL` | `false` | When `true`, enables mock storage and local adapters. `bridge/scripts/deploy.sh` always sets `false`. |
| `VERIFIER_SA` | *None* | Comma-separated emails allowed to call `/instructions/*` besides `<NAME_PREFIX>-agent-sa` and `<NAME_PREFIX>-bridge-sa` (e.g. operators using the read-only view endpoint, §3.1). No other caller is built in. `bridge/scripts/deploy.sh` passes it only when it is exported, so a redeploy without it keeps the value on the service; `VERIFIER_SA=none` removes it. Entries must be email addresses (lowercased; any other entry stops the bridge at startup). |
| `ADAPTER_TIMEOUT_SECONDS` | `30.0` | HTTP timeout for outbound ServiceNow API calls. |

---

### 1.2 Multi-Agent ADK Runtime (`ata-agent`)

The agent owns all incident grouping, atomic Open Knowledge File (OKF) storage with GCS preconditions, delayed timer scheduling via Cloud Tasks, multi-specialist root cause investigations, and ServiceNow instruction execution.

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PROJECT_ID` | *None (required unless `IS_LOCAL=true`)* | Google Cloud project ID. No default: startup fails without it. |
| `NAME_PREFIX` | *None (required)* | Stack prefix; must match `^[a-z][a-z0-9]{1,9}$` (the Terraform `name_prefix` rule). Derives every stack resource name the agent uses (timer queue, SA, incidents bucket, Apigee access log `<NAME_PREFIX>-apigee-access`, Cloud Tasks task ids, the `{prefix}` names in `topology/TOPOLOGY.md`) and the alert scope (see below). |
| `REGION` | *None (required unless `IS_LOCAL=true`)* | GCP deployment region (Cloud Run console links, `gcloud` commands in tickets). |
| `INCIDENTS_BUCKET` | `<PROJECT_ID>-<NAME_PREFIX>-incidents` | Primary GCS incident storage bucket. |
| `INCIDENT_STORE_PATH` | `/knowledge/incident-manager` | Local or mounted filesystem path for incident files. |
| `FALLBACK_STORE_PATH` | `/tmp/knowledge/incident-manager` | Fallback path when primary mount is read-only or absent. |
| `BRIDGE_URL` | `http://localhost:8080` | URL of `ata-bridge` for instruction endpoints (`/instructions/ticket/*`), and the audience of the agent's ID tokens. `agent/scripts/deploy.sh` sets it to the bridge's `status.url`, or to the bridge's deterministic URL `https://<NAME_PREFIX>-bridge-<project number>.<REGION>.run.app` when the bridge is not deployed yet. |
| `MODEL_NAME` | `gemini-3.8-flash` | Vertex AI Gemini model id for gray-zone correlation (scores 35-65), called with `google-genai` (`genai.Client(vertexai=True, project=PROJECT_ID, location=GEMINI_LOCATION)`). Validated as a model id. The agent SA needs `roles/aiplatform.user` and the project needs `aiplatform.googleapis.com`. When the call fails (package missing, permission denied, model not found, empty or invalid answer) the agent logs a WARNING, a deterministic heuristic decides, and the decision log, the group's `correlation.gray_zone_source` / `gray_zone_fallback_reason` and the ticket say "deterministic fallback (Gemini unavailable: <reason>)". |
| `GEMINI_LOCATION` | `global` | Vertex AI location for `MODEL_NAME`: `global` or a region such as `us-central1`. |
| `TIMER_QUEUE_NAME` | `<NAME_PREFIX>-timer-queue` | Dedicated Cloud Tasks queue for delayed timer wakes. `agent/scripts/deploy.sh` sets `${NAME_PREFIX}-timer-queue` (the foundation Terraform queue). |
| `LOCATION` | `<REGION>` | GCP region where the timer queue is hosted. `agent/scripts/deploy.sh` sets `${REGION}`. |
| `SERVICE_ACCOUNT_EMAIL`| `<NAME_PREFIX>-agent-sa@<PROJECT_ID>.iam.gserviceaccount.com` | Service account used by the agent to schedule delayed tasks with OIDC tokens. `agent/scripts/deploy.sh` sets it explicitly. |
| `IS_LOCAL` | `false` | Enables local offline mock mode. |
| `ORIGIN_ONSET_TOLERANCE_SECONDS` | `300` | Origin selection: an upstream service explains a downstream service only when its onset is at most this many seconds after the downstream onset. A downstream service that degraded earlier than that is not attributed to the upstream service, and a recent deploy on it stays implicated. |
| `AUTO_RESOLVE_ON_RECOVERY` | `false` | Sets what happens when every alert in a ticketed group has closed and the 180s cool-down passes with no recurrence. `false`: the ticket is not resolved. The agent posts one work note saying the alerts closed (with the measured traffic around the close) and that a human must verify the remediation, the ServiceNow state stays In Progress, and the group moves to the terminal `recovered` state. `true`: the agent calls `POST /instructions/ticket/resolve` and the group becomes `resolved`. `agent/scripts/deploy.sh` passes it only when it is exported (`true` or `false`, anything else stops the deploy), so a redeploy without it keeps the value on the service. See §5.1. |
| `RECOVERY_TRAFFIC_LOOKBACK_SECONDS` | `300` | Recovery note: request_count of the group's Cloud Run services is read from this many seconds before the last alert closed until the note is written, to state whether the close coincided with traffic stopping. |
| `ROLLBACK_TRAFFIC_LOOKBACK_MINUTES` | `30` | Change-induced rollback: the previous serving revision is the single revision that received requests (Cloud Monitoring request_count by revision) in this many minutes before the new revision was created. With split or no traffic no rollback target is named, and the reason is stated. |
| `LATE_NEW_SIGNAL_MAX_ONSET_GAP_SECONDS` | `600` | An alert of a signal type a ticketed group does not have yet (e.g. 5xx joining a latency group) opens a new group when its onset is more than this many seconds after the group's first alert. Same-service latency/5xx reach the 65-pt attach threshold on topology + causal score alone, so the temporal score never separates them. Live S5 (one incident) had latency -> 5xx 413 s apart; keep the value above that. Alerts that do join a ticketed group update its title (count, problem), raise its priority if more severe (never lowered) and post a work note. |
| `APIGEE_ACCESS_LOG_MAX_ENTRIES` | `5000` | Apigee access-log entries read per investigation, oldest first (live S3 had 634 in the window). When the cap is hit, the report says so (finding, evidence item and observation), no proxy revision switch is evaluated, and log-derived counts are stated as "at least N". |
| `LOGGING_READ_PAGE_SIZE` | `1000` | Entries per Cloud Logging `entries.list` page (API maximum 1000). Each page is one call against the project's shared "Read requests per minute" quota; the API default of 50 made a 5000-entry access-log read cost up to 100 calls. |
| `GCP_READ_RETRY_ATTEMPTS` | `5` | Total attempts for a telemetry read rejected with a quota error (HTTP 429 / `RESOURCE_EXHAUSTED`). After the last attempt the source is reported as unavailable ("Evidence degraded: ..."); a missing change source gives "Change-Induced: Not assessed (change detection incomplete: ...; change attribution not assessed)", never "No". |
| `GCP_READ_RETRY_BASE_SECONDS` | `2.0` | Full-jitter exponential backoff base: before retry n the wait is uniform in [0, min(MAX, BASE * 2^(n-1))] seconds. |
| `GCP_READ_RETRY_MAX_SECONDS` | `30.0` | Upper bound of one backoff wait. |
| `LOW_TRAFFIC_RPM` | `5` | Latency reports: when the origin's Cloud Run `request_count` over the investigation window (alert start −15 min to +15 min or now) averages below this many requests/min (measured total ÷ window), the problem statement, root cause and an observation state the measured volume (e.g. for INC0010327, 32 requests in its window: "low traffic: 32 requests in 17.2 min (1.9 requests/min) on ata-inventory-api, so the latency may come from a few slow requests (such as a cold start) rather than a service-wide degradation") instead of calling it a service degradation. Applies to the origin's own dossier only, also when the alert is on a caller. Nothing is suppressed, and no statement is made without a measured total and window. Live 2026-10-27: INC0010327 at 1.9 requests/min, S2's origin about 35 requests/min (627 requests in about 18 min). Must be > 0. |
| `INVESTIGATION_STALE_SECONDS` | `3600` | Stuck investigations: a group in `investigating` with no ServiceNow ticket whose last settle/max-wait wake attempt (`investigation_started_at` in the incident file) is more than this many seconds old is treated as stuck, because its wake exhausted the timer queue retries (see §4). Every retry of a failing wake re-stamps the attempt, so the value must exceed `timer_queue_max_backoff` plus the agent request timeout (Cloud Run default 300s); a test checks the default against the Terraform default. Detected by the next alert ingest (any alert) and by an `investigation_watchdog` wake scheduled when the group enters `investigating`. Recovery logs `STUCK_INVESTIGATION` at ERROR, appends a record to `investigation_recoveries`, re-enqueues a settle wake (fresh retry window) and a watchdog, and the eventual ticket carries an "Investigation recovery:" observation. Must be > 0. |
| `INVESTIGATION_MAX_RECOVERIES` | `3` | Recovery attempts per stuck group. An attempt whose settle wake cannot be scheduled (e.g. a Cloud Tasks error) still counts: it is logged at ERROR, recorded with `settle_wake_scheduled: false`, and the group stays `investigating` and detectable as stuck. On the watchdog path the wake fails (5xx) so Cloud Tasks retries it; on the ingest path the alert is still ingested and the next ingest or watchdog retries the recovery. Attempts are at least `INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS` apart, so a burst of alerts or watchdog retries during an outage uses one attempt; the group is degraded no sooner than that interval after its last attempt. When one more attempt would be needed, the group is marked `degraded` (`degraded_cause: stuck_investigation`; the `STUCK_INVESTIGATION` ERROR log lists its alert ids and policies, and `degraded_reason` states the last wake attempt and the attempts used) and is no longer attachable, so new alerts open a new group. The next group that opens for the same scope (its alert's resource or blast radius shares a resource with the degraded group's resource, origin, blast radius or alerts) carries the degraded group in `degraded_predecessors`, and its ticket states, as an observation, the degraded group's id, its alert ids and policies, and "investigation stuck; N recovery attempts failed". Each degraded group is carried into one new group (`carried_to_group_id`, logged as `DEGRADED_ALERTS_CARRIED` at WARNING). **If no new group ever opens for that scope, those alerts are not ticketed, and the `STUCK_INVESTIGATION` ERROR log (action `degraded`) is their only record.** Must be >= 1. |
| `INVESTIGATION_RECOVERY_MIN_INTERVAL_SECONDS` | `300` | Minimum spacing between two recovery attempts on one stuck group, measured from the last recorded attempt (`investigation_recoveries[-1].at`). A trigger sooner than this starts no attempt, uses none of `INVESTIGATION_MAX_RECOVERIES`, and does not degrade the group; it logs `RECOVERY_THROTTLED` at WARNING with the next allowed time. On alert ingest the WARNING is logged once per group per interval (`investigation_recovery_throttle_warned_at`) and the alert is ingested normally (the group stays attachable). On an `investigation_watchdog` wake (typically the Cloud Tasks retry of a watchdog whose recovery failed to schedule) the watchdog re-arms itself for the next allowed time and returns `ignored` (`recovery_throttled`); if that re-arm fails, the wake fails (5xx) and Cloud Tasks retries it. So the time from the first attempt to degradation is at least (`INVESTIGATION_MAX_RECOVERIES`) x this value. Must be > 0. |
| `TOOLS_URL` | *none (required when not local)* | URL of the deployed `<prefix>-tools` service. The ChangeSpecialist calls its `get_change_timeline` tool at `<TOOLS_URL>/mcp` with an ID token whose audience is this URL (see §1.3.1). Must be `https://<host>` with no path. A non-local agent without it fails at startup. `deploy.sh` sets it from `gcloud run services describe <prefix>-tools --format='value(status.url)'`, else `https://<prefix>-tools-<PROJECT_NUMBER>.<REGION>.run.app`. |
| `TOOLS_TIMEOUT_SECONDS` | `60` | Per-attempt HTTP timeout of an ata-tools call (0 < value <= 300). The timeline lists every Cloud Run service's revisions and the Apigee deployments. |
| `TOOLS_RETRY_ATTEMPTS` | `3` | Total attempts for an ata-tools call on a connection error, timeout or HTTP 429/5xx (1-5). Other 4xx (e.g. 403 when `roles/run.invoker` is missing) are not retried. |
| `TOOLS_RETRY_BASE_SECONDS` | `1.0` | Backoff before retry n: `BASE * 2^(n-1)` seconds, capped at 10 s. |

**Alert scope (stack isolation).** The bridge, agent and tools accept an alert only when its policy display name (or a resource-derived component: `service_name`, `proxy_name`, `database_id`, the `component` label) starts with `<NAME_PREFIX>-`, **and**, when the policy has a `stack` user label, that label equals `NAME_PREFIX`. A mismatching `stack` label is rejected and logged at WARNING with both values. The shared `app=alert-triage-agent` label does not put an alert in scope, because every stack sets it. Policies without a `stack` label are accepted on the name check alone; that is the transition until Terraform adds `stack=<prefix>` to every policy.

**Topology.** `topology/TOPOLOGY.md` writes stack names as `{prefix}-<name>`. The agent and tools substitute `NAME_PREFIX` when they load it, and fail at startup if `NAME_PREFIX` is unset or invalid, or if a `{...}` placeholder remains.

---

### 1.3 FastMCP Tools Server (`ata-tools`)

The FastMCP server exposes read-only tools for topology, Apigee metrics, proxy bundles, logs and change history. At runtime the agent calls one of them, `get_change_timeline`, over MCP (§1.3.1); the agent image does not contain the `tools` package.

`tools/scripts/deploy.sh` sets `APIGEE_ENV`, `ENFORCE_AUTH=true`, `VERIFY_OIDC=true`, `PROJECT_NUMBER` and `EXPECTED_AUDIENCE` (the agent's `TOOLS_URL`) ([DEPLOY.md §4.5](DEPLOY.md#45-environment-variables-set-by-the-deploy-scripts)).

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8080` | HTTP server listening port. |
| `PROJECT_ID` | *None (required unless `IS_LOCAL=true`)* | Google Cloud project ID. |
| `APIGEE_ORG` | *None (required for Apigee reads)* | Apigee organization whose deployments `get_change_timeline` reads. No fallback to `PROJECT_ID`: `deploy.sh` sets it explicitly. Unset: the Apigee deployment history is returned as a failed source ("APIGEE_ORG not set in ata-tools"). |
| `APIGEE_ENV` | *None (required for Apigee reads)* | Apigee environment the proxies are deployed to (e.g. `prod`). Unset: the Apigee deployment history is returned as a failed source ("APIGEE_ENV not set in ata-tools"; the ticket says so); it is never guessed. |
| `NAME_PREFIX` | *None (required)* | Stack prefix (`^[a-z][a-z0-9]{1,9}$`): alert scope, Apigee access log `<NAME_PREFIX>-apigee-access`, topology `{prefix}` names. `get_change_timeline` reports only `<NAME_PREFIX>-*` Apigee proxies; the default caller SA and audience derive from it. |
| `PROJECT_NUMBER` | *none* | Builds the accepted audience `https://<prefix>-tools-<PROJECT_NUMBER>.<REGION>.run.app`. |
| `REGION` | *None (required unless `IS_LOCAL=true`)* | Target GCP region. |
| `ENFORCE_AUTH` | `false` | When `true`, enforces OIDC token verification on tool endpoints. |
| `VERIFY_OIDC` | `false` | Validates Google OIDC ID token signature and audience. |
| `EXPECTED_AUDIENCE` | `None` | Comma-separated accepted ID-token audiences; set it to the URL the agent calls (`TOOLS_URL`). The deterministic URL from `PROJECT_NUMBER` is always accepted too. With `ENFORCE_AUTH` and `VERIFY_OIDC` on and no audience, startup fails; a token that cannot be verified is rejected (fail closed). |
| `ALLOWED_SERVICE_ACCOUNTS` | `<prefix>-agent-sa@<PROJECT_ID>.iam.gserviceaccount.com` | Comma-separated caller service accounts allowed (others get 403). |

#### 1.3.1 Change timeline call path (ChangeSpecialist -> ata-tools)

- **Call:** `POST <TOOLS_URL>/mcp`, one JSON-RPC `tools/call` of `get_change_timeline(window_hours=2, project_id, region)`. ata-tools runs stateless with JSON responses, so there is no `initialize` handshake or session. The client is plain httpx (`agent/app/tools/tools_client.py`).
- **Auth:** an ID token of the agent SA from the metadata server, audience = `TOOLS_URL`. Cloud Run checks `roles/run.invoker` on `<prefix>-tools` for `<prefix>-agent-sa`; with `ENFORCE_AUTH=true VERIFY_OIDC=true`, the ata-tools middleware also checks the audience and the caller SA.
- **Result:** Cloud Run revisions (new revisions, traffic shifts, service updates) and the Apigee deployment history of the stack's proxies: every deployed revision with its deploy time, and the deployments started in the window. Each source that fails is an `error` entry.
- **Failure semantics (no fallback):** if ata-tools is unreachable, times out, returns a non-2xx status, an MCP or tool error, or a malformed result, the agent logs an ERROR, and the change dossier states `change timeline unavailable: <reason>. Apigee proxy deployment history and Cloud Run revision history were not assessed; change attribution not assessed.` Change-Induced becomes `Not assessed (change detection incomplete: change timeline unavailable: <reason>; ...)`, never `No`. An `error` entry for one source is stated the same way for that source (for example `change timeline unavailable: Apigee deployment history source failed (APIGEE_ENV not set in ata-tools)`), and the other source is still used. The agent makes no Cloud Run-only query that could present partial history as complete.

---

### 1.4 Demo Applications

Configuration for demo workloads (`ata-inventory-api` and `ata-orders-api`).

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `PORT` | `8080` | HTTP listening port. |
| `PROJECT_ID` | *Set by Terraform* | Google Cloud project ID. |
| `REGION` | *Set by Terraform* | GCP deployment region. |
| `ATA_INVENTORY_DB_USER` | *none (required unless mock)* | IAM database user for Cloud SQL IAM authentication (`<prefix>-inventory-sa@<project>.iam`). |
| `ATA_INVENTORY_DB_NAME` | `inventory` | PostgreSQL database name. |
| `ATA_INVENTORY_INSTANCE_CONNECTION_NAME` | *none (required unless mock)* | Cloud SQL instance connection name (`<project>:<region>:<prefix>-inventory-db`). |
| `ATA_INVENTORY_DB_IAM_AUTH` | `true` | Enables IAM database authentication via pg8000 connector. |
| `ATA_INVENTORY_MOCK_DB` | `false` | Serve the in-memory catalog instead of Cloud SQL (`true`/`false`; other values fail at start). |

The inventory API reads only `ATA_INVENTORY_*` names; the legacy `DB_USER` / `MOCK_DB` aliases are not read.

Demo script settings (scripts/scenarios/common.sh):

| Variable | Default Value | Description |
| :--- | :--- | :--- |
| `ATA_DEMO_MAX_INSTANCES` | `1` | Max Cloud Run instances of the demo services (service and revision level). Keep equal to Terraform `demo_max_instance_count`. |
| `ATA_INVENTORY_RESTOCK_BASELINE` | `item-101=1000,...,item-105=1000` | Stock that `reset-scenarios.sh` sets via `POST /admin/restock` before each scenario (`<id>=<stock>`, stock >= 1). |
| `ATA_INVENTORY_PIN_FILE` | `scripts/scenarios/inventory-pin.<prefix>.env` (TODO(scen-lane): per-prefix name) | The inventory pin (revision, digest, previous pin). Change it with `scripts/repin-inventory.sh`. |
| `ATA_ARTIFACT_REGISTRY_HOST` | `<REGION>-docker.pkg.dev` | Artifact Registry host of the `<prefix>-docker` repository. |
| `INVENTORY_SERVICE_URL` | *Cloud Run URL* | Internal Cloud Run URL of inventory service called by orders API. |

---

## 2. Secret Manager Secrets Reference

Sensitive credentials are stored in Google Secret Manager under the stack's prefix (`ata-` on the live stack). All three are created by the foundation Terraform root; the ServiceNow ones with a `placeholder` version that `scripts/set-secrets.sh` replaces before the bridge is deployed ([DEPLOY.md Step 4](DEPLOY.md#step-5-secrets)). Each is readable only by the stack's own bridge SA (secret-level `roles/secretmanager.secretAccessor`):

| Secret Name | Purpose | Authorized Accessors | Management Script |
| :--- | :--- | :--- | :--- |
| `ata-webhook-token` | HTTP Basic Auth password for Cloud Monitoring alert webhook channel. | `ata-bridge-sa` | Terraform (`random_password.webhook_token`) |
| `ata-servicenow-username` | Integration account username for ServiceNow Table & Attachment APIs. | `ata-bridge-sa` | `scripts/set-secrets.sh` |
| `ata-servicenow-password` | Integration account password for ServiceNow. | `ata-bridge-sa` | `scripts/set-secrets.sh` |

> [!NOTE]
> **ServiceNow Instance URL is CONFIG, not a secret.** It is passed to `<prefix>-bridge` as the environment variable `SERVICENOW_INSTANCE_URL` by `bridge/scripts/deploy.sh` (§1.1). It is not stored in Secret Manager.

*Security Guardrail:* Secrets are resolved dynamically at runtime by `ata-bridge`. When running in local development mode (`IS_LOCAL=true`), mock values are used to prevent external credential leakage.

---

## 3. IAM Roles & Least Privilege Matrix

Each stack has **six** service accounts, all created by the foundation Terraform root. There is no tools SA: `<prefix>-tools` runs as `<prefix>-agent-sa`. Every grant is either scoped to the stack's own resources (a queue, a secret, a bucket, a repository, a Cloud Run service), or is a read-only, inference-only or log-writing project role. Two stacks in one project therefore cannot enqueue into, read, write or act as each other's resources.

| Service account | Roles | Where granted | Boundary |
| :--- | :--- | :--- | :--- |
| `<prefix>-bridge-sa` | `roles/cloudtasks.enqueuer` on `<prefix>-triage-queue` | foundation | The only component with ServiceNow credentials. |
| | `roles/secretmanager.secretAccessor` on `<prefix>-webhook-token`, `<prefix>-servicenow-username`, `<prefix>-servicenow-password` | foundation | |
| | `roles/storage.objectUser` on `gs://<project>-<prefix>-incidents` | foundation | |
| | `roles/iam.serviceAccountUser` on itself (the OIDC identity of its triage tasks) | foundation | |
| | `roles/run.invoker` on `<prefix>-agent` | triage-iam | |
| `<prefix>-agent-sa` (also runs `<prefix>-tools`) | `roles/logging.viewer`, `roles/monitoring.viewer`, `roles/run.viewer`, `roles/cloudsql.viewer` (project) | foundation | Read-only on the monitored infrastructure. |
| | custom role `<prefix>ApigeeDeployReader` (project): `apigee.deployments.list` and `apigee.proxyrevisions.get` only, for the change timeline and proxy bundles. Not `roles/apigee.readOnlyAdmin`, which reads every developer app key in the org. Teardown soft-deletes the role; a redeploy within 7 days undeletes it, later set `apigee_reader_role_suffix` | foundation | |
| | `roles/aiplatform.user` (project): Gemini gray-zone scoring | foundation | |
| | `roles/storage.objectUser` on `gs://<project>-<prefix>-incidents` | foundation | Its only data writes. |
| | `roles/cloudtasks.enqueuer` on `<prefix>-timer-queue` | foundation | |
| | `roles/iam.serviceAccountUser` on itself (the OIDC identity of its timer tasks) | foundation | |
| | `roles/run.invoker` on `<prefix>-agent` (timer wakes), `<prefix>-bridge` (instructions) and `<prefix>-tools` (MCP) | triage-iam | |
| `<prefix>-inventory-sa` | `roles/cloudsql.client`, `roles/cloudsql.instanceUser`, `roles/logging.logWriter` (project) | foundation | Cloud SQL IAM database user `<prefix>-inventory-sa@<project>.iam` with `SELECT, UPDATE` on `inventory_items` only (`demo/db/schema.sql`). |
| `<prefix>-orders-sa` | `roles/logging.logWriter` (project) | foundation | Cannot reach Cloud SQL. |
| | `roles/run.invoker` on `<prefix>-inventory-api` | applications | |
| `<prefix>-apigee-sa` | `roles/logging.logWriter` (project): the `ML-CloudLogging` access log | foundation | Identity of the Apigee proxies. |
| | `roles/run.invoker` on `<prefix>-orders-api` and `<prefix>-inventory-api` | applications | |
| `<prefix>-build-sa` | `roles/storage.objectAdmin` on `gs://<project>-<prefix>-build`, `roles/artifactregistry.writer` on `<prefix>-docker`, `roles/logging.logWriter` (project) | foundation | Cloud Build identity: every build (`scripts/build-image.sh`, the deploy scripts) runs as this SA, with source and logs in `gs://<project>-<prefix>-build`. |

The bridge's public invoker (`allUsers`, for the Cloud Monitoring webhook) is set by `bridge/scripts/deploy.sh` (`--allow-unauthenticated`), not by Terraform. Principals in the foundation variable `deployer_members` get `roles/iam.serviceAccountUser` on `<prefix>-apigee-sa` and `<prefix>-build-sa`, so that they can deploy without being project Owner.

### 3.1. Read-Only Ticket Verification Endpoint (`GET /instructions/ticket/view/{group_id or sys_id}`)

To verify ServiceNow ticket lifecycle state without distributing or exposing ServiceNow credentials:
- **Endpoint:** `GET /instructions/ticket/view/{group_id or sys_id}`
- **Authentication:** Google-signed OIDC ID token (`Bearer <id_token>`) from an allowlisted caller identity:
  - `<prefix>-agent-sa@<project>.iam.gserviceaccount.com` and `<prefix>-bridge-sa@<project>.iam.gserviceaccount.com`
  - The comma-separated emails in the `VERIFIER_SA` environment variable on `<prefix>-bridge` (§1.1). No operator account is built in.
- **Audience:** the token's audience must be the bridge URL (its `status.url` or `https://<prefix>-bridge-<PROJECT_NUMBER>.<REGION>.run.app`). A user token from `gcloud auth print-identity-token` carries gcloud's own audience and is rejected; mint one for a `VERIFIER_SA` service account with `--impersonate-service-account=<sa> --audiences=<bridge URL> --include-email` ([DEPLOY.md §4.6](DEPLOY.md#46-read-only-ticket-verification-endpoint)).
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

Alert processing and delayed timer evaluations utilize dedicated Cloud Tasks queues to guarantee isolation and prevent head-of-line blocking. Both queues are defined in `infra/terraform/foundation/main.tf`:

```hcl
resource "google_cloud_tasks_queue" "triage_queue" {
  name     = "${var.name_prefix}-triage-queue"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 1
    max_dispatches_per_second = 10
  }

  # Cloud Tasks stops retrying only once BOTH max_attempts and max_retry_duration
  # are satisfied, so a small max_attempts makes the duration the binding limit.
  retry_config {
    max_attempts       = var.triage_queue_max_attempts
    max_retry_duration = var.triage_queue_max_retry_duration
    min_backoff        = var.triage_queue_min_backoff
    max_backoff        = var.triage_queue_max_backoff
  }
}

resource "google_cloud_tasks_queue" "timer_queue" {
  name     = "${var.name_prefix}-timer-queue"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 50
  }

  # Same rule as the triage queue: retrying stops only once BOTH max_attempts and
  # max_retry_duration are satisfied, so the duration is the binding limit.
  retry_config {
    max_attempts       = var.timer_queue_max_attempts
    max_retry_duration = var.timer_queue_max_retry_duration
    min_backoff        = var.timer_queue_min_backoff
    max_backoff        = var.timer_queue_max_backoff
  }
}
```

The retry values of both queues come from variables in `infra/terraform/foundation/variables.tf` (`triage_queue_*` and `timer_queue_*`). The defaults below are copied from that file, which is the source of truth. The rest of the documentation refers to the retry windows as `triage_queue_max_retry_duration` and `timer_queue_max_retry_duration` and does not repeat their values. For each queue, Terraform validation rejects `max_attempts` below 1, malformed durations, and a `max_backoff` smaller than `min_backoff`. It also enforces these ranges, which follow the Cloud Tasks limits:
- **Retry duration:** greater than `0s` and at most `"2678400s"`, the 31-day [maximum task retention](https://cloud.google.com/tasks/docs/quotas).
- **Both backoffs:** from `"1s"` to `"2592000s"`. The upper bound is the 30-day [maximum schedule time](https://cloud.google.com/tasks/docs/quotas), since no retry can be scheduled further ahead. The lower bound is there because the [`RetryConfig` reference](https://cloud.google.com/tasks/docs/reference/rpc/google.cloud.tasks.v2#retryconfig) says backoffs are truncated to whole seconds, so `"0s"` and sub-second values are rejected.

The `max_backoff >= min_backoff` check runs only when both values are well formed, so a malformed value (for example `"5m"`) reports only its own format error.

| Variable | Default | Effect |
| :--- | :--- | :--- |
| `triage_queue_max_retry_duration` | `"86400s"` (24h) | Retry window: how long a failed task keeps being retried, measured from its first attempt. Must be greater than `0s`. With the default, alerts survive outages of up to about 24h, for example overnight. That is about 296 attempts per task if each failure returns quickly (fewer if requests hang until the dispatch deadline). |
| `triage_queue_min_backoff` | `"1s"` | First wait between retries. The wait doubles on each retry, up to `max_backoff` (16 doublings by default). |
| `triage_queue_max_backoff` | `"300s"` (5 min) | Longest wait between retries. |
| `triage_queue_max_attempts` | `5` | Minimum attempts per task. Cloud Tasks stops retrying only when **both** this and the retry duration are satisfied. It is kept at a small positive value so that the retry window sets the limit. `-1` is rejected by validation: the [Cloud Tasks `RetryConfig` reference](https://cloud.google.com/tasks/docs/reference/rest/v2/RetryConfig) describes `-1` only together with a `maxRetryDuration` of `0` (retry until the task retention limit), and [Configuring queues](https://cloud.google.com/tasks/docs/configuring-queues#retry) states that the retry duration still applies when attempts are set to `-1`. It would add nothing here. |
| `timer_queue_max_retry_duration` | `"86400s"` (24h) | Retry window for a failed settle, max-wait, or cool-down wake, measured from its first attempt. Must be greater than `0s`. With the default, a wake that fails during an outage of up to about 24h is still delivered, so a group still gets its ticket. That is about 1445 attempts per wake if each failure returns quickly (fewer if requests hang until the dispatch deadline). |
| `timer_queue_min_backoff` | `"1s"` | First wait between retries. The wait doubles on each retry, up to `max_backoff` (16 doublings by default). |
| `timer_queue_max_backoff` | `"60s"` (1 min) | Longest wait between retries, so a wake runs at most about a minute after the agent recovers. |
| `timer_queue_max_attempts` | `5` | Minimum attempts per wake. Same rule as `triage_queue_max_attempts`: kept small and positive so that the retry window sets the limit, and `-1` is rejected. |

- **Serialized `ata-triage-queue` (`max_concurrent_dispatches = 1`):** Alerts are processed one at a time, which keeps correlation and grouping deterministic and prevents one incident from being split across groups.
- **Explicit triage retry window:** A failed `/triage` dispatch is retried until `triage_queue_max_retry_duration` has passed since its first attempt. With fast failures, the backoff runs 1s, 2s, 4s … 256s and then holds at 300s. The window is measured per task, so an alert is lost only if its first delivery attempt was more than one window before the agent recovered. Alerts that arrive later in a long outage are still delivered.
- **Dropped alerts are silent:** When the window ends, Cloud Tasks deletes the task. Cloud Tasks has no dead-letter queue, and the foundation module does not enable queue logging (`stackdriver_logging_config`), so nothing records the drop.
- **Explicit timer retry window:** A failed `/timer/wake` dispatch is retried until `timer_queue_max_retry_duration` has passed since its first attempt. With fast failures, the backoff runs 1s, 2s, 4s … 32s and then holds at 60s. Retrying a wake is safe: a wake that arrives before its deadline, or for a group that is already ticketed, resolved, or recovered, returns `ignored` without side effects. A late wake still tickets the group, and the bridge returns the existing ticket if one was already created for the group. When the window ends, a still-failing wake is deleted silently, as on the triage queue.
- **Applying changes:** Queue settings change only when you apply the foundation module. Editing `main.tf` or the variables does not affect the live queues until then. To change only one queue, use a targeted apply as described in [DEPLOY.md, Applying Queue-Only Changes](DEPLOY.md#41-applying-queue-only-changes).
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
- **Cool-Down Linger (`180s`):** When all member alerts close, the group transitions to `cooling_down` and a 180s timer is scheduled. If an alert re-triggers within 180s, the group reactivates to `ticketed` and appends a work note. If 180s elapses uninterrupted, the outcome depends on `AUTO_RESOLVE_ON_RECOVERY`:
  - `false` (default): the ticket stays In Progress. The agent posts one work note: "Monitoring alerts closed: all <N> alerts of this incident closed by <time> and none reopened during the 180s cool-down. An alert also closes when its service stops receiving traffic, so a close is not proof of a fix; the agent recorded no remediation. <traffic facts> A human must verify the remediation (...) before resolving this incident." The traffic facts come from Cloud Monitoring `run.googleapis.com/request_count` for each affected Cloud Run service, from `RECOVERY_TRAFFIC_LOOKBACK_SECONDS` before the last close until the note is written: either "recorded no requests ... (missing data)", or the request and HTTP 5xx counts, or "not checked" with the reason. The alert state itself carries no close reason. The group becomes `recovered`, a terminal state. Like `resolved`, it is excluded from grouping, so a new alert opens a new group. The agent claims the group (`cooling_down` → `recovered`) before it posts the note, so a retried or duplicate cooling wake is ignored and posts no second note. If posting the note fails, the claim is released and the wake returns an error, so the timer queue retries it. The OKF records `recovered_at` (when the cool-down began) and `recovery_note_posted_at`.
  - `true`: the ticket is resolved in ServiceNow (`POST /instructions/ticket/resolve`) and the group becomes `resolved`.

  An unticketed group with an explicit suppression reason has no ServiceNow incident and is marked `resolved` with either setting. The agent logs the decision at INFO (`JSON_DECISION: auto_resolve_on_recovery=...`).

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

Client traffic reaching the demo microservices routes through the Apigee X environment `APIGEE_ENV` of organization `APIGEE_ORG`. `scripts/deploy-apigee-proxies.sh` creates every name below from `NAME_PREFIX` ([DEPLOY.md Step 9](DEPLOY.md#step-13-apigee)), and `scripts/teardown-apigee.sh` removes them:

| Property | Value | Description |
| :--- | :--- | :--- |
| **Ingress Hostname** | `APIGEE_HOST` | Environment group hostname that serves `APIGEE_ENV` (for example `<IP>.nip.io`). |
| **Proxies and base paths** | `<prefix>-orders-v1` (`/<prefix>/orders/v1`), `<prefix>-inventory-v1` (`/<prefix>/inventory/v1`) | Run as `<prefix>-apigee-sa`. |
| **TargetServers** | `<prefix>-orders-target`, `<prefix>-inventory-target` | Point at the stack's own Cloud Run services. |
| **API Product** | `<prefix>-product` | Bundles `<prefix>-orders-v1` and `<prefix>-inventory-v1`. |
| **Developer Account** | `<prefix>-dev@example.com` | Registered Apigee developer identity. |
| **Developer App** | `<prefix>-demo-app` | Registered application holding API credentials. |
| **Credential Key File** | `.<prefix>-api-key` | Stores the consumer API key (mode 600, gitignored). |
| **Apigee Env File** | `.<prefix>-apigee.env` | `APIGEE_HOST`, `APIGEE_ORG` and `APIGEE_ENV`, sourceable (gitignored). |
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
| `env` | `demo` \| `staging` \| `prod` | Deployment environment (default: `demo`; the live deployment uses `demo`). Terraform modules take it from the `environment` variable (`-var="environment=..."`); the deploy and bootstrap scripts take it from the shell `ENVIRONMENT` variable. |
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
   - **Cloud Storage Buckets:** State bucket `gs://<project>-<prefix>-tfstate/`, incidents bucket `gs://<project>-<prefix>-incidents/` and build bucket `gs://<project>-<prefix>-build/`.
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

