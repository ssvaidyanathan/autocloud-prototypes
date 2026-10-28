# Alert Triage Agent: Operator & Evaluator Demonstration Guide

This guide is for SRE operators, site leads, and evaluators demonstrating the capabilities of the **Alert Triage Agent (ATA)** across 5 realistic failure scenarios (**S1 through S5**).

Resource names in this guide use the default prefix `ata` (`ata-inventory-api`, `ata-orders-v1`, `/ata/inventory/v1`, ...). For a stack deployed with another `NAME_PREFIX`, read `<prefix>-` for `ata-`. Commands use `${PROJECT_ID}`, `${REGION}`, `${NAME_PREFIX}`, `${APIGEE_ORG}` and `${APIGEE_HOST}`: load the stack's inputs first ([DEPLOY.md §2.2](DEPLOY.md#22-inputs)). To deploy a stack, see [DEPLOY.md](DEPLOY.md).

---

## 1. Run It Yourself (Operator Runbook)

Follow this section to execute scenarios directly from your laptop.

### 1.1 Prerequisites & Laptop Setup

1. **Authenticate with Google Cloud:**
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project "${PROJECT_ID}"
   ```
   `gcloud auth application-default login` is needed because `reset-scenarios.sh` queries the Cloud Monitoring Alerts API with an Application Default Credentials token: federated gcloud user tokens are rejected there with `401 ACCESS_TOKEN_TYPE_UNSUPPORTED`.
   > [!IMPORTANT]
   > Authenticate using your individual user credentials. Do NOT mint tokens for service accounts or use `--impersonate-service-account`. Scenario scripts automatically detect user accounts and fall back to audience-free identity tokens for Cloud Run invocation.

2. **Check Out the Branch:**
   ```bash
   gh pr checkout 40
   cd alert-triage-agent
   ```

3. **Verify Required IAM Roles on `${PROJECT_ID}`:**
   Ensure your GCP user account has the following roles:
   - `roles/run.invoker` (Cloud Run Invoker) on `ata-inventory-api` and `ata-orders-api` (to trigger demo endpoints `/admin/fault/inject` and `/admin/leak-connections`)
   - `roles/apigee.admin` or `roles/apigee.readOnly` (to read developer app consumer keys)
   - `roles/storage.objectViewer` on `gs://${PROJECT_ID}-${NAME_PREFIX}-incidents` (to read OKF incident files via `check-result.sh`)
   - `roles/monitoring.viewer` (to query Cloud Monitoring alert states during resets)

4. **Acquire Apigee Consumer API Key:**
   `scripts/deploy-apigee-proxies.sh` writes the key to `.${NAME_PREFIX}-api-key` in the checkout that deployed the stack. From another checkout, retrieve the consumer key for `${NAME_PREFIX}-demo-app` using your gcloud user credentials:
   ```bash
   curl -s -H "Authorization: Bearer $(gcloud auth print-access-token)" \
     "https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}/developers/${NAME_PREFIX}-dev@example.com/apps/${NAME_PREFIX}-demo-app" \
     | python3 -c "import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])" > ".${NAME_PREFIX}-api-key"
   chmod 600 ".${NAME_PREFIX}-api-key"
   ```
   The scenario scripts read `.${NAME_PREFIX}-api-key` and the Apigee values in `.${NAME_PREFIX}-apigee.env` (written by `deploy-apigee-proxies.sh`); `ATA_API_KEY` overrides the key file. The key files are gitignored.

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
- **Latency and 5xx alerts (S1, S2, S3, S5)** close after the scenario's traffic stops, even though the fault is still injected. With `evaluation_missing_data = EVALUATION_MISSING_DATA_INACTIVE` (Terraform `alert_evaluation_missing_data`, once applied) a series with no data counts as not violating, so they close about 1–2 evaluation periods after the last request. QA round 3 measured 6m31s–7m42s from the end of the traffic to the close (table below). Without it, a latency series that stops reporting waits for `auto_close` (Terraform `alert_auto_close_secs`, default 1800s): in the first QA run of 2026-10-27, S2's alerts closed about 35 min after they opened, and S5's latency alert on the old revision about 28 min after it opened.
- **S4's connections alert does not close on its own.** The leaked connections are held by `ata-inventory-api` until `reset-scenarios.sh` releases them, so `ata-cloudsql-connections-critical` keeps receiving data and stays OPEN. QA saw it still open 18 min after the traffic stopped. **For S4, run `./scripts/reset-scenarios.sh` first**; the alert closes about 7 min after the connections are released (QA round 3: 6m37s, recovery note 3m01s later; QA round 2: released 13:39:12Z, closed 13:46:18Z; opening shows the same lag, leak 13:22:06Z to open 13:29:37Z: Cloud SQL metric ingestion of up to 210 s plus alert evaluation, not the policy's 60s duration) and the recovery note follows about 4 min later. The reset's `POST /admin/fault/reset` releases the connections only when it reaches the Cloud Run instance that holds them; in QA round 2 it reached another instance, and the leak lasted until Cloud Run scaled the leaking instance in, 3 min later.
- **The ticket stays In Progress.** With the default `AUTO_RESOLVE_ON_RECOVERY=false` the agent does **not** resolve the ticket. About 180s after the last alert closes it posts one work note: "Monitoring alerts closed: all <N> alerts of this incident closed by <time> and none reopened during the 180s cool-down. An alert also closes when its service stops receiving traffic, so a close is not proof of a fix; the agent recorded no remediation. <traffic facts> A human must verify the remediation (...) before resolving this incident." The traffic facts come from Cloud Monitoring `request_count` for each affected Cloud Run service around the close: "recorded no requests ... (missing data)" when the traffic generator has stopped, or the request and HTTP 5xx counts. The incident group's OKF `status` becomes `recovered`. Verify the fault is gone (after `reset-scenarios.sh`, or the S5 revert) and resolve the ticket yourself. A new alert after that point opens a new group and a new ticket. "Incident resolved. All monitoring conditions recovered and cool-down expired." appears only if the agent is deployed with `AUTO_RESOLVE_ON_RECOVERY=true`.
- **The reset waits for the recovery note.** Before archiving, `reset-scenarios.sh` waits for the alerts to close. From that moment two waits run together: its 180s quiet period, and the wait for every incident group in `knowledge/incident-manager/` to reach a terminal state (`resolved`, `archived`, `degraded`, or `recovered` with `recovery_note_posted_at` set). The agent writes `recovered` when the cool-down ends, before it posts the note, and sets `recovery_note_posted_at` only after the note is posted, so a `recovered` group without it still counts as active. It archives when both are done, so the reset ends about max(180s, time to the recovery note) after the last close, not the sum: about 3 min, since the note also follows the close by about 180s. A group still `cooling_down`, `ticketed`, `open`, `investigating` or `mitigation-in-progress` when `--timeout` (default 30 min) expires fails the reset with the groups listed; nothing is archived. The default is 30 min because of S4 (below). For a group that will never become terminal (e.g. stuck `investigating`), `--ignore-group <file>` leaves only that group out of this wait (risky: it is archived in whatever state it is in); `--no-wait` skips every wait and can archive a group whose note is still pending.

- **A single slow request does not open a latency alert** once the Terraform retest window `latency_duration_secs` (default 240s) is applied: the per-service p99 must stay above 2000ms for 240s, i.e. more than three consecutive 60s windows (a cold start raised it for 1 or 2 windows in QA). Before that change (60s), one cold start on an idle revision (first request after scale-to-zero, ~5 s) opened `ata-*-latency` about 6 min later, which is how the stray tickets INC0010327 (a `/health` call after an S5 revert) and INC0010328 (`verify-demo-infra.sh`) were created. The latency alerts therefore open about 3 min later than with the 60s window; the QA round-3 timings below were measured with 240s.

- **The reset's own requests cannot leave an alert behind.** Every request `reset-scenarios.sh` sends to the demo services runs before its alert wait: the fault reset (step 1) and, after the Apigee and Cloud Run restore, step 5b, which warms up both services with `GET /health` (the cold start of an idle revision, e.g. the pinned revision after an S5 revert, lands here), checks health through Apigee and probes the admin endpoints. Nothing is sent after the archive. Each request is timed. If one was slower than 2000 ms (`ATA_RESET_SLOW_REQUEST_MS`, keep it equal to the latency policies' threshold), returned HTTP 5xx or got no response, step 6 keeps polling for alerts until `--alert-lag` after it (default 600s: the live lag from a slow request to its alert was 332–411 s with the 60s retest window, and a 240s window can add up to 3 min), and `--timeout` counts from the end of that window. An alert that opens in that time is waited for like any other: it must close, the quiet period must pass and its incident group must become terminal before the archive. Without such a request no time is added. While the latency policies still have the 60s retest window, such an alert still gets a ticket; the reset only makes sure it is finished and archived before the reset ends. In the QA run of 2026-10-27, INC0010327 came from a cold `/health` call at 10:05:26Z whose alert opened at 10:10:58Z, after the reset had finished.
- **A cold start of the reset itself no longer costs 10 minutes.** In the final QA run (2026-10-28) every reset after a scenario armed that 600s window and took 645–662 s: the demo services had scaled to zero, so `POST /admin/fault/reset` started a new instance on both (Cloud Run log "Starting new instance", 3.4–3.8 s), and `POST /admin/restock` was the first database call on the new instance (3.5–3.8 s), about 30 s later. The `/health` warm-ups between them took 64–229 ms. Now a slow request with a non-5xx response arms the window only provisionally. After step 5b the reset reads the live `ata-inventory-latency` and `ata-orders-latency` policies (Cloud Monitoring, read-only) and releases the window when the burst of slow requests is too short to keep the p99 above 2000 ms for their retest window: a burst under 60 s holds it for under 180 s, within 240 s with a 60 s margin. It prints `Alert window released: ...` and the risk that remains (other slow traffic in the same minutes, e.g. a scenario still running or `verify-demo-infra.sh` just before, or a policy change). The full window is kept, with the reason printed, when the burst lasts 60 s or more, a policy is missing or has a shorter retest window or a lower threshold (every policy carrying the name is checked, so a duplicate with a 60s window keeps it), the policies cannot be read, any request returned 5xx or failed, with `--alert-window-from-start`, or with `--thorough` (env `ATA_RESET_THOROUGH=true`), which restores the old full wait.

**Expected timing (QA round 3, 2026-10-27, `--hold 7m`, 240s latency window and missing-data setting live):**

| Scenario | First alert after start | Ticket after first alert | Ticket after start | Alerts close | Recovery note | Reset after the scenario |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| S1 | 5m48s | 2m24s | 8m12s | 6m31s after traffic stops | 3m02s after the last close | 276 s |
| S2 | 8m54s (1m43s after the 7 min traffic has stopped) | 2m12s | 11m06s | 7m15s after traffic stops | 3m03s after the last close | 285 s |
| S3 | 6m16s (inventory 5xx), 10m27s (orders 429) | 2m09s / 2m13s | 8m25s / 12m40s | 6m58s (inventory) / 7m42s (429) after traffic stops | ~3 min after each group's last close | 319 s |
| S4 | 7m34s / 7m44s (two runs) | 2m15s / 2m10s | ~9m50s | only after `reset-scenarios.sh`: 6m37s after the connections were released | 3m01s after the close | 600 s (started right after the hold) |
| S5 | 7m51s | 2m16s | 10m07s | 6m34s after traffic stops | 3m02s after the last close | 292 s (plus 23 s for `--revert`) |

The first reset after a cold `verify-demo-infra.sh` warning, with `--alert-window-from-start`, took 616 s.

**Reset duration now: ~4 min (about 3.5–4.5 min) after S1, S2, S3 and S5** (expected from the steps below; not yet measured on a live run since this change). The "Reset after the scenario" column above is QA round 3, before the services scaled to zero during the recovery wait. The final QA run of 2026-10-28, before this change, measured 645 s (after S1), 662 s (S2), 717 s (S3) and 639 s (S5, plus 21 s for `--revert`); only S1's first run, with warm services, took 352 s. The cause: the 600s alert window armed by the reset's own cold start (above), plus step 5c's wait for a fresh `num_backends` sample (50–100 s) before the quiet period could start. With the cold start checked against the live 240s retest window and step 5c running alongside the alert wait, a reset is about 40 s of restore and warm-up, then the 180s quiet period (or the recovery note, whichever is later), then about 15 s of archiving. The quiet period is the floor: 180s is the default you asked for, covering Apigee's 1–3 min metric lag. `--thorough` brings back the ~11 min wait after a cold start. S4 is unchanged (alert close and recovery note: about 10–15 min, see S4; final QA: 609 s).


---

#### Scenario S1: Backend 5xx Error Spike

Injects an internal 500 error fault at an 85% error rate into `ata-inventory-api` and drives sustained traffic through Apigee.

- **Exact Command:**
  ```bash
  ./scripts/scenarios/s1_backend_5xx.sh --hold 7m
  ```
- **How Long to Wait:**
  **7 minutes** (default `--hold 7m`). The script provides real-time progress every 15s. In QA, `ata-inventory-5xx` and `ata-orders-5xx` opened about 6 min after the start (orders-api returns 502 when inventory fails). No latency alert fires in S1.
  *(Press `Ctrl-C` at any time to safely stop traffic early and view next steps. Under `run-all-scenarios.sh`, Ctrl-C stops the whole run; see [§5](#5-master-scenario-driver).)*
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
  **7 minutes** (default `--hold 7m`). Sustains requests with 6.5s latency to ensure Cloud Monitoring latency policies trigger. With the 240s retest window the latency alerts open after the traffic has stopped: in QA round 3, 8m54s after the start (1m43s after the traffic ended), and the ticket followed at 11m06s.
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
    - Actions: (1) review the connection release in `ata-inventory-api` (the Cloud Run caller in TOPOLOGY.md, read-only log read); (2) `pg_terminate_backend` of idle sessions, labelled "Mutating, operator judgement required" and only after the review shows leaked connections; (3) a read-only `gcloud sql instances describe` of tier and flags (no flag patch)
    - Attached diagnostic HTML investigation report
  - **Order traffic** uses the in-stock item with the most stock (`GET /items`); the script stops with an error if no item is in stock, because every order would return HTTP 400.
- **Reset Command (run it right after the hold; the alert does not close before it):**
  ```bash
  ./scripts/reset-scenarios.sh
  ```
  The leaked connections persist until the reset releases them, so `ata-cloudsql-connections-critical` stays OPEN after the traffic stops. The reset waits for the close, then for both its 180s quiet period and the group reaching `recovered` (the recovery note is posted about 180s after the close), which run at the same time. The note lands before the incident file is archived.

  **Alerts: `(1 alert)`.** In QA round 2 the three `POST /admin/leak-connections` calls (2.2–2.6 s each, 13:22:00–06Z) also opened `ata-inventory-latency`, because the latency policies still had a 60s retest window. With `latency_duration_secs = 240` applied (this branch's default), p99 must exceed 2000 ms in every evaluation for 240 s. 6 s of slow admin calls raise the p99 of at most 2 one-minute windows, so they no longer open the alert, and the ticket title says `(1 alert)`. QA round 3, with 240s live, confirmed it: both S4 runs opened only `ata-cloudsql-connections-critical`, and the titles said `(1 alert)`.

  **Expected S4 reset time: about 10–15 min, up to ~27 min.** QA round 3 took 600 s (10 min) with the reset started right after the hold: the fault reset reached the leaking instance (`demo_max_instance_count = 1`), the connections alert closed 6m37s after the release (Cloud SQL metric ingestion plus alert evaluation), and the recovery note followed 3m01s later. QA round 2 took 896 s: the fault reset reached a Cloud Run instance that did not hold the leak, so the connections were released only when Cloud Run scaled the leaking instance in (3 min later). Scale-in of an idle instance can take up to ~15 min, so the worst case is about 15 + 7.5 + 4 ≈ 27 min. The default `--timeout` is 30 min for that reason. The old 15 min default left 4 s of margin in QA.

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
    In the QA run of 2026-10-27 a third alert, `ata-inventory-latency`, also opened: the cold start of the new revision raised the p99 above 2000ms for two 60s windows (09:26 at 4 rpm, 09:27 at 47 rpm). With the Terraform retest window `latency_duration_secs` (default 240s) applied, a cold start alone no longer opens it, so expect 2 alerts. QA round 3 (240s live) confirmed 2 alerts.
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

**Stale OPEN alerts from earlier revisions (troubleshooting).** The Alerts API and the console can list old `ata-inventory-latency` / `ata-orders-latency` alerts that are still OPEN on revisions that no longer serve traffic (final QA, 2026-10-27/28: 16 such alerts, opened 2026-10-23/24, on the current policy IDs). They do not block anything:
- the reset ignores them and lists them once at its first poll: `Ignored 16 stale/non-serving open alert(s):`, one line each with the policy, the revision and the reason (`Non-serving revision (...)`, or `Opened > 60m before reset started`; the age limit is `--max-alert-age`, default 1 h);
- Cloud Monitoring sends no new notification for an alert that stays open, so the agent does nothing with them (none led to a ticket in the final QA).
They are clutter only. The policies' `alert_strategy.auto_close` is `alert_auto_close_secs` (default 1800 s), yet these alerts stayed open for days; why is not established (they may predate that setting, whose Cloud Monitoring default is 7 days). To clear them, close them in the console (Monitoring > Alerting > the alert > Close), with no scenario running. Do not change the reset's ignore rules for them: an alert on a serving revision, opened within the age limit, is still waited for.

### Reset Execution Sequence & Metric Lag Safeguards

This script:
1. Calls `POST /admin/fault/reset` on `ata-inventory-api` and `ata-orders-api`. This releases S4's leaked connections and clears injected faults, but only on the Cloud Run instance the request reaches: fault state and leaked connections live in one instance's memory (see step 4b). Every admin call mints a fresh ID token, and any non-2xx response stops the reset with the endpoint and HTTP status (401/403 name the `gcloud auth login` / `roles/run.invoker` fix).
2. Terminates local background PostgreSQL sleep helpers and logs whether any were running.
3. Restores healthy Apigee proxy revisions (`demo/apigee/`): a proxy is redeployed only when its deployed revision differs from the rendered bundle (e.g. after S3), otherwise it is left alone and logged as `not redeployed`; a failed redeploy stops the reset.
4. Reverts any modified Cloud Run environment variables (pinned revisions).
4b. **Verifies on the database that no leaked connections remain (step 5c in the script):** polls the Cloud SQL `num_backends` gauge of database `inventory` on `ata-inventory-db` (Cloud Monitoring, read-only, same token as the alert wait) until a sample taken after the fault reset shows at most `ATA_RESET_DB_MAX_BACKENDS` (default 5; baseline 0–2, S4 leaks showed 10–22). Samples arrive every 60 s and become visible up to 210 s later (QA 2026-10-28: 50–100 s of waiting after the restore steps), so the check runs in the background, alongside the alert wait (step 6), instead of delaying it; its progress lines still name `ata-inventory-db`. A failure stops step 6 at once, and nothing is archived until the check has passed. If the fault reset reached an instance that does not hold the leak, the connections stay open until Cloud Run scales that instance in (QA round 2: 3 min later). The check waits up to `ATA_RESET_DB_RELEASE_TIMEOUT` (default 1200s: ~15 min scale-in plus ~4.5 min metric lag). It then fails loudly with the last value and what is still left (the leaked connections, and the connections alert that stays open). It never treats a failed query as zero; `--no-wait` skips it with a warning. With the Terraform `demo_max_instance_count = 1` applied, there is only one instance to reach.
5. **Waits for open alerts to close, then for the quiet period and for incident groups to reach a terminal state (concurrently):** Polls the Cloud Monitoring Alerts API (read-only) for active `OPEN` alerts on `ata-*` policies (labeled `app=alert-triage-agent`) and waits until none are open. From the first poll with zero open alerts, the configurable quiet period (default 3 minutes / 180s) runs, and on every poll the script reads the OKF `status` of every file in `gs://<bucket>/knowledge/incident-manager/` (`gcloud storage cat`, the same login as the archive step) and waits until every group is terminal: `resolved`, `archived`, `degraded`, or `recovered` with `recovery_note_posted_at` set. Any other status, or none, counts as active (e.g. `cooling_down` while the agent's 180s cool-down runs). So does `recovered` without `recovery_note_posted_at`: the agent claims the group as `recovered` before it posts the recovery note. Groups named with `--ignore-group` are left out of this wait. It proceeds when the quiet period has elapsed with no alert reopening AND every group is terminal, so the wait after the close is the longer of the two, not their sum. A reopened alert restarts the quiet period; a group that becomes active again only delays the archive until it is terminal. Both waits share `--timeout` (default 30 min) and `--poll-interval`. On timeout the reset fails and lists the still-open alerts or the groups that are not terminal, with their status; nothing is archived. If the incident files cannot be read, the reset fails with gcloud's error. If the Alerts API cannot be queried, the script exits non-zero with the cause; it never treats a failed query as zero open alerts. Transient errors (timeouts, HTTP 429/5xx) are retried for up to 3 polls first. Errors that cannot clear on their own fail at once: HTTP 401 (`gcloud auth application-default login` for the default ADC token source; `401 ACCESS_TOKEN_TYPE_UNSUPPORTED` means the token type is not accepted, so switch source or use `--no-wait`), HTTP 403 (grant `roles/monitoring.viewer` on the project), and TLS `CERTIFICATE_VERIFY_FAILED`. On macOS, `export SSL_CERT_FILE=/etc/ssl/cert.pem` fixes the certificate error (the script also honours `REQUESTS_CA_BUNDLE`, then `certifi`, then `/etc/ssl/cert.pem` on macOS).
   - **Why resets wait for alerts to clear (Metric Lag Rationale):** Telemetry ingestion pipelines—specifically Apigee proxy metrics published to Cloud Monitoring—experience an ingestion lag of 1 to 3 minutes. If incident files and group state are archived immediately upon proxy restoration, lagging Apigee metric data can trigger a delayed alert (such as `ata-apigee-orders-429`) minutes after the reset has completed. Because prior incident files are already archived, the agent treats the late-arriving alert as a brand new incident and creates a stray ServiceNow ticket (e.g., finding `INC0010294`). Waiting until all open alerts have cleared plus the 3-minute quiet period covers Apigee's documented 1-3 minute metric lag, so the ingestion window has stabilized before incident state is archived. The agent's recovery note is not covered by time: the reset waits for the groups' terminal state instead. Raise it with `--quiet-period` if you still see late alerts.
6. **Archives active OKF files:** Only after all alerts have closed, every incident group is terminal and the quiet period has elapsed does the script move active incident files from `gs://<bucket>/knowledge/incident-manager/*.md` to `archive/` and remove leftover root copies.
7. **Verifies healthy baseline:** Confirms HTTP 200 health responses across all services and verifies 0 active incident groups on the bridge.
8. **Preflights admin endpoints:** GETs the fault injection endpoints (`/admin/fault/inject` and `/admin/leak-connections`) on the pinned inventory revision. Only HTTP 2xx passes; 401/403, 404, connection errors and other codes fail the reset with a clear message.
9. **Restocks the demo items (step 5b, before the alert wait):** `POST /admin/restock` on the pinned revision sets every item in `ATA_INVENTORY_RESTOCK_BASELINE` (default `item-101`..`item-105` = 1000 each) to that absolute stock. The reset then checks the response and reads the stock back with `GET /items`. Scenario traffic used to drain the stock across runs, until `POST /orders` returned HTTP 400. Any failure stops the reset. A 404 means the serving revision predates the restock endpoint: re-pin (DEPLOY.md, "Re-pinning the inventory baseline"). `./scripts/reset-scenarios.sh --restock-only` runs just the pinned-baseline checks and the restock.

### CLI Options & Flags

| Flag | Description | Default |
| :--- | :--- | :--- |
| `--no-wait` | Skips polling Cloud Monitoring, the incident group state check and the quiet period for quick developer resets (also skips the alert query, so Monitoring/TLS errors cannot fail the reset). **Risky:** it archives groups the agent is still working on, e.g. a pending recovery note | `false` |
| `--ignore-group <file\|id>` | Leaves one incident group (file name `incident-<id>.md` or group id; repeatable; env `ATA_RESET_IGNORE_GROUPS`) out of the terminal-state wait only, for a group that will never become terminal. The alert wait, the quiet period and the wait for every other group still run. **Risky:** the group is archived in whatever state it is in; the reset warns with its name and status | none |
| `--quiet-period <duration>` | Quiet period window to maintain after all open alerts close, running alongside the wait for terminal incident groups (e.g. `180s`, `3m`); covers Apigee's 1-3 min metric lag | `180s` (`3m`) |
| `--timeout <duration>` | Maximum time to wait for open alerts to clear and incident groups to reach a terminal state before failing with the still-open alerts or active groups listed | `1800s` (`30m`; S4's reset can take up to ~27 min) |
| `--poll-interval <duration>`| Interval between Cloud Monitoring alert queries and incident group state reads | `15s` |
| `--monitoring-token-source <adc\|gcloud>` | Token for the alert check: `adc` = `gcloud auth application-default print-access-token`, `gcloud` = `gcloud auth print-access-token`. Only the chosen source is tried; if it fails the reset fails (run `gcloud auth application-default login`, or use `--no-wait`). Env: `ATA_MONITORING_TOKEN_SOURCE` | `adc` |
| `--no-restock` | Skips the restock with a warning (e.g. after `repin-inventory.sh rollback` to code without `/admin/restock`, where the restock would fail with 404) | `false` |
| `--restock-only` | Only restocks the demo items (`POST /admin/restock` on the pinned revision, verified with `GET /items`) to `ATA_INVENTORY_RESTOCK_BASELINE`; no fault reset, no alert wait | `false` |
| `--thorough` | After any slow (>= 2000 ms) demo request, wait the full `--alert-lag` window (600s) even when the live latency retest window shows the request cannot open an alert (env `ATA_RESET_THOROUGH=true`). Adds about 7 min after a cold start | `false` |
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

**Ctrl-C stops the whole run.** A Ctrl-C while `run-all-scenarios.sh` runs stops the current scenario's traffic early. That scenario finishes as it does when run on its own: its next steps are printed (and logged), and its fault stays injected. The driver then runs no further scenario and does **not** reset the environment. The summary marks the interrupted scenario `INTERRUPTED` and the remaining ones `SKIPPED`, and the driver exits with status 130. **Run `./scripts/reset-scenarios.sh` afterwards**, before you run any scenario again. A Ctrl-C during the driver's own resets (before the first scenario or between scenarios) or its preflight stops the run the same way, and that reset may be incomplete: run the reset yourself in that case too.

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

# Re-check an earlier scenario after later ones ran: bound the window on both sides
./scripts/scenarios/check-result.sh --since "<S1 start>" --until "<S2 start>" --scenario s1
```

`check-result.sh` reads both `knowledge/incident-manager/` and `archive/` (the reset archives a scenario's groups, so a check after the reset still finds them). With `--since`, a file's time is its `timestamp`, `updated_at` or `created_at`, else its latest alert `started_at`, else the creation stamp in its `group_id`, else (active files only) the object's GCS update time; the GCS time of an archived file is the reset's move, not the incident. A file whose time cannot be determined is not counted: the script prints `WARNING ... undetermined time; excluded from --since window` naming it. An invalid `--since` is an error.

`--until <time>` (optional) is the upper bound. Without it, a check is meaningful only right after its scenario: re-checking S1 after S3 ran also counts S3's groups (Origin and Ticket then name the newest group). `--until` compares the time the group **opened** (its first alert `started_at`, else the creation stamp in its `group_id`), not its last update, so a group updated later (recovery note, archive) still belongs to its scenario. A file excluded by `--until` is logged as `INFO ... opened <time> (...), after --until; excluded`; a file with no opening time is not counted, with `WARNING ... opening time undetermined; excluded from --until window`. An invalid `--until`, or one before `--since`, is an error. Use the next scenario's start time as `--until`.

### 2. View Raw GCS OKF Incident Files
Inspect active incident Markdown files stored in Cloud Storage under `knowledge/incident-manager/`:
```bash
gcloud storage ls -l "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/knowledge/incident-manager/"
gcloud storage cat "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/knowledge/incident-manager/<incident-filename>.md"
```

Archived incidents from scenario resets are moved to:
```bash
gcloud storage ls -l "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/archive/"
```

Each file contains YAML frontmatter:
```yaml
---
type: incident
group_id: grp_20261022t220216z_ata-inventory-api
project: <project-id>
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
