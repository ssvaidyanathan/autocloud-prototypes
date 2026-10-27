---
name: gke-troubleshooting
description: Troubleshooting Kubernetes Engine workloads, pod termination and rescheduling gaps, crash loops, OOM, probe failures, Autopilot provisioning, and ingress routing.
---

# GKE Troubleshooting Playbook

## Overview
This playbook guides SRE specialists in diagnosing incidents on Google Kubernetes Engine (GKE) standard and Autopilot clusters. It provides structured workflows for investigating pod termination gaps, automated scaling outages, crash loops, memory exhaustion, probe misconfigurations, and external ingress failures.

## Key Investigation Tools
When an incident is backed by GKE or involves Kubernetes workloads:
- **`describe_gke_clusters`** (`resources.py`): Discover GKE clusters across all regions (`locations/-`), Autopilot status, node counts, and cluster health.
- **`resolve_endpoint_to_gke`** (`resources.py`): Map external IP addresses or uptime check URLs to Compute Forwarding Rules, Target Pools / Backend Services, backing Kubernetes Service (`kubernetes.io/service-name`), and cluster name.
- **`query_gke_workload_status`** (`resources.py`): Inspect Deployments and StatefulSets (desired vs. ready replicas), Pod phases (`Pending`, `Running`, `CrashLoopBackOff`), container restart counts, last termination exit codes/reasons, and readiness/liveness/startup probe configurations.
- **`get_gke_workload_events`** (`logs.py`): Query structured Kubernetes lifecycle events (`logs/events`, Warning, `Killing`, `OOMKilling`, `FailedScheduling`, `Unhealthy`, `BackOff`) and K8s audit activity (`cloudaudit.googleapis.com/activity`).
- **`query_gke_metrics`** (`metrics.py`): Query Cloud Monitoring time series for container restart counts (`restart_count`), memory limit utilization (`memory_limit_utilization`), CPU usage (`cpu_core_usage`), and autoscaler metrics (`autoscaler_pending_pods`, `autoscaler_failures`).

---

## Diagnostic Workflows

### 1. Pod Termination and Rescheduling Gaps (Scale-to-Zero and Scaler-driven Outages)
- **Symptom**: An external endpoint or uptime check experiences intermittent or sudden 5xx / connection refused errors, often correlated with scaling operations.
- **Mechanism**:
  - Automated scalers, cron jobs, or operators execute scaling operations (e.g., `io.k8s.apps.v1.deployments.scale.patch` or rollout restart).
  - On single-replica workloads (`replicas: 1`), terminating the sole active pod before the replacement pod is ready leaves a zero-replica availability gap.
  - If readiness probes are missing or misconfigured, the container port may receive traffic before the application process has finished initializing.
- **Investigation Steps**:
  1. Call `query_gke_workload_status` for the target namespace to check `desired_replicas` vs `ready_replicas`.
  2. Call `get_gke_workload_events` over the incident window to check for pod `Killing` events, deletion timestamps, and subsequent `Scheduled` events.
  3. Call `query_audit_logs` or review `get_gke_workload_events` for `k8s.io` scale patches or administrative modifications.
  4. Compare the timestamp of pod termination against the uptime check failure onset.
- **Remediations**:
  - Implement `PodDisruptionBudget` (PDB) with `minAvailable: 1` to prevent eviction of the last replica.
  - Set deployment rolling update strategy to `maxUnavailable: 0` and `maxSurge: 1`.
  - Add startup and readiness probes to protect application warmup.

### 2. CrashLoopBackOff & Application Termination
- **Symptom**: Pods fail to remain in `Running` phase; restart counts steadily increase.
- **Investigation Steps**:
  1. Call `query_gke_workload_status` to inspect `container_statuses`:
     - Note the `restart_count`.
     - Inspect `last_state` for `exit_code` and `reason`.
  2. Call `query_gke_metrics(metric_type="restart_count")` to identify the restart rate and onset timestamp.
  3. Analyze exit codes:
     - **Exit Code 0**: Process exited cleanly; check if workload should be a Job instead of a Deployment.
     - **Exit Code 1**: Application exception / uncaught error; query container application logs via `query_logs`.
     - **Exit Code 137**: Process killed by SIGKILL (commonly Linux cgroup OOM killer or external kill).
     - **Exit Code 143**: Process killed by SIGTERM (graceful shutdown requested by Kubernetes).
  4. Call `query_logs` filtering for `resource.type = "k8s_container"` and target container stderr.

### 3. Out of Memory (OOMKilled)
- **Symptom**: Container restarts abruptly with exit code 137.
- **Investigation Steps**:
  1. Call `query_gke_workload_status`: check if `last_state.reason == "OOMKilled"` or `exit_code == 137`.
  2. Call `query_gke_metrics(metric_type="memory_limit_utilization")`: check if memory utilization reached 1.0 (100% of configured limit) prior to restart.
  3. Call `get_gke_workload_events`: search for events with reason `OOMKilling`.
- **Remediations**:
  - Increase container `resources.limits.memory`.
  - Profile application memory usage to resolve leaks or unbounded cache allocations.

### 4. Probe Failures (Readiness, Liveness, Startup)
- **Symptom**: Traffic is dropped or 502 Bad Gateway is returned while container is running; or container is repeatedly restarted after a fixed duration.
- **Investigation Steps**:
  1. Inspect probe configuration in `query_gke_workload_status` (`containers[*].readiness_probe`, `liveness_probe`, `startup_probe`).
  2. Check probe failure events via `get_gke_workload_events` searching for reason `Unhealthy`.
  3. Analyze probe types:
     - **Missing Startup Probe**: If an application takes 30+ seconds to initialize, liveness probes can kill the container prematurely during startup. Add `startupProbe` with adequate `failureThreshold`.
     - **TCP Socket vs HTTP Probe**: A TCP probe passes as soon as the port binds, potentially forwarding traffic before the application HTTP router or web server is ready. Use `httpGet` probes where available.
     - **Readiness Failure**: When readiness probe fails, the pod is removed from endpoints, causing upstream load balancers to return 502/503.

### 5. Failed Scheduling & Autopilot / Cluster Provisioning
- **Symptom**: Pods remain stuck in `Pending` phase with 0 ready replicas.
- **Investigation Steps**:
  1. Check pod status via `query_gke_workload_status`.
  2. Call `get_gke_workload_events` searching for reason `FailedScheduling`.
  3. Check cluster autoscaler telemetry via `query_gke_metrics(metric_type="autoscaler_pending_pods")` and `autoscaler_failures`.
  4. On GKE Autopilot clusters, verify if pod resource requests exceed compute class limits or if quota was exhausted.

### 6. Endpoint to Workload Ingress Resolution
- **Symptom**: An alert provides only an external IP address (e.g. `34.x.x.x:8501`) or Uptime Check URL without a clear service name.
- **Investigation Steps**:
  1. Call `resolve_endpoint_to_gke(endpoint=...)` passing the external IP or URL.
  2. The tool maps the IP through Compute Forwarding Rules and Target Pools / Backend Services to extract the `kubernetes.io/service-name` annotation and cluster name.
  3. Use the discovered namespace, service name, and cluster name to immediately target `query_gke_workload_status` and `get_gke_workload_events`.
