---
name: triage-playbook
description: Playbook for parsing incident tickets, identifying affected services, setting time windows, and selecting specialists.
---

# Triage & Investigation Playbook

## Overview
This playbook guides the Dispatcher agent through parsing incoming incident tickets, formulating an `InvestigationPlan`, bounding telemetry time windows, and dispatching specialists.

## Ticket Parsing Rules
1. **Service Extraction**:
   - Inspect `service_hint`, ticket title, and description for service identifiers (e.g., `shop-api`, `checkout`, `inventory-svc`).
   - Match against known services in service catalog runbooks.
2. **Symptom Classification**:
   - Extract primary symptoms: HTTP 5xx errors, latency breach, crash loop, connection timeout, deployment regression.
3. **Time Window Determination**:
   - Baseline window: `start_time = opened_at - 60 minutes` and `end_time = now` (or incident opened_at + 15 minutes).
   - If ticket specifies exact incident start or change time, center window around that timestamp.

## Specialist Dispatch Strategy
- **LogsSpecialist**: Dispatch whenever errors, crashes, 5xx responses, or unhandled exceptions are mentioned.
- **MetricsSpecialist**: Dispatch whenever latency degradation, throughput drop, CPU/memory saturation, or alert policy violations are indicated.
- **ChangesSpecialist**: Always dispatch for P1/P2 incidents or when symptoms began suddenly, to correlate with recent deploys, builds, or IAM changes.
- **ResourcesSpecialist**: Dispatch when service configuration, memory limits, database states, or cluster node capacities may be involved.

## Platform Classification & Routing
1. **Evidence-Driven Platform Identification**:
   - Do NOT default to assuming Cloud Run when an alert or incident mentions an external IP, Uptime Check host, or Kubernetes workload.
   - If ticket references an external IP, port (e.g. 8501, 8080), or Uptime Check URL:
     - Direct `ResourcesSpecialist` to use `resolve_endpoint_to_gke` to discover the backing Compute Forwarding Rule, Target Pool / Backend Service, and Kubernetes Service.
   - If the service is not found in Cloud Run (`describe_cloud_run_service` returns `NOT_FOUND` or Cloud Run metrics return 0 requests):
     - Check GKE clusters via `describe_gke_clusters` and inspect Kubernetes workloads via `query_gke_workload_status`.
2. **GKE Incident Routing**:
   - When evidence points to GKE (clusters, pods, deployments, namespaces, or resolved GKE endpoints):
     - Apply the `gke-troubleshooting` and `k8s-change-correlation` playbooks.
     - Direct `ResourcesSpecialist` to use `query_gke_workload_status` and `describe_gke_clusters`.
     - Direct `LogsSpecialist` to use `get_gke_workload_events` (`logs/events` and `cloudaudit.googleapis.com/activity`).
     - Direct `MetricsSpecialist` to use `query_gke_metrics` (`restart_count`, `memory_limit_utilization`, `cpu_core_usage`, `autoscaler_*`).
     - Direct `ChangesSpecialist` to correlate `k8s.io` audit operations (scale patches, rollout updates, ConfigMap edits) via `k8s-change-correlation`.

