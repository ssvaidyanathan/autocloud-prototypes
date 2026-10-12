---
name: cloud-run-troubleshooting
description: Generic diagnostic procedures for Cloud Run startup failures, OOM kills, configuration regressions, dependency authorization failures, cold starts, and concurrency limits.
---

# Cloud Run Troubleshooting Skill

## Overview
This skill provides service-agnostic diagnostic and remediation procedures for containerized workloads hosted on Google Cloud Run (fully managed).

## Key Inspection Checklist
1. **Revision State & Traffic Allocation**: Is the active revision serving 100% of traffic, or is traffic split across multiple revisions (`gcloud run services describe <service> --format="yaml(status.traffic)"`)?
2. **Container Startup & Readiness Failure**: Does the container fail readiness probes or crash on initialization before binding to `$PORT` within the configured timeout?
3. **Memory Limits & OOM Kills**: Did memory utilization reach the container limit, causing the kernel to send SIGKILL (exit code 137)?
4. **Concurrency & Thread Saturation**: Is `max_instance_request_concurrency` exhausted, causing requests to queue or timeout?
5. **Downstream Dependencies & IAM Invoker**: Are downstream service invocations failing with HTTP 403 Forbidden due to missing IAM `roles/run.invoker` or identity tokens?
6. **Cold Starts & Autoscaling Delays**: Are instance scaling delays causing latency spikes during traffic bursts?

---

## Diagnostic & Remediation Procedures

### 1. Memory Saturation & OOM Killer Terminations
- **Signals**:
  - Cloud Logging entries: `Memory limit of <LIMIT> exceeded`, `exit code 137`, `terminated by kernel OOM-killer`.
  - Cloud Monitoring metric: `run.googleapis.com/container/memory/utilizations` ramping linearly to 100%.
  - Intermittent HTTP 503 Service Unavailable errors as container instances crash and are replaced.
- **Diagnostic Steps**:
  1. Inspect container memory utilization curve in Cloud Monitoring to determine if consumption is gradual (leak) or instantaneous (workload spike).
  2. Correlate container restart count (`run.googleapis.com/container/instance_count` or revision status) with OOM events.
  3. Inspect application heap dumps and memory allocations for unreleased objects across request lifecycles.
- **Remediation Playbook**:
  1. Temporarily increase container memory limit to stabilize the service:
     ```bash
     gcloud run services update <service> --memory <LIMIT> --region <REGION>
     ```
  2. Inspect application code for unclosed streams, growing global buffers, or unbounded caches.
  3. Configure Cloud Monitoring alerting on `run.googleapis.com/container/memory/utilizations` exceeding 80% over a 5-minute window.
  4. Verify post-mitigation that memory utilization stabilizes below 60% and container restarts cease.

### 2. Configuration Regressions & Startup/Readiness Failures
- **Signals**:
  - Cloud Logging: `Missing required environment variable: <VAR>`, `Readiness probe failed`, `Container failed to start and listen on the port`.
  - HTTP 503 errors on health probes (e.g. `/readyz`) and user-facing endpoints immediately following a revision deployment.
  - Cloud Audit Logs: Recent `UpdateService` or `CreateRevision` API calls.
- **Diagnostic Steps**:
  1. Compare environment variables and container configuration between the failing revision and prior stable revision:
     ```bash
     gcloud run revisions describe <FAILING_REV> --region <REGION> --format="yaml(spec.containers[0].env)"
     gcloud run revisions describe <STABLE_REV> --region <REGION> --format="yaml(spec.containers[0].env)"
     ```
  2. Check Secret Manager references and IAM permissions of the service runtime service account.
  3. Verify probe endpoints directly or via staging test calls.
- **Remediation Playbook**:
  1. Immediate traffic rollback to the prior known-healthy revision:
     ```bash
     gcloud run services update-traffic <service> --to-revisions <STABLE_REV>=100 --region <REGION>
     ```
  2. Restore missing environment variables or Secret Manager bindings in the service configuration.
  3. Re-verify health and readiness probe endpoints return HTTP 200.
  4. Configure Cloud Monitoring alerting on HTTP 503 error rates and probe failures.

### 3. Downstream Dependency & IAM Authorization Failures
- **Signals**:
  - Cloud Logging: HTTP 403 Forbidden, `The request was not authenticated`, or `Cloud Run IAM invoker check failed` when calling downstream microservices.
  - HTTP 500 or 502 Bad Gateway returned to clients due to upstream dependency rejection.
  - Cloud Audit Logs: `SetIamPolicy` events modifying service accounts or IAM role bindings.
- **Diagnostic Steps**:
  1. Identify calling service account from Cloud Run revision specification:
     ```bash
     gcloud run services describe <service> --format="value(spec.template.spec.serviceAccountName)"
     ```
  2. Check IAM policy on the downstream target service:
     ```bash
     gcloud run services get-iam-policy <downstream-service> --region <REGION>
     ```
  3. Confirm caller is generating OpenID Connect (OIDC) ID tokens targeted at the downstream service's audience URL.
- **Remediation Playbook**:
  1. Grant `roles/run.invoker` to the caller's service account on the downstream service:
     ```bash
     gcloud run services add-iam-policy-binding <downstream-service> \
       --member="serviceAccount:<CALLER_SA>@<PROJECT_ID>.iam.gserviceaccount.com" \
       --role="roles/run.invoker" \
       --region <REGION>
     ```
  2. Verify network egress and Cloud Run ingress bindings (e.g. `--ingress=internal` vs `--ingress=all`).
  3. Configure Cloud Monitoring alerting on downstream dependency HTTP 403 / 5xx error rates.
  4. Ensure graceful degradation and fallback handling in application code when downstream services are unavailable.

### 4. Latency Spikes, Saturation & Concurrency Bottlenecks
- **Signals**:
  - Elevated p95/p99 request latencies (`run.googleapis.com/request_latencies`).
  - Client request timeouts or gateway timeouts (HTTP 504).
  - High container instance count scaling towards maximum limits.
- **Diagnostic Steps**:
  1. Profile application request pipelines and asynchronous event loops for synchronous blocking calls or CPU saturation.
  2. Inspect synchronous locks, thread contention, downstream processing delays, or external API latency.
  3. Check Cloud Run concurrency configuration (`containerConcurrency`):
     ```bash
     gcloud run services describe <service> --format="value(spec.template.spec.containerConcurrency)"
     ```
- **Remediation Playbook**:
  1. Execute request pipeline profiling and eliminate synchronous blocking locks or downstream delays.
  2. Tune container concurrency and worker thread configurations: adjust worker thread pools or adjust concurrency settings to match workload I/O vs CPU profile.
  3. Configure Cloud Monitoring latency alerting (evaluating service p95 SLO thresholds on `run.googleapis.com/request_latencies`).
  4. Enable caching layers (e.g. Redis, Cloud CDN) or asynchronous queueing for long-running downstream operations.

### 5. Revision Rollback & Traffic Management
- **Immediate 100% Rollback**:
  ```bash
  gcloud run services update-traffic <service> --to-revisions <STABLE_REV>=100 --region <REGION>
  ```
- **Canary / Gradual Traffic Shift**:
  ```bash
  gcloud run services update-traffic <service> --to-revisions <NEW_REV>=10,<STABLE_REV>=90 --region <REGION>
  ```
- **Verify Active Traffic Distribution**:
  ```bash
  gcloud run services describe <service> --region <REGION> --format="table(status.traffic.revisionName,status.traffic.percent)"
  ```
