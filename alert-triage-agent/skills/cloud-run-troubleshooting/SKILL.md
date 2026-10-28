---
name: cloud-run-troubleshooting
description: Diagnostic procedures for Cloud Run HTTP 5xx errors, container instance metrics, cold starts, memory/CPU saturation, and concurrency bottlenecks.
---

# Cloud Run Troubleshooting Skill

## Overview
This skill provides structured diagnostic procedures for investigating server errors, performance degradation, and resource saturation on Google Cloud Run services (`ata-orders-api`, `ata-inventory-api`).

---

## 1. Cloud Run HTTP 5xx Classification & Root Causes

| Status Code | Primary Mechanism | Characteristic Signals & Logs |
| :--- | :--- | :--- |
| **HTTP 500** | Internal Application Exception | Unhandled Python exceptions, database driver connection crashes, traceback in `stdout`/`stderr`. |
| **HTTP 502** | Bad Gateway / Protocol Fault | Container terminated abruptly while handling request, proxy protocol mismatch, socket closed prematurely. |
| **HTTP 503** | Service Unavailable | Concurrency limit reached with zero spare capacity, container failed readiness probe or crashed on startup. |
| **HTTP 504** | Gateway Timeout | Request duration exceeded configured Cloud Run request timeout (`timeoutSeconds`), slow downstream RPC. |

---

## 2. Resource Utilization & Saturation Diagnostics

### Memory Saturation & OOM Terminations
- **Signals**:
  - Container instance restart or sudden termination with **exit code 137** (SIGKILL by kernel OOM killer).
  - Metric `run.googleapis.com/container/memory/utilizations` ramping to 1.0 (100%).
  - Cloud Logging: `Memory limit of 512MiB exceeded with 513MiB used. Consider increasing the memory limit.`
- **Telemetry Query**:
  ```
  resource.type = "cloud_run_revision"
  resource.labels.service_name = "<service_name>"
  (textPayload =~ "exit code 137" OR textPayload =~ "Memory limit")
  ```

### CPU Throttling
- **Signals**:
  - Latency spikes on CPU-bound operations (serialization, JSON parsing, crypto).
  - Metric `run.googleapis.com/container/cpu/utilizations` exceeding 0.90 continuously.
  - Request processing queued behind thread event loop.

### Concurrency Bottlenecks & Container Scaling
- **Signals**:
  - Metric `run.googleapis.com/container/instance_count` hitting `max_instances`.
  - Cold starts: High latency on initial requests to newly scheduled container instances (`container/startup_latencies`).
  - Active request count per instance exceeding `container_concurrency`.

---

## 3. Investigation Playbook

1. **Check Active Revision Health**:
   Verify whether traffic is routed to a newly deployed revision or an existing revision:
   ```bash
   gcloud run services describe <service> --region=us-west1 --format="yaml(status.traffic)"
   ```
2. **Inspect Error Rate Metric**:
   Query Cloud Monitoring `run.googleapis.com/request_count` grouped by `response_code_class` (5xx).
3. **Inspect Application Error Logs**:
   Retrieve stack traces and error messages in Cloud Logging:
   ```
   resource.type = "cloud_run_revision"
   resource.labels.service_name = "<service_name>"
   severity >= ERROR
   ```
4. **Inspect Dependency Connectivity**:
   Confirm whether backend exceptions correlate with downstream service failures (e.g., inventory API returning 500 when Cloud SQL is saturated).

---

## 4. Remediation Guidelines
- **Application Crash**: Roll back traffic to the previous healthy revision using `gcloud run services update-traffic`.
- **Memory Saturation**: Temporarily bump container memory allocation or inspect memory leak in application heap.
- **Concurrency Saturation**: Increase `max-instances` or adjust `--concurrency` settings.
