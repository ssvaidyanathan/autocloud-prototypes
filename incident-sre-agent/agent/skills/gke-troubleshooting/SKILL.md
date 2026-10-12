---
name: gke-troubleshooting
description: Troubleshooting Kubernetes Engine workloads, CrashLoopBackOff, OOMKilled, Evicted pods, and ingress failures.
---

# GKE Troubleshooting Skill

## Overview
This skill provides structured diagnostic steps for Kubernetes workloads deployed on Google Kubernetes Engine (GKE).

## Common Pod Failure Modes
1. **CrashLoopBackOff**:
   - The application process terminates immediately upon launch.
   - Root causes: Misconfigured configmaps/secrets, database connection refused, runtime exceptions.
2. **OOMKilled (Exit Code 137)**:
   - Linux cgroup killer terminated the container because memory usage exceeded `resources.limits.memory`.
   - Distinguish from node-level pressure (Evicted status).
3. **ImagePullBackOff / ErrImagePull**:
   - Image tag does not exist in Artifact Registry, or workload identity lacks `roles/artifactregistry.reader`.
4. **Ingress & Service Routing Failures**:
   - Readiness probes failing causing endpoints controller to remove pod from target proxy pool.
   - 502 Bad Gateway emitted by Cloud Load Balancing ingress.

## Recommended Investigation Queries
- Check container restart counts and termination messages.
- Filter logs by: `resource.type = "k8s_container" AND jsonPayload.reason = "Evicted" OR "OOMKilled"`.
