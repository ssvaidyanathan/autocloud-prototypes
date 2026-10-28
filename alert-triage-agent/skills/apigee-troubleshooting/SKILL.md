---
name: apigee-troubleshooting
description: Diagnostic procedures for Apigee X API proxies, distinguishing proxy vs target error codes, diagnosing SpikeArrest policy faults, and inspecting MessageLogging access logs.
---

# Apigee Troubleshooting Skill

## Overview
This skill provides structured procedures for diagnosing API proxy errors and latency regressions on Apigee X (`ata-orders-v1`, `ata-inventory-v1`), distinguishing edge proxy faults from backend target service errors.

---

## 1. Proxy vs. Target Error Disambiguation

| Symptom | Fault Source | Mechanism | Diagnostic Evidence |
| :--- | :--- | :--- | :--- |
| **HTTP 429 Too Many Requests** | `Apigee Proxy` | SpikeArrest policy rate exceeded (e.g. rate limit configured too strict). | Metric `fault_code = "SpikeArrestViolation"`, target latency = 0, zero backend requests in Cloud Run. |
| **HTTP 502 Bad Gateway** | `Apigee Target Connection` | Target endpoint unreachable, invalid backend host, SSL handshake failure. | `fault_code = "TARGET_CONNECT_TIMEOUT"` or `DNSResolutionError`, bundle target URL points to invalid backend. |
| **HTTP 504 Gateway Timeout** | `Apigee Target / Backend` | Target response time exceeded proxy connection timeout (default 55s or custom). | Target latency metric elevated, Cloud Run backend latency exceeds timeout window. |
| **HTTP 500 Internal Error** | `Backend Service` | Target Cloud Run service responded with 500; Apigee relays response to client. | Target response code = 500, Cloud Run logs show application exception. |

---

## 2. SpikeArrest Fault Diagnostics

- **Signals**:
  - Burst of HTTP 429 status codes returned to clients.
  - Access log `ata-apigee-access` shows `fault_source = "apigee"` and `error_code = "policies.ratelimit.SpikeArrestViolation"`.
  - Backend Cloud Run metrics report zero received requests during the spike.
- **Verification via `get_proxy_bundle`**:
  Fetch and parse the active proxy revision bundle in-memory:
  - Inspect `spike_arrest_policies` in parsed output.
  - Check `<Rate>`: If configured to an overly strict value (such as `1pm` or `1ps` instead of `100ps` or `500pm`), requests are aggressively throttled.
- **Remediation**:
  Deploy updated revision with standard production SpikeArrest rate (e.g., `100ps` or `1000pm`).

---

## 3. MessageLogging Access Log Inspection

Apigee proxies write structured JSON access logs to Cloud Logging at `projects/<apigee-org>/logs/<prefix>-apigee-access` (the Apigee organization's project; `<prefix>` is the stack's `NAME_PREFIX`, default `ata`).

### Key Log Fields
- `proxy_name`: API proxy name (`<prefix>-orders-v1`, `<prefix>-inventory-v1`).
- `response_status_code`: Status code returned to the external client.
- `target_status_code`: Status code returned by the downstream backend service (empty if fault occurred in proxy flow).
- `client_received_start_timestamp`: Request arrival timestamp.
- `target_response_time_ms`: Backend target execution latency in milliseconds.
- `fault_name`: Name of the policy that triggered an error flow (e.g., `SpikeArrest-1`).

### Query Pattern
```
logName = "projects/<apigee-org>/logs/<prefix>-apigee-access"
jsonPayload.proxy_name = "<proxy_name>"
jsonPayload.response_status_code >= 400
```

---

## 4. Telemetry Metrics Correlation
1. **Proxy Response Counts**: Query `apigee.googleapis.com/proxy/response_count` filtered by `response_code` and `fault_code`.
2. **Target Latencies**: Query `apigee.googleapis.com/target/latencies` to verify if backend service delay caused timeouts.
