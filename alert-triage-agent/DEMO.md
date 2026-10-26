# Alert Triage Agent: Operator & Evaluator Demonstration Guide

This guide is for SRE operators, site leads, and evaluators demonstrating the capabilities of the **Alert Triage Agent (ATA)** across 5 realistic failure scenarios (**S1 through S5**).

---

## 1. Run It Yourself (Operator Runbook)

Follow this section to execute scenarios directly from your laptop.

### 1.1 Prerequisites & Laptop Setup

1. **Authenticate with Google Cloud:**
   ```bash
   gcloud auth login
   gcloud config set project cloud-assist-fde-2
   ```
   > [!IMPORTANT]
   > Authenticate using your individual user credentials. Do NOT mint tokens for service accounts or use `--impersonate-service-account`. Scenario scripts automatically detect user accounts and fall back to audience-free identity tokens for Cloud Run invocation.

2. **Check Out the Branch:**
   ```bash
   gh pr checkout 40
   cd alert-triage-agent
   ```

3. **Verify Required IAM Roles on `cloud-assist-fde-2`:**
   Ensure your GCP user account has the following roles:
   - `roles/run.invoker` (Cloud Run Invoker) on `ata-inventory-api` and `ata-orders-api` (to trigger demo endpoints `/admin/fault/inject` and `/admin/leak-connections`)
   - `roles/apigee.admin` or `roles/apigee.readOnly` (to read developer app consumer keys)
   - `roles/storage.objectViewer` on `gs://cloud-assist-fde-2-ata-incidents` (to read OKF incident files via `check-result.sh`)
   - `roles/monitoring.viewer` (to query Cloud Monitoring alert states during resets)

4. **Acquire Apigee Consumer API Key:**
   Retrieve the consumer key for `ata-demo-app` using your gcloud user credentials:
   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     "https://apigee.googleapis.com/v1/organizations/cloud-assist-fde-2/developers/ata-dev@example.com/apps/ata-demo-app" \
     | python3 -c "import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])" > .ata-api-key
   chmod 600 .ata-api-key
   ```
   *(Alternatively: `export ATA_API_KEY="..."`). The `.ata-api-key` file is gitignored.*

5. **Verify Baseline Health:**
   ```bash
   # 1. Verify demo microservice endpoints
   ./scripts/verify-demo-infra.sh

   # 2. Reset any leftover faults from previous runs
   ./scripts/reset-scenarios.sh
   ```

---

### 1.2 Scenario-by-Scenario Guide (S1 through S5)

Run scenarios sequentially. Always run `./scripts/reset-scenarios.sh` between scenarios to clear faults and allow metric lag windows to settle.

```
Scenario Workflow:
[Inject & Sustained Traffic] ---> [Wait for Hold Window] ---> [check-result.sh] ---> [Verify ServiceNow] ---> [reset-scenarios.sh]
```

---

#### Scenario S1: Backend 5xx Error Spike

Injects an internal 500 error fault at an 85% error rate into `ata-inventory-api` and drives sustained traffic through Apigee.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s1_backend_5xx.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). The script provides real-time progress every 15s. This duration ensures Cloud Monitoring 5-minute alignment windows evaluate and trigger `ata-inventory-5xx` and `ata-inventory-latency`, and allows the single-dispatch windowing lock to complete.
  *(Press `Ctrl-C` at any time to safely stop traffic early and view next steps).*
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s1
  ```
  *(To inspect only incidents created since you started: pass `--since <START_TIMESTAMP>` displayed in the script header).*
- **What to Look for in ServiceNow:**
  - **Ticket Count:** Exactly **1** Incident Ticket (e.g. `INC0010482`).
  - **Title / Short Description:** `P1 Incident: ata-inventory-api - Elevated upstream HTTP 5xx error rate (85%)`
  - **Origin Attribution:** `ata-inventory-api`
  - **Work Notes & Links:**
    - Structured 4-bullet SRE summary (Problem, Systems, Observations, Recommended Actions)
    - Blast radius: `ata-inventory-v1`, `ata-orders-api`, `ata-orders-v1`
    - Propagated alert labels: `app=alert-triage-agent`, `component=ata-inventory-api`, `severity=p1`, `signal=5xx`, `tier=service`
    - Attached self-contained diagnostic HTML investigation report
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

#### Scenario S2: Backend Latency Degradation

Injects an artificial 6500ms latency delay into `ata-inventory-api`, exceeding Apigee's 5000ms target timeout.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s2_backend_latency.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). Sustains requests with 6.5s latency to ensure Cloud Monitoring latency policies trigger.
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s2
  ```
- **What to Look for in ServiceNow:**
  - **Ticket Count:** Exactly **1** Incident Ticket.
  - **Title / Short Description:** Backend latency degradation on `ata-inventory-api`.
  - **Origin Attribution:** `ata-inventory-api` (Upstream origin).
  - **Work Notes & Links:**
    - Root cause analysis attributing latency to backend response time exceeding Apigee 5s timeout
    - Blast radius: `ata-inventory-api`, `ata-apigee-inventory-v1`, `ata-apigee-orders-v1`, `ata-orders-api`
    - Attached diagnostic HTML investigation report
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

#### Scenario S3: Concurrent Disjoint Incidents

Simultaneously triggers Scenario S1 on `ata-inventory-api` and strict SpikeArrest rate limiting (`1pm`) on `ata-orders-v1`.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s3_concurrent_disjoint.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). Generates concurrent traffic streams to inventory (driving 5xx errors) and orders (driving 429 rate limits).
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s3
  ```
- **What to Look for in ServiceNow:**
  - **Ticket Count:** **EXACTLY 2 DISTINCT SERVICENOW TICKETS** (Proves the correlation engine splits concurrent independent incidents rather than false merging).
  - **Ticket 1 (Inventory Group):**
    - Origin: `ata-inventory-api`
    - Root Cause: 5xx Application Error in inventory service
    - Alerts: `ata-inventory-5xx`, `ata-inventory-latency`
  - **Ticket 2 (Orders Group):**
    - Origin: `ata-orders-v1`
    - Root Cause: SpikeArrest rate limiting in Apigee orders proxy
    - Alerts: `ata-apigee-orders-429`
  - **Work Notes & Links:** Correlation engine multi-factor score < 35 pts (below auto-new group threshold); separate blast radiuses and HTML diagnostic reports on each ticket.
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

#### Scenario S4: Database Connection Pool Exhaustion

Simulates a database connection leak exhausting Cloud SQL PostgreSQL backend connections.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s4_db_connection_pool.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). Leaks connections and sustains traffic to observe downstream database starvation.
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s4
  ```
- **What to Look for in ServiceNow:**
  - **Ticket Count:** Exactly **1** Incident Ticket.
  - **Title / Short Description:** Database connection pool exhaustion on Cloud SQL.
  - **Origin Attribution:** `ata-inventory-db` (Cloud SQL instance identified by earliest onset timestamp).
  - **Work Notes & Links:**
    - Causal failure chain: `ata-inventory-db` -> `ata-inventory-api` -> `ata-orders-api` -> Apigee proxies
    - Root cause: PostgreSQL connection starvation / pool exhaustion
    - Attached diagnostic HTML investigation report
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

#### Scenario S5: Bad Deploy / Config Failure

Deploys a new Cloud Run revision of `ata-inventory-api` configured with invalid IAM database credentials.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s5_bad_deploy.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). Sustains traffic against the failing revision.
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s5
  ```
- **What to Look for in ServiceNow:**
  - **Ticket Count:** Exactly **1** Incident Ticket.
  - **Title / Short Description:** Database authentication / configuration deployment failure.
  - **Origin Attribution:** Explicitly named Cloud Run revision (e.g. `ata-inventory-api-00062-6pj`).
  - **Work Notes & Links:**
    - Distinction between change-induced configuration failure and runtime resource exhaustion
    - Rollback instructions targeting previous healthy revision
    - Attached diagnostic HTML investigation report
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

## 2. Demo Architecture & Triage Workflow

```
[Fault Injected] ---> [Apigee / Cloud Run / Cloud SQL] ---> [Cloud Monitoring Fires]
                                                                     |
                                                                     v
                                                            [Bridge Webhook]
                                                                     |
                                                                     v (Single-dispatch queue)
                                                            [Cloud Tasks Queue]
                                                                     |
                                                                     v
                                                            [ata-agent ADK Runtime]
                                                                     |
                                                                     +--> [1. Pre-Analysis Lock (GCS OKF)]
                                                                     +--> [2. MCP Specialist Tools (FastMCP)]
                                                                     +--> [3. Correlation Engine (Score 0-100)]
                                                                     +--> [4. Root Cause & Blast Radius]
                                                                     +--> [5. ServiceNow Ticket & HTML Report]
```

### Key Capabilities Demonstrated
1. **Deduplication & Intelligent Grouping:** Groups cascading alerts across DB, backend, and API gateway into a single unified incident.
2. **Root Cause Attribution:** Traces failure origins back to the earliest fault (e.g. identifying Cloud SQL as the origin of downstream 504 errors).
3. **Change-Induced Fault Detection:** Distinguishes between configuration rollouts (bad database credentials) and runtime resource exhaustion.
4. **Disjoint Incident Separation (S3):** Proves the correlation engine splits concurrent unrelated failures into distinct incident groups and tickets.
5. **Fault Control Telemetry Exclusion:** Proves `/admin/fault` endpoints are strictly suppressed and invisible in Cloud Logging.

---

## 3. Scenario Matrix (S1–S5)

| ID | Failure Scenario | Target Layer | Expected Groups | Expected Tickets | Origin Component |
| :---: | :--- | :--- | :---: | :---: | :--- |
| **S1** | **Backend 5xx Cascade** | Cloud Run (`ata-inventory-api`) | 1 | 1 | `ata-inventory-api` |
| **S2** | **Backend Latency** | Cloud Run (`ata-inventory-api`) | 1 | 1 | `ata-inventory-api` (Upstream origin) |
| **S3** | **Concurrent Disjoint** | Cloud Run & Apigee simultaneously | **2** | **2** | `ata-orders-v1` & `ata-inventory-api` |
| **S4** | **DB Connection Pool** | Cloud SQL (`ata-inventory-db`) | 1 | 1 | `ata-inventory-db` |
| **S5** | **Bad Deploy / Config Failure** | Cloud Run (`ata-inventory-api`) | 1 | 1 | Explicit revision (e.g. `ata-inventory-api-00062-6pj`) |

---

## 4. Resetting Faults Between Scenarios

To restore all demo services and Apigee proxies back to a healthy state:

```bash
./scripts/reset-scenarios.sh
```

For rapid development runs where waiting for metric stabilization is not required:
```bash
./scripts/reset-scenarios.sh --no-wait
```

### Reset Execution Sequence & Metric Lag Safeguards

This script:
1. Calls `POST /admin/fault/reset` on all Cloud Run demo services.
2. Terminates background PostgreSQL sleep connections and releases leaks.
3. Restores healthy Apigee proxy revisions (`demo/apigee/`).
4. Reverts any modified Cloud Run environment variables (pinned revisions).
5. **Waits for open alerts to close & observes quiet period:** Polls the Cloud Monitoring Alerts API (read-only) for active `OPEN` alerts on `ata-*` policies (labeled `app=alert-triage-agent`) and waits until none are open, followed by a configurable quiet period (default 5 minutes / 300s).
   - **Why resets wait for alerts to clear (Metric Lag Rationale):** Telemetry ingestion pipelines—specifically Apigee proxy metrics published to Cloud Monitoring—experience an ingestion lag of 1 to 3 minutes. If incident files and group state are archived immediately upon proxy restoration, lagging Apigee metric data can trigger a delayed alert (such as `ata-apigee-orders-429`) minutes after the reset has completed. Because prior incident files are already archived, the agent treats the late-arriving alert as a brand new incident and creates a stray ServiceNow ticket (e.g., finding `INC0010294`). Waiting until all open alerts have cleared plus the 5-minute quiet period guarantees that the metric ingestion window has fully stabilized before incident state is archived.
6. **Archives active OKF files:** Only after all alerts have closed and the quiet period has elapsed does the script move active incident files from `gs://<bucket>/knowledge/incident-manager/*.md` to `archive/` and remove leftover root copies.
7. **Verifies healthy baseline:** Confirms HTTP 200 health responses across all services and verifies 0 active incident groups on the bridge.
8. **Preflights admin endpoints:** Verifies required fault injection endpoints (`/admin/fault/inject` and `/admin/leak-connections`) are active on deployed microservices.

### CLI Options & Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--no-wait` | Skips polling Cloud Monitoring and bypasses the quiet period for quick developer resets | `false` |
| `--quiet-period <duration>` | Quiet period window to maintain after all open alerts close (e.g. `300s`, `5m`) | `300s` (`5m`) |
| `--timeout <duration>` | Maximum timeout to wait for open alerts to clear before failing with still-open alerts listed | `900s` (`15m`) |
| `--poll-interval <duration>`| Interval between Cloud Monitoring alert queries | `15s` |
| `--dry-run` | Simulates reset steps without issuing network mutations | `false` |
| `--mock` | Executes in mock mode without GCP cloud dependencies | `false` |

---

## 5. Master Scenario Driver

To execute scenarios automatically with logging and result assertions:

```bash
# Run prioritized scenarios (S4, S1, S3, S5)
./scripts/run-all-scenarios.sh --priority

# Run full scenario suite (S1 through S5)
./scripts/run-all-scenarios.sh --all

# Run a specific scenario with custom hold duration
./scripts/run-all-scenarios.sh --scenario s3 --hold 5m

# Dry-run validation mode (no network modification)
./scripts/run-all-scenarios.sh --dry-run
```

---

## 6. Inspecting Generated Artifacts

### 1. Result Checker Script
Run the automated result checker to inspect OKF incident files in GCS and validate against DEMO.md expectations:
```bash
# Inspect all recent incidents
./scripts/scenarios/check-result.sh

# Filter to incidents created since a timestamp
./scripts/scenarios/check-result.sh --since "2026-10-26T22:00:00Z"

# Compare against specific scenario expectation
./scripts/scenarios/check-result.sh --scenario s3
```

### 2. View Raw GCS OKF Incident Files
Inspect active incident Markdown files stored in Cloud Storage under `knowledge/incident-manager/`:
```bash
gsutil ls -l gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/
gsutil cat gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/<incident-filename>.md
```

Archived incidents from scenario resets are moved to:
```bash
gsutil ls -l gs://cloud-assist-fde-2-ata-incidents/archive/
```

Each file contains YAML frontmatter:
```yaml
---
type: incident
group_id: grp_20261022t220216z_ata-inventory-api
project: cloud-assist-fde-2
policy_name: ata-inventory-5xx
resource_name: ata-inventory-api
status: mitigation-in-progress
origin: ata-inventory-api
blast_radius:
  - ata-inventory-api
  - ata-inventory-v1
  - ata-orders-api
  - ata-orders-v1
priority: P1
servicenow_sys_id: sn-sys-10482
servicenow_number: INC0010482
---
```

### 3. View ServiceNow Incident Records
Log into the ServiceNow instance (e.g. `https://<instance>.service-now.com`) and navigate to **Incidents > All**:
- **Number:** `INC0010482`
- **Short Description:** Executive summary with origin attribution and priority.
- **Description:** Structured 4-bullet SRE summary (Problem, Systems, Observations, Recommended Actions).
- **Attachments:** Self-contained diagnostic HTML investigation report.
