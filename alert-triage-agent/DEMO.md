# Alert Triage Agent: Operator & Evaluator Demonstration Guide

This guide is for SRE operators, site leads, and evaluators demonstrating the capabilities of the **Alert Triage Agent (ATA)** across 5 realistic failure scenarios (**S1 through S5**).

Resource names use the default prefix `ata` (`ata-inventory-api`, `ata-orders-v1`, `/ata/inventory/v1`). For a stack deployed with another `NAME_PREFIX`, substitute accordingly. Commands use `${PROJECT_ID}`, `${REGION}`, and `${NAME_PREFIX}`. To deploy a stack, see [DEPLOY.md](DEPLOY.md).

---

## 0. One-Time Setup (macOS / Linux)

### Prerequisites
- macOS `/bin/bash` 3.2+ or Linux bash; `curl`, `python3`. PyYAML is optional (check-result uses a built-in parser if PyYAML is not installed).
- GCP user account with `roles/run.invoker` on `ata-inventory-api` and `ata-orders-api`, `roles/apigee.readOnly` (or admin), `roles/monitoring.viewer`, and object read/write on `gs://${PROJECT_ID}-${NAME_PREFIX}-incidents`.
- Authenticate with your individual user credentials. Do NOT use service account impersonation (`--impersonate-service-account`).

```bash
# 1. Authenticate with Google Cloud (both logins required; ADC is needed for Monitoring Alerts API)
gcloud auth login
gcloud auth application-default login
gcloud config set project cloud-assist-fde-2

# 2. Check out repository (main branch)
git clone <repo-url> && cd autocloud-prototypes
git checkout main && git pull
cd alert-triage-agent                   # ALL subsequent commands run from alert-triage-agent/

# 3. Environment configuration
export PROJECT_ID=cloud-assist-fde-2
export REGION=us-west1
export NAME_PREFIX=ata
export SSL_CERT_FILE=/etc/ssl/cert.pem  # macOS: prevents TLS CERTIFICATE_VERIFY_FAILED in alert checks
```

### Apigee Configuration
The scripts automatically read `.${NAME_PREFIX}-apigee.env` and `.${NAME_PREFIX}-api-key` from `alert-triage-agent/`. In a fresh clone:

```bash
# Apigee environment settings
cat > .ata-apigee.env <<'EOF'
APIGEE_HOST=8.233.247.52.nip.io
APIGEE_ORG=cloud-assist-fde-2
APIGEE_ENV=prod
EOF

# Fetch Apigee consumer API key (mode 600; export ATA_API_KEY=... overrides this file)
curl -s -H "Authorization: Bearer $(gcloud auth application-default print-access-token)" \
  "https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG:-cloud-assist-fde-2}/developers/${NAME_PREFIX:-ata}-dev@example.com/apps/${NAME_PREFIX:-ata}-demo-app" \
  | python3 -c "import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])" > ".${NAME_PREFIX:-ata}-api-key.tmp" \
  && test -s ".${NAME_PREFIX:-ata}-api-key.tmp" && mv ".${NAME_PREFIX:-ata}-api-key.tmp" ".${NAME_PREFIX:-ata}-api-key"
chmod 600 ".${NAME_PREFIX:-ata}-api-key"
```

---

## 1. Before the Demo (Pre-Demo Checklist)

Run through this checklist before presenting:

1. **Wake ServiceNow PDI:**
   Free ServiceNow Personal Developer Instances hibernate after inactivity. Log into [developer.servicenow.com](https://developer.servicenow.com) and wake your instance before starting. Verify access to the ServiceNow UI (e.g. `https://<instance>.service-now.com` -> Incidents > All).
2. **Verify Baseline Infrastructure:**
   ```bash
   ./scripts/verify-demo-infra.sh
   ```
   Verifies Cloud Run health and Apigee routes (`/ata/inventory/v1`, `/ata/orders/v1`). If it warns about a slow request (>2000 ms) or 5xx from cold start, add `--alert-window-from-start` to the reset below.
3. **Reset to Clean State:**
   ```bash
   ./scripts/reset-scenarios.sh
   ```
   Wait for exit 0 (`Environment Healthy`, `Verified 0 active incident groups`). Reset duration is `226–234 s typical (measured live, 2026-10-28)` (~4 min).
4. **Preflight Health Verification:**
   ```bash
   ./scripts/scenarios/preflight.sh
   ```
   Expect `All Preflight Health Checks Passed`.
   *(If preflight fails with HTTP 500 "no available instance", run `./scripts/reset-scenarios.sh --alert-window-from-start` and retry; see Troubleshooting).*

---

## 2. Per-Scenario Cards (S1 through S5)
<a id="12-scenario-by-scenario-guide-s1-through-s5"></a>

Execute scenarios sequentially. Always run `./scripts/reset-scenarios.sh` between scenarios to clear faults and allow metric lag windows to settle. Press `Ctrl-C` at any time to safely stop traffic early and view next steps.

```
Scenario Loop:
[reset-scenarios.sh] ---> [Run Scenario (--hold 7m)] ---> [check-result.sh (~+9-11m)] ---> [Verify ServiceNow]
```

---

### S1: Backend 5xx Error Spike (`ata-inventory-api`, 85% HTTP 500)

- **What it shows:** Upstream root cause attribution during cascading failure. When `ata-inventory-api` returns HTTP 500, downstream `ata-orders-api` fails with 502 Bad Gateway. ATA deduplicates alerts and correctly attributes the single incident upstream to `ata-inventory-api`.
- **Command:**
  ```bash
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ./scripts/scenarios/s1_backend_5xx.sh --hold 7m
  ```
- **What to say while waiting (~7 min hold):**
  - "We are injecting an 85% HTTP 500 fault into `ata-inventory-api` via `/admin/fault/inject` while driving realistic API traffic through Apigee."
  - "Notice that `/admin/fault` endpoints are strictly suppressed and invisible in Cloud Logging, ensuring ATA relies solely on genuine production telemetry."
  - "Downstream orders service also fails with 502 Bad Gateway. Rather than triggering duplicate tickets for each service, ATA's topology-aware correlation engine groups these alerts into one incident."
- **Time to Ticket:** First alert at **6m23s**; ticket created at **8m38s** (INC0010356). Alerts close ~6m18s after traffic ends; recovery work note arrives +3m00s later.
- **Expected Ticket & Fields:**
  - **Count:** 1 ticket. **Title:** `[P1] ata-inventory-api: Elevated HTTP 5xx error rate (2 alerts)`
  - **SN Priority:** `1 - Critical`. **State:** `In Progress`. **Origin:** `ata-inventory-api`.
  - **Issue Type:** `runtime_anomaly`.
  - **Change-Induced:** `No (no deployment or configuration change on ata-inventory-api or its upstream dependencies in the 2 h before the alert; checked: ...)`.
  - **Alerts (2):** `ata-inventory-5xx` and `ata-orders-5xx` (no latency alert).
  - **Blast Radius:** `ata-inventory-api, ata-inventory-v1, ata-orders-api, ata-orders-v1`.
  - **Work Notes:**
    - "Origin selection: ... ata-orders-api 5xx explained by upstream ata-inventory-api".
    - Action 1: "no recent change on ata-inventory-api is implicated, so no traffic change is recommended".
    - Measured 5xx rates (~82% on inventory and orders); DB connection pool normal (3/25).
    - Scoped Apigee line: notes no deployments of relevant proxies in 2h window.
    - Attached self-contained diagnostic HTML investigation report.

---

### S2: Backend Latency Degradation (`ata-inventory-api` +6500 ms → Apigee 504)

- **What it shows:** Identifies origin latency behind gateway timeouts. An artificial 6500 ms delay exceeds the 5000 ms Apigee target timeout, returning HTTP 504 GatewayTimeout. ATA identifies the Cloud Run backend delay as the true cause.
- **Command:**
  ```bash
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ./scripts/scenarios/s2_backend_latency.sh --hold 7m
  ```
- **What to say while waiting (~7 min hold):**
  - "We inject a 6500 ms latency delay into `ata-inventory-api`. The Apigee proxy target timeout is 5000 ms, causing Apigee to drop requests with HTTP 504."
  - "Cloud Monitoring latency alert policies use a 240 s retest window (`latency_duration_secs=240`) to ensure single cold starts don't cause false alarms."
  - "Because of metric lag and the retest window, latency alerts open shortly after the 7 min traffic stops. The ticket arrives ~4 min after traffic ends."
- **Time to Ticket:** First alert at **9m02s** (1m51s after traffic ends); ticket created at **11m20s** (INC0010357). Recovery note at +3m00s after alerts close.
- **Expected Ticket & Fields:**
  - **Count:** 1 ticket. **Title:** `[P2] ata-inventory-api: Elevated latency (2 alerts)`
  - **SN Priority:** `2 - High` (P2 -> Urgency 1, Impact 2 -> `2 - High`).
  - **Origin:** `ata-inventory-api`. **Issue Type:** `latency_spike`. **Change-Induced:** `No (...)`.
  - **Alerts (2):** `ata-inventory-latency` and `ata-orders-latency`.
  - **Blast Radius:** `ata-inventory-api, ata-inventory-v1, ata-orders-api, ata-orders-v1`.
  - **Root Cause:** Cites peak latency (~6525 ms) and HTTP 504 faults (`messaging.adaptors.http.flow.GatewayTimeout`) on `ata-inventory-v1` (live: ~382 requests); states requests did not complete within the proxy target timeout without guessing unmeasured values.

---

### S3: Concurrent Disjoint Incidents (S1 Fault on Inventory + SpikeArrest 1pm on Orders)

- **What it shows:** Disjoint incident separation. Proves ATA's correlation engine splits concurrent, unrelated failures into two separate incident groups and tickets rather than falsely merging them.
- **Command:**
  ```bash
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ./scripts/scenarios/s3_concurrent_disjoint.sh --hold 7m
  ```
- **What to say while waiting (~7 min hold):**
  - "We run two unrelated failures simultaneously: an 85% 5xx fault on `ata-inventory-api` and deploying a strict SpikeArrest rate limit (`Rate=1pm`) on `ata-orders-v1`."
  - "Legacy AIOps tools either trigger an alert storm or merge concurrent alerts across different services into one messy incident."
  - "ATA computes a topological and temporal correlation score (0–100). Because the score is below the 35-point threshold, it isolates the failure into an independent group."
- **Time to Ticket:** Inventory ticket at **9m03s**; Orders ticket at **10m33s** (INC0010360 + INC0010361). Wait ~11 min before checking results.
- **Expected Tickets & Fields (EXACTLY 2 DISTINCT TICKETS):**
  - **Ticket 1 (Inventory Group):**
    - **Title:** `[P1] ata-inventory-api: Elevated HTTP 5xx error rate (1 alert)`
    - **SN Priority:** `1 - Critical`. **Origin:** `ata-inventory-api`. **Issue Type:** `runtime_anomaly`. **Change-Induced:** `No (...)`.
    - **Alert:** `ata-inventory-5xx` only (orders traffic is throttled at Apigee).
    - **Scoped Apigee Line:** Notes 1 other proxy changed in window without attributing it to inventory.
  - **Ticket 2 (Orders Group):**
    - **Title:** `[P2] ata-orders-v1: Rate limit exceeded (HTTP 429) (1 alert)`
    - **SN Priority:** `2 - High`. **Origin:** `ata-orders-v1`. **Issue Type:** `change_induced`.
    - **Change-Induced:** `Yes (Apigee proxy ata-orders-v1 revision <new> first served ..., replacing revision <prev>; access log: revision <new> served ... requests with ... SpikeArrest faults, revision <prev> served ... requests with none)`.
    - **Action 1:** Rolls back proxy to `<prev>` (`gcloud apigee apis deploy <prev> --api=ata-orders-v1 ... --override`).
    - **Correlation Note:** Notes best correlation score (16.73 pts) against open group was below the 35 pts threshold.
    - **HTML Report:** Cloud Run rows say `not applicable (Apigee proxy)`.

---

### S4: Database Connection Pool Exhaustion (`ata-inventory-db`)

- **What it shows:** Database root cause attribution. Traces downstream inventory service degradation to PostgreSQL connection exhaustion on Cloud SQL, distinguishing DB pool saturation from application code bugs.
- **Command:**
  ```bash
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ./scripts/scenarios/s4_db_connection_pool.sh --hold 7m
  ./scripts/reset-scenarios.sh   # START IMMEDIATELY AFTER HOLD: leaked connections persist until reset!
  ```
- **What to say while waiting (~7 min hold):**
  - "We call `/admin/leak-connections` on `ata-inventory-api` to leak PostgreSQL backend connections on Cloud SQL (`ata-inventory-db`)."
  - "Because leaked connections persist in memory on Cloud Run, the Cloud SQL alert does not auto-close when traffic stops. Starting the reset releases the leak."
  - "ATA identifies Cloud SQL as the earliest onset origin component rather than blaming the Cloud Run web server."
- **Time to Ticket:** First alert at **7m27s**; ticket created at **9m48s** (INC0010362, during reset). Reset takes ~10–11 min (live: 629 s; up to about 10–11 min after S4) as it releases connections, waits for the alert to close, and waits for the recovery note.
- **Expected Ticket & Fields:**
  - **Count:** 1 ticket. **Title:** `[P1] ata-inventory-db: Database connection pool saturation (1 alert)`
  - **SN Priority:** `1 - Critical`. **Origin:** `ata-inventory-db`. Alert: `ata-cloudsql-connections-critical` only.
  - **Issue Type:** `runtime_exhaustion`. **Change-Induced:** `No (...)`.
  - **Blast Radius:** `ata-inventory-db, ata-inventory-api, ata-inventory-v1, ata-orders-api, ata-orders-v1`.
  - **Work Notes & Actions:**
    - Identifies connections at 24/25. Notes "(no HTTP 5xx errors measured on ata-inventory-api, ata-orders-api; HTTP 5xx rate not measured on ata-inventory-v1, ata-orders-v1)".
    - Action 1: Inspect connection pool release logic in `ata-inventory-api` (read-only log read).
    - Action 2: `SELECT pg_terminate_backend(pid)` marked **"Mutating, operator judgement required"** (only after review).
    - Action 3: Read-only `gcloud sql instances describe` (no destructive flag patch).
    - HTML report: Cloud Run rows say `not applicable (Cloud SQL)`.

---

### S5: Bad Deploy / Config Failure (`ata-inventory-api` invalid DB user) — Run Last

- **What it shows:** Change-induced failure detection & smart rollback. Detects an invalid IAM database user deployed to Cloud Run, attributes failure to the new revision, and identifies the exact previously *serving* revision to rollback to from traffic history.
- **Command:**
  ```bash
  START=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  ./scripts/scenarios/s5_bad_deploy.sh --hold 7m
  ```
- **What to say while waiting (~7 min hold):**
  - "We deploy a new Cloud Run revision (`ata-inventory-api-s5-<epoch>`) configured with an invalid database username (`invalid-iam-user`)."
  - "ATA recognizes this as change-induced because the alert fired within 15 min of deploy (`CHANGE_ONSET_WINDOW_MINUTES=15`) and the revision had 0 healthy minutes."
  - "ATA inspects Cloud Monitoring traffic history to recommend rolling back to the revision that was actually *serving* traffic (e.g. `ata-inventory-api-pin-...`), avoiding broken 0%-traffic revisions."
- **Time to Ticket:** First alert at **7m32s**; ticket created at **10m05s** (INC0010363). Recovery note at +3m00s after alerts close.
- **Expected Ticket & Fields:**
  - **Count:** 1 ticket. **Title:** `[P1] ata-inventory-api: Deployment regression: database authentication failure (2 alerts)`
  - **SN Priority:** `1 - Critical`. **Origin:** `ata-inventory-api`.
  - **Issue Type:** `change_induced`.
  - **Change-Induced:** `Yes (revision ata-inventory-api-s5-<epoch> on ata-inventory-api deployed ... (7m before the alert); ata-inventory-api degraded (alert ata-inventory-5xx fired on it) with no degraded upstream dependency)`.
  - **Work Notes & Actions:**
    - Identifies database authentication failure in application logs.
    - Action 1: Recommends `gcloud run services update-traffic ata-inventory-api --to-revisions=<serving-pin>=100` targeting the confirmed serving revision from lookback traffic history. Never uses `--to-latest`.
    - Distinguishes auth configuration failure from connection exhaustion (DB 2/25 normal).
  - **Revert:** `./scripts/scenarios/s5_bad_deploy.sh --revert` restores the pinned revision (the reset also restores it). Always run S5 last or reset immediately after.

---

## 3. After Each Scenario (Validation & Reset)
<a id="4-resetting-faults-between-scenarios"></a>

### 1. Validate Results with `check-result.sh`
Run the result checker right after each scenario (approx. 9–11 min from start):

```bash
# Check current scenario results
./scripts/scenarios/check-result.sh --since "$START" --scenario sN

# To re-check an earlier scenario after later ones have run, bound with --until:
./scripts/scenarios/check-result.sh --since "<S1_START>" --until "<S2_START>" --scenario s1
```
`check-result.sh` inspects active and archived incident markdown files in GCS (`gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/`). Look for `[MATCH]` across Group Count, Origin, Ticket Number, Alerts, and Root Cause.

### 2. Reset the Environment
```bash
./scripts/reset-scenarios.sh
```
- Restores healthy Apigee proxy bundles and Cloud Run pinned revisions.
- Restocks demo catalog items (`POST /admin/restock` sets `item-101`..`item-105` to 1000).
- Concurrently waits for open alerts to close, the 180s quiet period, and incident groups to become terminal (`recovered` with recovery note posted).
- Archives terminal incident groups to `gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/archive/`.
- **Duration:**
  - S1, S2, S3, S5: **226–234 s typical (measured live, 2026-10-28)** (~4 min).
  - S4: **up to about 10–11 min after S4** (629 s measured; waits for released connections and alert lifecycle).
  - Use `--thorough` if you want the reset to wait the full 600s alert window after any slow request.

### 3. Verify ServiceNow Record
- Ticket State remains `In Progress`: ATA does not auto-resolve tickets (`AUTO_RESOLVE_ON_RECOVERY=false`).
- Each ticket contains exactly 2 work notes: initial investigation summary (with attached HTML report) and the recovery note (posted 180s after alerts close).
- Manually resolve the ticket in ServiceNow after verifying remediation.

*(Optional)* **Automated Suite Driver:**
```bash
./scripts/run-all-scenarios.sh --priority    # Core suite: S4, S1, S3, S5
./scripts/run-all-scenarios.sh --all         # Full suite: S1 through S5
```

---

## 4. Troubleshooting & Gotchas

| Issue / Symptom | Root Cause | Solution |
| :--- | :--- | :--- |
| **Preflight 500 "no available instance"** | Demo services capped at 1 instance (`max-instances=1`); instance turnover can briefly 500. | Run `./scripts/reset-scenarios.sh --alert-window-from-start`, then retry preflight. |
| **ServiceNow PDI Hibernating** | Free ServiceNow developer PDIs sleep after inactivity; bridge cannot create tickets. | Log in to [developer.servicenow.com](https://developer.servicenow.com) and wake the instance before the demo. |
| **gcloud auth 401 `ACCESS_TOKEN_TYPE_UNSUPPORTED`** | Google APIs (Apigee management API, Monitoring Alerts API) reject plain user tokens; scripts use ADC tokens by default (`ATA_TOKEN_SOURCE=adc`). | Run `gcloud auth application-default login`. |
| **Identity token empty / Cloud Run 403** | `CLOUDSDK_AUTH_ACCESS_TOKEN` is set in the environment or user lacks invoker role. | Run `unset CLOUDSDK_AUTH_ACCESS_TOKEN` and ensure `roles/run.invoker` is granted. |
| **TLS `CERTIFICATE_VERIFY_FAILED` on macOS** | Python on macOS does not load system keychain root certificates. | Run `export SSL_CERT_FILE=/etc/ssl/cert.pem`. |
| **Stale OPEN alerts listed in reset output** | Old latency alerts from non-serving revisions remain open in Cloud Monitoring. | Harmless: reset lists `Ignored N stale/non-serving open alert(s)`. Close in GCP Console if desired. |
| **Cloud Logging read quota exceeded** | Project quota is 200 reads/min; shared project burst causes `Change-Induced: Not assessed`. | Expected behavior when quota exceeded: agent does not guess. Reset, wait 2–3 min, retry scenario. |
| **Deploy within `CHANGE_WINDOW_MINUTES`** | Deploys in the 120m window before alert onset are evaluated for change attribution. | Onset within 15m or 0 healthy minutes attributes cause; older deploys with healthy traffic listed under "Recent changes (likely unrelated)". |
| **S5 revision churn** | S5 creates a new serving revision; revert creates a 0%-traffic revision. | Always run S5 last, or run `./scripts/reset-scenarios.sh` immediately after it. |
| **Ctrl-C during scenario** | Stops traffic early; fault remains injected. | Run `./scripts/reset-scenarios.sh` to clean up before running another scenario. |
