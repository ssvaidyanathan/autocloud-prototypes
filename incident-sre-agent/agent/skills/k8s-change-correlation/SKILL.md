---
name: k8s-change-correlation
description: Correlating Kubernetes Engine incidents with k8s.io audit log changes, workload rollouts, scale patches, ConfigMap/Secret edits, and Autopilot events.
---

# Kubernetes Change Correlation Playbook

## Overview
Outages on Google Kubernetes Engine (GKE) standard and Autopilot clusters are predominantly triggered by recent configuration changes, deployment rollouts, automated or manual scaling patches, or cluster infrastructure adjustments. This playbook guides specialists in querying and correlating Kubernetes administrative operations with incident onset.

## Primary Telemetry Sources & Tools
- **`query_audit_logs`** (`changes.py`): Search Cloud Audit Logs for administrative mutations across GCP and Kubernetes resources (`protoPayload.serviceName = "k8s.io"`). Primary audit tool for `ChangesSpecialist`.
- **`get_gke_workload_events`** (`logs.py`): Query structured Kubernetes lifecycle events and `k8s.io` Cloud Audit Logs (used by `LogsSpecialist` in cross-specialist investigations).
- **`query_gke_workload_status`** (`resources.py`): Inspect current desired vs. ready replicas, active rollout state, and probe definitions (used by `ResourcesSpecialist`).
- **`query_gke_metrics`** (`metrics.py`): Correlate change timestamps with container restart counts, memory utilization spikes, and autoscaler actions (used by `MetricsSpecialist`).

---

## Core Change Categories to Inspect

### 1. Workload Scaling Operations (Manual & Automated)
- **Method Names**: `io.k8s.apps.v1.deployments.scale.patch`, `io.k8s.apps.v1.deployments.scale.update`, `io.k8s.apps.v1.statefulsets.scale.patch`.
- **Actors**:
  - Custom scaler service accounts (e.g. `*-scaler@...iam.gserviceaccount.com`).
  - Kubernetes HorizontalPodAutoscaler (HPA) controller.
  - Scheduled maintenance or cron scaling scripts.
  - Manual engineer `kubectl scale` commands.
- **Diagnostic Pattern**:
  - Identify the exact scale patch timestamp $T_{\text{scale}}$.
  - Check whether the patch reduced replica count (e.g. `replicas: 1 -> 0`) or triggered a pod termination without surge capacity.
  - Correlate with pod `Killing` events and subsequent `Scheduled` / `Pulled` / `Started` events.
  - If the uptime check or service alert fires between $T_{\text{kill}}$ and $T_{\text{ready}}$, the outage is directly attributable to the scaling transition gap.

### 2. Workload Rollouts & Container Image Updates
- **Method Names**: `io.k8s.apps.v1.deployments.patch`, `io.k8s.apps.v1.deployments.update`, `io.k8s.apps.v1.daemonsets.patch`.
- **Actors**: CI/CD pipelines (Cloud Build, GitHub Actions), Helm, ArgoCD, or SRE deployments.
- **Diagnostic Pattern**:
  - Check if a new ReplicaSet was generated or a new container image tag was deployed.
  - Verify whether new pods entered `CrashLoopBackOff`, `ImagePullBackOff`, or failed readiness probes.
  - Correlate the rollout completion time with the onset of elevated 5xx error rates or increased p95 latency.

### 3. ConfigMap & Secret Modifications
- **Method Names**: `io.k8s.core.v1.configmaps.patch`, `io.k8s.core.v1.secrets.patch`.
- **Diagnostic Pattern**:
  - Application pods that read environment variables from ConfigMaps/Secrets on startup will only reflect changes when restarted.
  - Pods mounting ConfigMaps as volumes may experience hot-reload exceptions or syntax errors when new config is applied.
  - Check whether application crash loops started immediately after a ConfigMap/Secret update.

### 4. Node Pool & GKE Autopilot Events
- **Operations**: Node pool autoscaling, GKE version auto-upgrades, node drain/eviction.
- **Diagnostic Pattern**:
  - Search for `FailedScheduling` events or `node_provisioning_failed` in cluster autoscaler metrics.
  - Correlate node upgrades with mass pod evictions or transient capacity deficits in specific zones.

---

## Systematic Correlation Workflow
1. **Define Incident Onset Window**:
   - Establish symptom onset $T_0$ from the alert open timestamp or initial latency/error spike.
   - Set search window: $[T_0 - 60\text{ min}, T_0 + 15\text{ min}]$.
2. **Execute Audit Query**:
   - For `ChangesSpecialist`, invoke `query_audit_logs(service_name=..., start_time=..., end_time=...)` to identify administrative mutations matching `protoPayload.serviceName = "k8s.io"`.
   - In cross-specialist investigations, examine `get_gke_workload_events` results retrieved by `LogsSpecialist` for correlated Kubernetes lifecycle events.
   - Identify operations with `protoPayload.serviceName = "k8s.io"`.
3. **Identify Principal & Rationale**:
   - Note `principal_email` to distinguish human operators from automated scaling service accounts.
4. **Synthesize Findings**:
   - Quantify the exact duration between the administrative mutation and recovery.
   - Formulate concrete operational recommendations (e.g., PodDisruptionBudgets, rolling update tuning, probe refinement).
