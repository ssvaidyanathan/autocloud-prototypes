# Alert Triage Agent: Operator & Evaluator Demonstration Guide

This guide is for SRE operators, site leads, and evaluators demonstrating the capabilities of the **Alert Triage Agent (ATA)** across 5 realistic failure scenarios (**S1 through S5**).

---

## 1. Run It Yourself (Operator Runbook)

Follow this section to execute scenarios directly from your laptop.

### 1.1 Prerequisites & Laptop Setup

1. **Authenticate with Google Cloud:**
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project cloud-assist-fde-2
   ```
   `gcloud auth application-default login` is needed because `reset-scenarios.sh` queries the Cloud Monitoring Alerts API with an Application Default Credentials token: federated gcloud user tokens are rejected there with `401 ACCESS_TOKEN_TYPE_UNSUPPORTED`.
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
   `verify-demo-infra.sh` times every request. The first request to an idle revision is a cold start (~5 s); if a request was slower than 2000 ms (the latency alert threshold), returned HTTP 5xx or got no response, it lists it and tells you to run `./scripts/reset-scenarios.sh --alert-window-from-start` next. With the Terraform latency retest window (`latency_duration_secs`, default 240s) applied, a single cold start cannot open a latency alert. With the older 60s window, a cold start opened `ata-inventory-latency` 5.5–7 min later and the agent ticketed it (stray ticket INC0010328, whose group then absorbed S1's alerts). The reset cannot stop that ticket. `--alert-window-from-start` makes it watch for such an alert for `--alert-lag` (default 10 min) from its start, wait until the alert has closed and its incident group is terminal, and archive it, so it does not spill into the next scenario.

---

### 1.2 Scenario-by-Scenario Guide (S1 through S5)

Run scenarios sequentially. Always run `./scripts/reset-scenarios.sh` between scenarios to clear faults and allow metric lag windows to settle.

```
Scenario Workflow:
[Inject & Sustained Traffic] ---> [Wait for Hold Window] ---> [check-result.sh] ---> [Verify ServiceNow] ---> [reset-scenarios.sh]
```

**When the alerts close:**
- **Latency and 5xx alerts (S1, S2, S3, S5)** close after the scenario's traffic stops, even though the fault is still injected. With `evaluation_missing_data = EVALUATION_MISSING_DATA_INACTIVE` (Terraform `alert_evaluation_missing_data`, once applied) a series with no data counts as not violating, so they close about 1–2 evaluation periods after the last request. This is the expected behaviour; it has not been measured live yet. Without it, a latency series that stops reporting waits for `auto_close` (1800s): in the QA run of 2026-10-27, S2's alerts closed about 35 min after they opened, and S5's latency alert on the old revision about 28 min after it opened.
- **S4's connections alert does not close on its own.** The leaked connections are held by `ata-inventory-api` until `reset-scenarios.sh` releases them, so `ata-cloudsql-connections-critical` keeps receiving data and stays OPEN. QA saw it still open 18 min after the traffic stopped. **For S4, run `./scripts/reset-scenarios.sh` first**; the alert closes about 7 min after the connections are released (QA round 2: released 13:39:12Z, closed 13:46:18Z; opening shows the same lag, leak 13:22:06Z to open 13:29:37Z: Cloud SQL metric ingestion of up to 210 s plus alert evaluation, not the policy's 60s duration) and the recovery note follows about 4 min later. The reset's `POST /admin/fault/reset` releases the connections only when it reaches the Cloud Run instance that holds them; in QA round 2 it reached another instance, and the leak lasted until Cloud Run scaled the leaking instance in, 3 min later.
- **The ticket stays In Progress.** With the default `AUTO_RESOLVE_ON_RECOVERY=false` the agent does **not** resolve the ticket. About 180s after the last alert closes it posts one work note: "Monitoring alerts closed: all <N> alerts of this incident closed by <time> and none reopened during the 180s cool-down. An alert also closes when its service stops receiving traffic, so a close is not proof of a fix; the agent recorded no remediation. <traffic facts> A human must verify the remediation (...) before resolving this incident." The traffic facts come from Cloud Monitoring `request_count` for each affected Cloud Run service around the close: "recorded no requests ... (missing data)" when the traffic generator has stopped, or the request and HTTP 5xx counts. The incident group's OKF `status` becomes `recovered`. Verify the fault is gone (after `reset-scenarios.sh`, or the S5 revert) and resolve the ticket yourself. A new alert after that point opens a new group and a new ticket. "Incident resolved. All monitoring conditions recovered and cool-down expired." appears only if the agent is deployed with `AUTO_RESOLVE_ON_RECOVERY=true`.
- **The reset waits for the recovery note.** Before archiving, `reset-scenarios.sh` waits for the alerts to close. From that moment two waits run together: its 180s quiet period, and the wait for every incident group in `knowledge/incident-manager/` to reach a terminal state (`resolved`, `archived`, `degraded`, or `recovered` with `recovery_note_posted_at` set). The agent writes `recovered` when the cool-down ends, before it posts the note, and sets `recovery_note_posted_at` only after the note is posted, so a `recovered` group without it still counts as active. It archives when both are done, so the reset ends about max(180s, time to the recovery note) after the last close, not the sum: about 3 min, since the note also follows the close by about 180s. A group still `cooling_down`, `ticketed`, `open`, `investigating` or `mitigation-in-progress` when `--timeout` (default 30 min) expires fails the reset with the groups listed; nothing is archived. The default is 30 min because of S4 (below). For a group that will never become terminal (e.g. stuck `investigating`), `--ignore-group <file>` leaves only that group out of this wait (risky: it is archived in whatever state it is in); `--no-wait` skips every wait and can archive a group whose note is still pending.

- **A single slow request does not open a latency alert** once the Terraform retest window `latency_duration_secs` (default 240s) is applied: the per-service p99 must stay above 2000ms for 240s, i.e. more than three consecutive 60s windows (a cold start raised it for 1 or 2 windows in QA). Before that change (60s), one cold start on an idle revision (first request after scale-to-zero, ~5 s) opened `ata-*-latency` about 6 min later, which is how the stray tickets INC0010327 (a `/health` call after an S5 revert) and INC0010328 (`verify-demo-infra.sh`) were created. The latency alerts therefore open about 3 min later than in the QA timings below.

- **The reset's own requests cannot leave an alert behind.** Every request `reset-scenarios.sh` sends to the demo services runs before its alert wait: the fault reset (step 1) and, after the Apigee and Cloud Run restore, step 5b, which warms up both services with `GET /health` (the cold start of an idle revision, e.g. the pinned revision after an S5 revert, lands here), checks health through Apigee and probes the admin endpoints. Nothing is sent after the archive. Each request is timed. If one was slower than 2000 ms (`ATA_RESET_SLOW_REQUEST_MS`, keep it equal to the latency policies' threshold), returned HTTP 5xx or got no response, step 6 keeps polling for alerts until `--alert-lag` after it (default 600s: the live lag from a slow request to its alert was 332–411 s with the 60s retest window, and a 240s window can add up to 3 min), and `--timeout` counts from the end of that window. An alert that opens in that time is waited for like any other: it must close, the quiet period must pass and its incident group must become terminal before the archive. Without such a request no time is added. While the latency policies still have the 60s retest window, such an alert still gets a ticket; the reset only makes sure it is finished and archived before the reset ends. In the QA run of 2026-10-27, INC0010327 came from a cold `/health` call at 10:05:26Z whose alert opened at 10:10:58Z, after the reset had finished.

**Expected timing (QA run, 2026-10-27, `--hold 7m`):**

| Scenario | First alert after start | Ticket after first alert | Alerts close | Recovery note |
| :--- | :--- | :--- | :--- | :--- |
| S1 | ~6 min | ~2.3 min | ~6 min after traffic stops | ~3 min after the last close |
| S2 | ~6 min | ~3.8 min (the orders alert joins during the 120s settle) | via `auto_close` until the missing-data change is applied (QA: ~35 min after opening) | ~3 min after the last close |
| S3 | ~6 min (inventory 5xx), ~9 min (orders 429) | ~2.3 min each | ~6–7 min after traffic stops | ~3 min after each group's last close |
| S4 | ~8 min | ~2.3 min | only after `reset-scenarios.sh`: ~7 min after the connections are released (QA round 2) | ~4 min after the close |
| S5 | ~5 min | ~4.4 min | 5xx ~7 min after traffic stops; the latency alert on the old revision via `auto_close` until the missing-data change is applied (QA: ~28 min) | ~3 min after the last close |


---

#### Scenario S1: Backend 5xx Error Spike

Injects an internal 500 error fault at an 85% error rate into `ata-inventory-api` and drives sustained traffic through Apigee.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s1_backend_5xx.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). The script provides real-time progress every 15s. In QA, `ata-inventory-5xx` and `ata-orders-5xx` opened about 6 min after the start (orders-api returns 502 when inventory fails). No latency alert fires in S1.
  *(Press `Ctrl-C` at any time to safely stop traffic early and view next steps).*
- **Check Results Command:**
  ```bash
  ./scripts/scenarios/check-result.sh --scenario s1
  ```
  *(To inspect only incidents created since you started: pass `--since <START_TIMESTAMP>` displayed in the script header).*
- **What to Look for in ServiceNow:**
  - **Ticket Count:** Exactly **1** Incident Ticket (e.g. `INC0010482`).
  - **Title / Short Description:** `[P1] ata-inventory-api: <problem summary> (<N> alerts)`, e.g. `[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)`
  - **Origin Attribution:** `ata-inventory-api`
  - **Work Notes & Links:**
    - Structured 4-bullet SRE summary (Problem, Systems, Observations, Recommended Actions)
    - "Origin selection" line: `ata-orders-api` 5xx explained by upstream `ata-inventory-api`
    - No traffic change or rollback is recommended (no recent change on the origin)
    - Blast radius: `ata-inventory-v1`, `ata-orders-api`, `ata-orders-v1`
    - Propagated alert labels: `app=alert-triage-agent`, `component=ata-inventory-api`, `severity=p1`, `signal=5xx`, `tier=service`
    - Attached self-contained diagnostic HTML investigation report
- **Reset Command Before Next Scenario:**
  ```bash
  ./scripts/reset-scenarios.sh
  ```

---

#### Scenario S2: Backend Latency Degradation

Injects an artificial 6500ms latency delay into `ata-inventory-api`. The `ata-inventory-v1` proxy's target timeout (5000ms in the proxy bundle) is shorter, so Apigee returns HTTP 504 GatewayTimeout.

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
  - **Title / Short Description:** `[P2] ata-inventory-api: Elevated latency (<N> alerts)` (alerts: `ata-inventory-latency`, `ata-orders-latency`).
  - **Origin Attribution:** `ata-inventory-api` (Upstream origin).
  - **Work Notes & Links:**
    - Root cause states the measured peak latency of `ata-inventory-api` and, from Apigee telemetry, the HTTP 504 responses with fault `GatewayTimeout` on `ata-inventory-v1`. The timeout value itself is not in the gathered telemetry, so it is not stated.
    - Blast radius: `ata-inventory-api`, `ata-inventory-v1`, `ata-orders-api`, `ata-orders-v1`
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
    - Root Cause: HTTP 5xx errors on the inventory service
    - Alerts: `ata-inventory-5xx` (`ata-orders-5xx` does not fire: orders traffic is throttled at Apigee)
  - **Ticket 2 (Orders Group):**
    - Origin: `ata-orders-v1`
    - Root Cause: the new `ata-orders-v1` proxy revision (deployed by the script with `Rate=1pm`) rejects requests with SpikeArrest faults, which the previous revision did not; action 1 redeploys the previous revision (explicit number)
    - Alerts: `ata-apigee-orders-429`
  - **Work Notes & Links:** each ticket has a "Correlation:" observation. The orders ticket states the best correlation score against the open inventory group and that it is below the 35 pts new-group threshold. Separate blast radiuses and HTML diagnostic reports on each ticket.
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
  - **Title / Short Description:** `[P1] ata-inventory-db: Database connection pool saturation (1 alert)` (alert: `ata-cloudsql-connections-critical`).
  - **Origin Attribution:** `ata-inventory-db` (Cloud SQL instance identified by earliest onset timestamp).
  - **Work Notes & Links:**
    - Problem: connection pool saturation with the measured connection count. A 5xx cascade to dependents is stated only when a dependent's 5xx rate was measured; in QA no 5xx occurred, and the ticket says so.
    - Actions: review the connection release in `ata-inventory-api` (the Cloud Run caller in TOPOLOGY.md) and a read-only `gcloud sql instances describe` of tier and flags (no flag patch)
    - Attached diagnostic HTML investigation report
  - **Order traffic** uses the in-stock item with the most stock (`GET /items`); the script stops with an error if no item is in stock, because every order would return HTTP 400.
- **Reset Command (run it right after the hold; the alert does not close before it):**
  ```bash
  ./scripts/reset-scenarios.sh
  ```
  The leaked connections persist until the reset releases them, so `ata-cloudsql-connections-critical` stays OPEN after the traffic stops. The reset waits for the close, then for both its 180s quiet period and the group reaching `recovered` (the recovery note is posted about 180s after the close), which run at the same time. The note lands before the incident file is archived.

  **Expected S4 reset time: about 12–15 min, up to ~27 min.** The connections alert closes about 7 min after the connections are released (Cloud SQL metric ingestion plus alert evaluation), and the recovery note and archive follow about 4 min later. QA round 2 took 896 s: the fault reset reached a Cloud Run instance that did not hold the leak, so the connections were released only when Cloud Run scaled the leaking instance in (3 min later). Scale-in of an idle instance can take up to ~15 min, so the worst case is about 15 + 7.5 + 4 ≈ 27 min. The default `--timeout` is 30 min for that reason. The old 15 min default left 4 s of margin in QA.

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
  - **Title / Short Description:** `[P1] ata-inventory-api: Deployment regression: database authentication failure (<N> alerts)` (alerts: `ata-inventory-5xx`, `ata-orders-5xx`). The priority is the most severe alert's `severity` label in the group.
    In the QA run of 2026-10-27 a third alert, `ata-inventory-latency`, also opened: the cold start of the new revision raised the p99 above 2000ms for two 60s windows (09:26 at 4 rpm, 09:27 at 47 rpm). With the Terraform retest window `latency_duration_secs` (default 240s) applied, a cold start alone no longer opens it, so expect 2 alerts.
  - **Origin Attribution:** `ata-inventory-api` (the origin is a topology component). The bad revision (`ata-inventory-api-s5-<epoch>`) is named in the problem, the root cause and the rollback.
  - **Work Notes & Links:**
    - Distinction between change-induced configuration failure and runtime resource exhaustion
    - Rollback to the revision that **served** before the deploy, chosen from traffic history (Cloud Monitoring `request_count` by revision in the 30 min before the new revision was created; `ROLLBACK_TRAFFIC_LOOKBACK_MINUTES`), with that evidence in the action, e.g. `ata-inventory-api-00073-9vb`. The previously *created* revision is never used: after earlier S5 reverts it is a 0%-traffic revision. With split or no traffic the action says the target could not be identified.
    - Attached diagnostic HTML investigation report
  - **Revert:** `./scripts/scenarios/s5_bad_deploy.sh --revert` restores the pinned revision. Each revert also creates a new 0%-traffic revision.
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
| **S5** | **Bad Deploy / Config Failure** | Cloud Run (`ata-inventory-api`) | 1 | 1 | `ata-inventory-api` (bad revision named in root cause and rollback) |

---

## 4. Resetting Faults Between Scenarios

To restore all demo services and Apigee proxies back to a healthy state:

```bash
./scripts/reset-scenarios.sh
```

For rapid development runs where waiting for metric stabilization is not required (risky: it also skips the incident group state check, so a group whose recovery note is still pending can be archived):
```bash
./scripts/reset-scenarios.sh --no-wait
```

If the reset times out on a group that will never become terminal (for example a group stuck in `investigating`, or a stub file left by an older agent), skip only that group. The alert wait, the quiet period and the wait for every other group still run, and the reset prints a warning naming each group it archives in a non-terminal state:
```bash
./scripts/reset-scenarios.sh --ignore-group incident-<group-id>.md
```

### Reset Execution Sequence & Metric Lag Safeguards

This script:
1. Calls `POST /admin/fault/reset` on `ata-inventory-api` and `ata-orders-api` (this releases S4's leaked connections). Every admin call mints a fresh ID token, and any non-2xx response stops the reset with the endpoint and HTTP status (401/403 name the `gcloud auth login` / `roles/run.invoker` fix).
2. Terminates local background PostgreSQL sleep helpers and logs whether any were running.
3. Restores healthy Apigee proxy revisions (`demo/apigee/`); a failed redeploy stops the reset.
4. Reverts any modified Cloud Run environment variables (pinned revisions).
5. **Waits for open alerts to close, then for the quiet period and for incident groups to reach a terminal state (concurrently):** Polls the Cloud Monitoring Alerts API (read-only) for active `OPEN` alerts on `ata-*` policies (labeled `app=alert-triage-agent`) and waits until none are open. From the first poll with zero open alerts, the configurable quiet period (default 3 minutes / 180s) runs, and on every poll the script reads the OKF `status` of every file in `gs://<bucket>/knowledge/incident-manager/` (`gcloud storage cat`, the same login as the archive step) and waits until every group is terminal: `resolved`, `archived`, `degraded`, or `recovered` with `recovery_note_posted_at` set. Any other status, or none, counts as active (e.g. `cooling_down` while the agent's 180s cool-down runs). So does `recovered` without `recovery_note_posted_at`: the agent claims the group as `recovered` before it posts the recovery note. Groups named with `--ignore-group` are left out of this wait. It proceeds when the quiet period has elapsed with no alert reopening AND every group is terminal, so the wait after the close is the longer of the two, not their sum. A reopened alert restarts the quiet period; a group that becomes active again only delays the archive until it is terminal. Both waits share `--timeout` (default 30 min) and `--poll-interval`. On timeout the reset fails and lists the still-open alerts or the groups that are not terminal, with their status; nothing is archived. If the incident files cannot be read, the reset fails with gcloud's error. If the Alerts API cannot be queried, the script exits non-zero with the cause; it never treats a failed query as zero open alerts. Transient errors (timeouts, HTTP 429/5xx) are retried for up to 3 polls first. Errors that cannot clear on their own fail at once: HTTP 401 (`gcloud auth application-default login` for the default ADC token source; `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` means the token type is not accepted, so switch source or use `--no-wait`), HTTP 403 (grant `roles/monitoring.viewer` on the project), and TLS `CERTIFICATE_VERIFY_FAILED`. On macOS, `export SSL_CERT_FILE=/etc/ssl/cert.pem` fixes the certificate error (the script also honours `REQUESTS_CA_BUNDLE`, then `certifi`, then `/etc/ssl/cert.pem` on macOS).
   - **Why resets wait for alerts to clear (Metric Lag Rationale):** Telemetry ingestion pipelines—specifically Apigee proxy metrics published to Cloud Monitoring—experience an ingestion lag of 1 to 3 minutes. If incident files and group state are archived immediately upon proxy restoration, lagging Apigee metric data can trigger a delayed alert (such as `ata-apigee-orders-429`) minutes after the reset has completed. Because prior incident files are already archived, the agent treats the late-arriving alert as a brand new incident and creates a stray ServiceNow ticket (e.g., finding `INC0010294`). Waiting until all open alerts have cleared plus the 3-minute quiet period covers Apigee's documented 1-3 minute metric lag, so the ingestion window has stabilized before incident state is archived. The agent's recovery note is not covered by time: the reset waits for the groups' terminal state instead. Raise it with `--quiet-period` if you still see late alerts.
6. **Archives active OKF files:** Only after all alerts have closed, every incident group is terminal and the quiet period has elapsed does the script move active incident files from `gs://<bucket>/knowledge/incident-manager/*.md` to `archive/` and remove leftover root copies.
7. **Verifies healthy baseline:** Confirms HTTP 200 health responses across all services and verifies 0 active incident groups on the bridge.
8. **Preflights admin endpoints:** GETs the fault injection endpoints (`/admin/fault/inject` and `/admin/leak-connections`) on the pinned inventory revision. Only HTTP 2xx passes; 401/403, 404, connection errors and other codes fail the reset with a clear message.

### CLI Options & Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--no-wait` | Skips polling Cloud Monitoring, the incident group state check and the quiet period for quick developer resets (also skips the alert query, so Monitoring/TLS errors cannot fail the reset). **Risky:** it archives groups the agent is still working on, e.g. a pending recovery note | `false` |
| `--ignore-group <file\|id>` | Leaves one incident group (file name `incident-<id>.md` or group id; repeatable; env `ATA_RESET_IGNORE_GROUPS`) out of the terminal-state wait only, for a group that will never become terminal. The alert wait, the quiet period and the wait for every other group still run. **Risky:** the group is archived in whatever state it is in; the reset warns with its name and status | none |
| `--quiet-period <duration>` | Quiet period window to maintain after all open alerts close, running alongside the wait for terminal incident groups (e.g. `180s`, `3m`); covers Apigee's 1-3 min metric lag | `180s` (`3m`) |
| `--timeout <duration>` | Maximum time to wait for open alerts to clear and incident groups to reach a terminal state before failing with the still-open alerts or active groups listed | `1800s` (`30m`; S4's reset can take up to ~27 min) |
| `--poll-interval <duration>`| Interval between Cloud Monitoring alert queries and incident group state reads | `15s` |
| `--monitoring-token-source <adc\|gcloud>` | Token for the alert check: `adc` = `gcloud auth application-default print-access-token`, `gcloud` = `gcloud auth print-access-token`. Only the chosen source is tried; if it fails the reset fails (run `gcloud auth application-default login`, or use `--no-wait`). Env: `ATA_MONITORING_TOKEN_SOURCE` | `adc` |
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
gcloud storage ls -l gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/
gcloud storage cat gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/<incident-filename>.md
```

Archived incidents from scenario resets are moved to:
```bash
gcloud storage ls -l gs://cloud-assist-fde-2-ata-incidents/archive/
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
