# Alert Triage Agent: Operator & Evaluator Demonstration Guide

This guide is for SRE operators, site leads, and evaluators demonstrating the capabilities of the **Alert Triage Agent (ATA)** across 5 realistic failure scenarios (**S1 through S5**).

---

## 1. Demo Architecture & Triage Workflow

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

## 2. Environment Preparation

Before executing scenarios, verify that demo credentials and services are reachable:

```bash
cd /workspace/alert-triage-agent

# 1. Verify demo microservice endpoints
./scripts/verify-demo-infra.sh

# 2. Reset any leftover faults from previous runs (waits for alerts to clear; use --no-wait for quick dev runs)
./scripts/reset-scenarios.sh
```

> [!NOTE]
> `reset-scenarios.sh` automatically polls Cloud Monitoring for open `ata-*` alerts and waits for a 5-minute quiet period to prevent lagged metric data (e.g. Apigee ingestion latency) from generating stray tickets. Use `./scripts/reset-scenarios.sh --no-wait` when quick iteration without waiting is desired.

Ensure `/workspace/alert-triage-agent/.ata-api-key` contains the active Apigee consumer key.


---

## 3. Scenario Matrix & Walkthrough (S1–S5)

| ID | Failure Scenario | Target Layer | Expected Groups | Expected Tickets | Origin Component |
| :---: | :--- | :--- | :---: | :---: | :--- |
| **S1** | **Backend 5xx Cascade** | Cloud Run (`ata-inventory-api`) | 1 | 1 | `ata-inventory-api` |
| **S2** | **Backend Latency** | Cloud Run (`ata-inventory-api`) | 1 | 1 | `ata-inventory-api` (Upstream origin) |
| **S3** | **Concurrent Disjoint** | Cloud Run & Apigee simultaneously | **2** | **2** | `ata-orders-v1` & `ata-inventory-api` |
| **S4** | **DB Connection Pool** | Cloud SQL (`ata-inventory-db`) | 1 | 1 | `ata-inventory-db` |
| **S5** | **Bad Deploy / Config Failure** | Cloud Run (`ata-inventory-api`) | 1 | 1 | Explicit revision (e.g. `ata-inventory-api-00062-6pj`) |

---

### Scenario S1: Backend 5xx Error Spike

Injects an internal 500 error fault at an 85% error rate into `ata-inventory-api`.

```bash
./scripts/scenarios/s1_backend_5xx.sh
```

- **Fault Mechanism:** `POST /admin/fault/inject` with `{"type": "500", "rate": 0.85}`.
- **Traffic:** Requests sent via Apigee gateway (`/ata/inventory/v1/items`).
- **Alerts Triggered:**
  - `ata-inventory-5xx` (Cloud Run error rate > 5%)
  - `ata-inventory-latency` (Cloud Run inventory latency > 2s triggered by error handling delay)
  - *Non-firing alert note:* `ata-apigee-orders-5xx` does not open because traffic directed to inventory does not hit the orders proxy, and orders proxy sees no downstream 5xx without concurrent order traffic.
- **Triage Result:**
  - **1 Incident Group, 1 ServiceNow Ticket**
  - **Origin:** `ata-inventory-api`
  - **Blast Radius:** `ata-inventory-v1`, `ata-orders-api`, `ata-orders-v1`
  - **Root Cause:** Internal application exception in inventory service.

---

### Scenario S2: Backend Latency Degradation

Injects an artificial 6500ms latency delay into `ata-inventory-api`, exceeding Apigee's 5000ms target timeout.

```bash
./scripts/scenarios/s2_backend_latency.sh
```

- **Fault Mechanism:** `POST /admin/fault/inject` with `{"type": "latency", "delay_ms": 6500}`.
- **Alerts Triggered:**
  - `ata-inventory-latency` (Cloud Run latency > 2s on `ata-inventory-api`)
  - *Non-firing alert note:* `ata-orders-latency` and Apigee 504 do not fire because isolated inventory delay triggers `ata-inventory-latency` directly, while Apigee's default target timeout (55s) exceeds the 6.5s delay and orders-api client handling completes gracefully without breaching caller thresholds.
- **Triage Result:**
  - **1 Incident Group, 1 ServiceNow Ticket**
  - **Origin:** `ata-inventory-api`
  - **Blast Radius:** `ata-inventory-api`, `ata-apigee-inventory-v1`, `ata-apigee-orders-v1`, `ata-orders-api`
  - **Root Cause:** Backend response time exceeding Apigee 5s target timeout.

---

### Scenario S3: Concurrent Disjoint Incidents

Simultaneously triggers Scenario S1 on `ata-inventory-api` and rate limiting on `ata-orders-v1`.

```bash
./scripts/scenarios/s3_concurrent_disjoint.sh
```

- **Fault Mechanism:** Concurrently triggers 500 error on `ata-inventory-api` and deploys strict SpikeArrest (`<Rate>1pm</Rate>`) on `ata-orders-v1`.
- **Alerts Triggered:**
  - Group 1 (Inventory): `ata-inventory-5xx` (Cloud Run error rate > 5%) and `ata-inventory-latency` (Cloud Run latency > 2s)
  - Group 2 (Orders): `ata-apigee-orders-429` (Apigee orders proxy 429 rate limit spike)
  - *Non-firing alert note:* There is no `ata-apigee-inventory-5xx` policy configured in the project; inventory failures trigger Cloud Run alerts directly.
- **Correlation Evaluation:**
  - Topology distance: 2 hops.
  - Causal failure chain: `client_429` vs `backend_5xx` -> incompatible (0 pts).
  - Total multi-factor score: < 35 pts (below auto-new group threshold).
- **Triage Result:**
  - **Exactly 2 Distinct Incident Groups, 2 Distinct ServiceNow Tickets**
  - **Origin:** Group 1: `ata-inventory-api` | Group 2: `ata-orders-v1`
  - **Blast Radius:** Group 1: `ata-inventory-api, ata-apigee-inventory-v1, ata-apigee-orders-v1, ata-orders-api` | Group 2: `ata-orders-api, ata-apigee-orders-v1, ata-orders-v1`
  - Proves the system does not incorrectly merge independent, concurrent incidents.

---

### Scenario S4: Database Connection Pool Exhaustion

Simulates a database connection leak exhausting Cloud SQL PostgreSQL backend connections.

```bash
./scripts/scenarios/s4_db_connection_pool.sh
```

- **Fault Mechanism:** Rapidly acquires and leaks PostgreSQL connections via `POST /admin/leak-connections` and background `SELECT pg_sleep(300)`.
- **Alerts Triggered:**
  - `ata-cloudsql-connections-critical` (Cloud SQL connections > 20 backends / ~80% of max 25)
  - `ata-inventory-latency` (Cloud Run inventory latency > 2s under connection starvation)
  - *Non-firing alert note:* `ata-inventory-5xx` and Apigee 502/504 do not fire because connection pool saturation causes request threads to block waiting for database connections, manifesting as severe backend latency rather than immediate application 5xx errors or proxy gateway timeouts.
- **Triage Result:**
  - **1 Incident Group, 1 ServiceNow Ticket**
  - **Origin:** `ata-inventory-db` (earliest onset timestamp)
  - **Blast Radius:** `ata-inventory-db` -> `ata-inventory-api` -> `ata-orders-api` -> Apigee Proxies
  - **Root Cause:** PostgreSQL connection starvation / pool exhaustion.

---

### Scenario S5: Bad Deploy / Config Failure

Deploys a new Cloud Run revision of `ata-inventory-api` configured with invalid IAM database credentials.

```bash
./scripts/scenarios/s5_bad_deploy.sh
```

- **Fault Mechanism:** `gcloud run services update` with invalid database user.
- **Alerts Triggered:**
  - `ata-inventory-5xx` (Cloud Run inventory 5xx rate > 5%)
  - `ata-inventory-latency` (Cloud Run inventory latency > 2s)
  - *Non-firing alert note:* Zero Cloud SQL alerts fire because authentication fails during connection initialization in Cloud Run before establishing valid database sessions.
- **Triage Result:**
  - **1 Incident Group, 1 ServiceNow Ticket**
  - **Origin:** `ata-inventory-api` explicitly named revision (e.g. `ata-inventory-api-00062-6pj`)
  - **Blast Radius:** `ata-inventory-api`, `ata-apigee-inventory-v1`, `ata-apigee-orders-v1`, `ata-orders-api`
  - **Root Cause:** Change-induced database authentication failure.

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

# Run a specific scenario
./scripts/run-all-scenarios.sh --scenario s3

# Dry-run validation mode (no network modification)
./scripts/run-all-scenarios.sh --dry-run
```

---

## 6. Inspecting Generated Artifacts

### 1. View Generated GCS OKF Incident Files
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

### 2. View ServiceNow Incident Records
Log into the ServiceNow instance (e.g. `https://<instance>.service-now.com`) and navigate to **Incidents > All**:
- **Number:** `INC0010482`
- **Short Description:** Executive summary with origin attribution and priority.
- **Description:** Structured 4-bullet SRE summary (Problem, Systems, Observations, Recommended Actions).
- **Attachments:** Self-contained diagnostic HTML investigation report.
