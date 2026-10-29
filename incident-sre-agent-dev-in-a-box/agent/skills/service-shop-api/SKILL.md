---
name: service-shop-api
description: Service runbook for shop-api demo service, covering architecture, endpoints, dependencies, SLOs, failure modes, and remediation playbooks.
---

# Service Runbook: shop-api

## 1. Service Overview
`shop-api` is the primary customer-facing storefront API workload running as a containerized service on Google Cloud Run in `us-central1`. It powers product catalog exploration, item detail queries, and purchase checkout transactions.

## 2. Architecture & Dependencies
- **Workload Platform**: Google Cloud Run (Service: `shop-api`, Service Account: `shop-api-sa`).
- **Primary Database**: Cloud SQL PostgreSQL instance `shop-db` (private IP via VPC connector).
- **Downstream Services**:
  - `inventory-svc`: Private Cloud Run service requiring IAM authentication (`roles/run.invoker`). Used for live stock validation and reservation locks.
  - `payment-gateway`: Third-party payment capture processor accessed via API key (`PAYMENT_GATEWAY_KEY`).
- **Upstream Callers**: Web and mobile storefronts, API clients, and the continuous load generator `loadgen`.

## 3. Critical Endpoints & Health Probes
- `GET /health`: Basic liveness probe returning HTTP 200 `{"status": "ok", "service": "shop-api"}`.
- `GET /readyz`: Readiness probe verifying mandatory environment configuration (`PAYMENT_GATEWAY_KEY`). Returns HTTP 200 when healthy, or HTTP 503 if configuration is missing.
- `GET /catalog`: Lists product catalog enriched with live inventory quantities from `inventory-svc`.
- `GET /catalog/{item_id}`: Fetches individual item details and current stock availability.
- `POST /checkout`: Executes customer purchases, calls `inventory-svc` to lock stock, and finalizes payment.

## 4. Service Level Objectives (SLOs)
- **Availability SLO**: >= 99.9% non-5xx HTTP responses over a rolling 30-day window.
- **Latency SLO**: 95% (p95) of `/checkout` and `/catalog` requests completed in under 500ms.
- **Error Budget Policy**: P1 alerts trigger if error rate exceeds 5% over 5 minutes or 1% over 30 minutes.

## 5. Diagnostic Queries & Telemetry Cookbook

### Cloud Logging Filters
- **Checkout Application Errors**:
  ```text
  resource.type="cloud_run_revision"
  resource.labels.service_name="shop-api"
  severity>=ERROR
  "checkout"
  ```
- **Readiness & Configuration Failures**:
  ```text
  resource.type="cloud_run_revision"
  resource.labels.service_name="shop-api"
  "PAYMENT_GATEWAY_KEY" OR "readyz"
  ```
- **Memory Saturation & Container Termination**:
  ```text
  resource.type="cloud_run_revision"
  resource.labels.service_name="shop-api"
  "Memory limit" OR "exit code 137"
  ```
- **Downstream Dependency Failures**:
  ```text
  resource.type="cloud_run_revision"
  resource.labels.service_name="shop-api"
  "inventory-svc" AND ("403" OR "Forbidden" OR "refused")
  ```

### Cloud Monitoring Metric Descriptors
- **Request Rate & 5xx Spikes**: `run.googleapis.com/request_count` grouped by `response_code_class`.
- **Latency Percentiles**: `run.googleapis.com/request_latencies` (aligner: `ALIGN_DELTA`, aggregator: `PERCENTILE_95`).
- **Container Memory Utilization**: `run.googleapis.com/container/memory/utilizations` (threshold: > 85%).
- **Instance Autoscaling**: `run.googleapis.com/container/instance_count`.

## 6. Known Failure Modes & Playbooks

### Failure Mode 1: Checkout Error Spike (Database Saturation)
- **Symptoms**: Surge in HTTP 500 errors on `/checkout` (~40% failures); availability drops below 99.9%.
- **Log Pattern**: `DatabaseConnectionError: Connection pool exhausted at shop-db:5432` or `TooManyConnectionsError`.
- **Diagnostic Steps**:
  1. Check Cloud SQL active connections: `cloudsql.googleapis.com/database/postgresql/num_backends`.
  2. Query Cloud Logging for stack traces originating in checkout handler.
  3. Verify connection pool size in `shop-api` environment variables (`DB_POOL_MAX`).
- **Remediation Playbook**:
  1. Increase PostgreSQL max connections or scale Cloud SQL instance tier.
  2. Verify PgBouncer connection pooler deployment.
  3. Reset fault injection if test incident: `bash demo/scripts/reset.sh`.

### Failure Mode 2: Extreme Latency Spike (> 500ms SLO Breach)
- **Symptoms**: p95/p99 latencies climb to 3–8 seconds; customers experience checkout timeouts; Cloud Run autoscaler maxes out instances.
- **Log Pattern**: High `httpRequest.latency` entries on `/catalog` and `/checkout`.
- **Diagnostic Steps**:
  1. Inspect Cloud Monitoring latency distributions for `shop-api` vs `inventory-svc`.
  2. Check thread pool / event loop blocking in FastAPI application.
- **Remediation Playbook**:
  1. Increase Cloud Run concurrency (`--concurrency 80`).
  2. Enable Cloud CDN or Redis read caching on catalog routes.
  3. Reset artificial latency fault: `bash demo/scripts/reset.sh`.

### Failure Mode 3: Bad Release (Missing Environment Configuration)
- **Symptoms**: Service returns HTTP 503 on `/readyz` and `/checkout` immediately following a deployment revision.
- **Log Pattern**: `Missing required environment variable: PAYMENT_GATEWAY_KEY` in application startup / readiness logs.
- **Audit Signal**: `CreateRevision` or `UpdateService` by developer or CI/CD service account shortly before incident.
- **Remediation Playbook**:
  1. Immediate Rollback:
     ```bash
     gcloud run services update-traffic shop-api --to-revisions PREVIOUS_REVISION=100
     ```
  2. Inject missing secret/env variable into Cloud Run revision configuration:
     ```bash
     gcloud run services update shop-api --set-env-vars PAYMENT_GATEWAY_KEY=live-key-xyz
     ```

### Failure Mode 4: Memory Leak Leading to OOM Restarts
- **Symptoms**: Memory utilization ramps linearly to 100%; instances crash with exit code 137; intermittent 503s during container replacement.
- **Log Pattern**: `Memory limit of 1024 MiB exceeded with 1032 MiB used. Consider increasing the memory limit.`
- **Diagnostic Steps**:
  1. Observe `run.googleapis.com/container/memory/utilizations` metric curve.
  2. Correlate instance restarts with memory saturation events.
- **Remediation Playbook**:
  1. Temporarily increase memory limit to stabilize production:
     ```bash
     gcloud run services update shop-api --memory 2Gi
     ```
  2. Review recent changes for unreleased byte buffers or global caches.
  3. Reset memory leak buffer: `bash demo/scripts/reset.sh`.

### Failure Mode 5: Downstream Inventory Authorization Failure (403 Forbidden)
- **Symptoms**: Catalog requests and inventory reservations fail with HTTP 502/500; `shop-api` logs report 403 Forbidden from `inventory-svc`.
- **Log Pattern**: `403 Forbidden: Cloud Run IAM invoker check failed` calling `inventory-svc`.
- **Audit Signal**: `SetIamPolicy` on `inventory-svc` modifying `roles/run.invoker`.
- **Remediation Playbook**:
  1. Restore IAM invoker permission on `inventory-svc`:
     ```bash
     gcloud run services add-iam-policy-binding inventory-svc \
       --member="serviceAccount:shop-api-sa@PROJECT_ID.iam.gserviceaccount.com" \
       --role="roles/run.invoker" \
       --region="us-central1"
     ```
  2. Reset fault state: `bash demo/scripts/reset.sh`.
