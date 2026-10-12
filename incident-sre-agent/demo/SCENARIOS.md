# Incident SRE Agent — Failure Scenarios Reference

This document provides complete specifications for the 5 demonstration failure scenarios for `incident-sre-agent`. Each scenario includes the fault injection trigger, expected telemetry signatures across Cloud Logging and Cloud Monitoring, incident filing templates for ServiceNow and Jira, and expected SRE Agent diagnostic findings and remediation actions.

> [!IMPORTANT]
> **Live Demo Operational Guidelines**:
> 1. **Clean Baseline ($\ge 60$ Minutes)**: Ensure at least 60 minutes of clean, uninterrupted healthy baseline traffic prior to testing any scenario (or reset between runs and allow 60 minutes to elapse). Because the SRE Agent uses a dynamic investigation lookback window (defaulting to 60 minutes prior to incident opening), running scenarios in rapid succession introduces residual error groups from earlier scenarios (lookback window contamination).
> 2. **Stabilization After Injection ($\ge 5$ Minutes)**: After injecting a fault, wait at least 5 full minutes under active traffic before filing the incident ticket. In Cloud Run, metrics aggregation (e.g. `request_latencies` and `request_count`) and Error Reporting error group aggregation have ingestion delays of 1–3 minutes. Filing tickets prematurely (< 2 minutes after injection) causes the investigation window to close before telemetry is fully ingested, resulting in false negatives or lookback window confusion.
> 3. **Symptom-Only Ticket Text**: Tickets filed in ServiceNow or Jira MUST describe symptoms only from the customer or operator perspective (e.g., "Customers getting errors at checkout on shop-api", "shop-api very slow, checkout timing out"). Never leak root cause mechanisms, scenario numbers ("Scenario 1"), internal function names, or secret keys in ticket descriptions. Real SRE agents must discover the causal mechanism autonomously through telemetry correlation, not from ticket hints.

---

## Scenario 1: Error Spike (Checkout Exceptions)

### 1. Overview & Root Cause
A sudden surge in application exceptions occurs on the `/checkout` endpoint of `shop-api` due to database connection pool exhaustion or simulated payment processing failures. Approximately 40% of checkout attempts fail with HTTP 500.

### 2. Fault Injection & Trigger
- **Trigger Script**:
  ```bash
  bash demo/scripts/inject_fault.sh 1
  ```
- **API Call**:
  ```http
  POST /fault/inject
  Content-Type: application/json

  {"scenario": 1, "error_rate": 0.40}
  ```
- **Reset**:
  ```bash
  bash demo/scripts/reset.sh
  ```

### 3. Expected Telemetry Signals
- **Cloud Logging**:
  - Filter: `resource.type="cloud_run_revision" AND resource.labels.service_name="shop-api" AND severity>=ERROR`
  - Error Signature:
    ```text
    DatabaseConnectionError: Connection pool exhausted at shop-db:5432 (active: 100, max: 100).
    Failed to acquire lock for checkout transaction.
    Traceback (most recent call last):
      File "/app/shop_app.py", line 210, in checkout
        db_conn = await pool.acquire(timeout=5.0)
    asyncpg.exceptions.TooManyConnectionsError: connection limit reached
    ```
  - HTTP 500 response logs in Cloud Run request log: `httpRequest.status=500`.
- **Cloud Monitoring**:
  - `run.googleapis.com/request_count`: 5xx response count spikes to ~40% of `/checkout` traffic.
  - Overall service availability drops below the 99.9% SLO threshold.
- **Audit Logs / Changes**:
  - No new deployments or IAM changes correlated with the onset.

### 4. Incident Filing Template
- **ServiceNow**:
  - **Short Description**: `Customers getting errors at checkout on shop-api`
  - **Description**: `Elevated HTTP 500 error rates detected on shop-api /checkout endpoint (~40% failures). Customers reporting failed orders with internal server errors.`
  - **Priority**: `1` (Critical) | **CMDB CI**: `shop-api`
- **Jira Cloud**:
  - **Summary**: `Customers getting errors at checkout on shop-api`
  - **Priority**: `Highest` | **Component**: `shop-api`
- **CLI Command**:
  ```bash
  python demo/scripts/create_incident.py --target snow --scenario 1
  ```

### 5. Expected Agent Findings & Remediation
- **Logs Specialist**: Identifies recurring `DatabaseConnectionError` and `TooManyConnectionsError` stack traces originating in checkout handler.
- **Metrics Specialist**: Detects 5xx error rate spike on `shop-api` exceeding baseline.
- **Probable Root Cause**: Application exception: Database connection saturation on `shop-db` during `/checkout` transactions.
- **Recommended Actions**:
  1. Inspect active database connection pool settings and scale connection limit or enable PgBouncer / Cloud SQL Auth Proxy connection pooling.
  2. Examine slow queries holding database transactions open.
  3. Verify payment processing timeouts on third-party gateways.

---

## Scenario 2: Latency Spike (Artificial Delay)

### 1. Overview & Root Cause
Artificial latency (3.0s to 8.0s) is injected across product browsing (`/catalog`) and checkout (`/checkout`) endpoints. Requests remain active for several seconds, exhausting concurrency limits and breaching the 500ms p95 latency SLO.

### 2. Fault Injection & Trigger
- **Trigger Script**:
  ```bash
  bash demo/scripts/inject_fault.sh 2
  ```
- **API Call**:
  ```http
  POST /fault/inject
  Content-Type: application/json

  {"scenario": 2, "latency_seconds": 5.0}
  ```
- **Reset**:
  ```bash
  bash demo/scripts/reset.sh
  ```

### 3. Expected Telemetry Signals
- **Cloud Monitoring**:
  - `run.googleapis.com/request_latencies`: p95 and p99 latencies jump from ~50ms to 4,000ms–8,000ms.
  - Latency SLO breach: p95 latency > 500ms alert fires.
  - `run.googleapis.com/container/instance_count`: Autoscaler scales up instances attempting to accommodate concurrent requests.
- **Cloud Logging**:
  - Request log entries reflect high `httpRequest.latency` (e.g. `5.234s`).
  - Zero application-level delay injection logs are emitted; telemetry relies solely on request metrics and latency distributions.
- **Audit Logs / Changes**:
  - Autoscaler revision adjustments; no unexpected administrative updates.

### 4. Incident Filing Template
- **ServiceNow**:
  - **Short Description**: `shop-api very slow, checkout timing out`
  - **Description**: `Significant latency spike observed on shop-api. P95 latency degraded to 4.5s-8.0s on /catalog and /checkout endpoints exceeding 500ms SLO threshold.`
  - **Priority**: `2` (High) | **CMDB CI**: `shop-api`
- **Jira Cloud**:
  - **Summary**: `shop-api very slow, checkout timing out`
  - **Priority**: `High` | **Component**: `shop-api`
- **CLI Command**:
  ```bash
  python demo/scripts/create_incident.py --target jira --scenario 2
  ```

### 5. Expected Agent Findings & Remediation
- **Metrics Specialist**: Confirms anomalous p95/p99 request latency degradation exceeding the 500ms threshold.
- **Logs Specialist**: Correlates latency entries across `/catalog` and `/checkout`.
- **Probable Root Cause**: Extreme latency degradation in request processing pipeline driving instance saturation.
- **Recommended Actions**:
  1. Profile synchronous endpoints and downstream dependency response times.
  2. Verify concurrency limits on Cloud Run revision (increase `container_concurrency` or min instances).
  3. Inspect upstream CDN / caching configurations for catalog routes.

---

## Scenario 3: Bad Release (Missing Required Environment Variable)

### 1. Overview & Root Cause
A new revision of `shop-api` is deployed missing the mandatory configuration environment variable `PAYMENT_GATEWAY_KEY`. The service readiness probe (`/readyz`) fails with HTTP 503 Service Unavailable, and checkout requests fail immediately.

### 2. Fault Injection & Trigger
> [!IMPORTANT]
> **Fault Injection Mechanism Note**:
> Scenario 3 currently uses an **in-app fault injection switch** (`POST /fault/inject {"scenario": 3}`) in `shop-api` that simulates the missing `PAYMENT_GATEWAY_KEY` configuration and readiness probe failure. In a production scenario, this represents an actual Cloud Run revision deployment with a missing environment variable or failed readiness check. Automated fault injection via real Cloud Run revision deployment (`gcloud run deploy --update-env-vars`) is recorded as a priority item in the engineering backlog.

- **Trigger Script**:
  ```bash
  bash demo/scripts/inject_fault.sh 3
  ```
- **API Call**:
  ```http
  POST /fault/inject
  Content-Type: application/json

  {"scenario": 3}
  ```
- **Reset**:
  ```bash
  bash demo/scripts/reset.sh
  ```

### 3. Expected Telemetry Signals
- **Audit Logs (Cloud Audit Admin Activity)**:
  - `protoPayload.methodName="google.cloud.run.v2.Services.UpdateService"` or `CreateRevision`.
  - Deployment timestamp matches the onset of failures.
- **Cloud Logging**:
  - Filter: `resource.type="cloud_run_revision" AND severity>=ERROR`
  - Error Signature:
    ```text
    Missing required environment variable: PAYMENT_GATEWAY_KEY
    Service Unavailable: Missing required environment variable PAYMENT_GATEWAY_KEY
    ```
  - Readiness probe failures returning HTTP 503.
- **Cloud Monitoring**:
  - Sudden 100% 503 error rate on `/readyz` and `/checkout`.
  - Revision traffic migration to unhealthy revision.

### 4. Incident Filing Template
- **ServiceNow**:
  - **Short Description**: `shop-api broken after this morning's release`
  - **Description**: `shop-api returning HTTP 503 Service Unavailable following latest deployment revision. Readiness probe failing due to missing required configuration PAYMENT_GATEWAY_KEY.`
  - **Priority**: `1` (Critical) | **CMDB CI**: `shop-api`
- **Jira Cloud**:
  - **Summary**: `shop-api broken after this morning's release`
  - **Priority**: `Highest` | **Component**: `shop-api`
- **CLI Command**:
  ```bash
  python demo/scripts/create_incident.py --target snow --scenario 3
  ```

### 5. Expected Agent Findings & Remediation
- **Changes Specialist**: Identifies Cloud Run service update / revision deployment shortly before incident opened.
- **Logs Specialist**: Locates explicit error message indicating `PAYMENT_GATEWAY_KEY` is missing.
- **Probable Root Cause**: Regression introduced by recent deployment revision: missing mandatory environment variable `PAYMENT_GATEWAY_KEY`.
- **Recommended Actions**:
  1. Roll back traffic immediately to the previous healthy Cloud Run revision:
     ```bash
     gcloud run services update-traffic shop-api --to-revisions PREVIOUS_REVISION=100
     ```
  2. Restore missing `PAYMENT_GATEWAY_KEY` in Cloud Run secret or environment configuration.

---

## Scenario 4: Memory Leak Leading to OOM Crash

### 1. Overview & Root Cause
Each incoming request allocates a 10MB byte array to an unreleased in-memory buffer in `shop-api`. As continuous traffic arrives, memory utilization reaches 100% of the container memory limit (1Gi / 512Mi), triggering kernel Out Of Memory (OOM) killer terminations and container restarts with HTTP 503 errors.

### 2. Fault Injection & Trigger
- **Trigger Script**:
  ```bash
  bash demo/scripts/inject_fault.sh 4
  ```
- **API Call**:
  ```http
  POST /fault/inject
  Content-Type: application/json

  {"scenario": 4}
  ```
- **Reset**:
  ```bash
  bash demo/scripts/reset.sh
  ```

### 3. Expected Telemetry Signals
- **Cloud Monitoring**:
  - `run.googleapis.com/container/memory/utilizations`: Linear increase from baseline (15%) towards 100%.
  - Memory limit exceeded alerts trigger.
- **Cloud Logging**:
  - System logs: `Memory limit of 1024 MiB exceeded with 1032 MiB used. Consider increasing the memory limit.`
  - Container terminated with exit code `137` (SIGKILL / OOM).
  - Request log shows intermittent HTTP 503 during container restart cycle.
- **Resources Specialist**:
  - Detects container restart count increments and memory saturation near threshold.

### 4. Incident Filing Template
- **ServiceNow**:
  - **Short Description**: `shop-api intermittently unavailable (503s)`
  - **Description**: `Container instances of shop-api experiencing progressive memory saturation and crashing with Memory limit exceeded (OOM) leading to intermittent 503s.`
  - **Priority**: `2` (High) | **CMDB CI**: `shop-api`
- **Jira Cloud**:
  - **Summary**: `shop-api intermittently unavailable (503s)`
  - **Priority**: `High` | **Component**: `shop-api`
- **CLI Command**:
  ```bash
  python demo/scripts/create_incident.py --target snow --scenario 4
  ```

### 5. Expected Agent Findings & Remediation
- **Resources Specialist**: Identifies memory saturation exceeding container limits and abnormal instance restarts.
- **Logs Specialist**: Locates Cloud Run infrastructure log `Memory limit exceeded`.
- **Probable Root Cause**: Service failure driven by memory exhaustion / OOM termination causing instance restarts.
- **Recommended Actions**:
  1. Temporarily increase Cloud Run memory limit to prevent immediate crash loops:
     ```bash
     gcloud run services update shop-api --memory 2Gi
     ```
  2. Inspect heap memory profiles and recent commits for unreleased object collections or memory leaks.

---

## Scenario 5: Downstream Dependency Failure (Inventory Service 403 Forbidden)

### 1. Overview & Root Cause
The IAM invoker permission (`roles/run.invoker`) allowing `shop-api-sa` to call the private `inventory-svc` Cloud Run service is revoked, or auth tokens are dropped. All downstream calls to `/inventory` and `/inventory/reserve` fail with HTTP 403 Forbidden, breaking checkout and catalog lookups.

### 2. Fault Injection & Trigger
> [!IMPORTANT]
> **Fault Injection Mechanism Note**:
> Scenario 5 currently uses an **in-app fault injection switch** (`POST /fault/inject {"scenario": 5}`) in `shop-api` that causes the application to omit the Google Cloud Run OIDC ID token when invoking `inventory-svc`, triggering authentic Cloud Run HTTP 403 Forbidden responses. In a production scenario, this fault represents an actual IAM policy modification (`roles/run.invoker` revoked from `shop-api-sa`). Automated fault injection via live IAM policy bindings (`gcloud run services remove-iam-policy-binding`) is recorded as a priority item in the engineering backlog.

- **Trigger Script**:
  ```bash
  bash demo/scripts/inject_fault.sh 5
  ```
- **API Call**:
  ```http
  POST /fault/inject
  Content-Type: application/json

  {"scenario": 5}
  ```
- **Reset**:
  ```bash
  bash demo/scripts/reset.sh
  ```

### 3. Expected Telemetry Signals
- **Cloud Logging**:
  - In `shop-api`:
    ```text
    Downstream dependency inventory-svc returned HTTP 403 Forbidden
    ```
  - In `inventory-svc`:
    - HTTP 403 responses recorded in request logs: `httpRequest.status=403`.
- **Audit Logs (Cloud Audit Admin Activity)**:
  - `SetIamPolicy` on `inventory-svc` removing `serviceAccount:shop-api-sa@...` from `roles/run.invoker`.
- **Cloud Monitoring**:
  - 502 Bad Gateway / 500 error spikes on `shop-api`.
  - 403 Forbidden error counts rise on `inventory-svc`.

### 4. Incident Filing Template
- **ServiceNow**:
  - **Short Description**: `Product availability not loading on shop-api`
  - **Description**: `Downstream inventory verification failing on shop-api with HTTP 403 Forbidden errors when contacting inventory-svc. IAM permission roles/run.invoker appears revoked.`
  - **Priority**: `2` (High) | **CMDB CI**: `shop-api`
- **Jira Cloud**:
  - **Summary**: `Product availability not loading on shop-api`
  - **Priority**: `High` | **Component**: `shop-api`
- **CLI Command**:
  ```bash
  python demo/scripts/create_incident.py --target jira --scenario 5
  ```

### 5. Expected Agent Findings & Remediation
- **Logs Specialist**: Locates downstream 403 Forbidden errors from `inventory-svc`.
- **Changes Specialist**: Identifies IAM policy modification removing `shop-api-sa` from `inventory-svc`.
- **Probable Root Cause**: Authorization failure on downstream dependency: `inventory-svc` returning 403 Forbidden due to missing `roles/run.invoker` IAM binding.
- **Recommended Actions**:
  1. Restore IAM invoker binding on `inventory-svc`:
     ```bash
     gcloud run services add-iam-policy-binding inventory-svc \
       --member="serviceAccount:shop-api-sa@PROJECT_ID.iam.gserviceaccount.com" \
       --role="roles/run.invoker" \
       --region="us-central1"
     ```
  2. Verify service account credentials and token generation inside `shop-api`.
