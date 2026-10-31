---
name: cloud-monitoring-metrics
description: Metric descriptors, baseline comparison rules, and anomaly thresholds for Cloud Monitoring.
---

# Cloud Monitoring Metrics Skill

## Overview
This skill provides guidance on selecting and evaluating key Google Cloud metrics to detect outages, service degradation, and anomalies.

## Diagnostic Workflow
1. **Identify the Primary Resource**: Determine whether the target is Cloud Run, GKE, Cloud SQL, or Compute Engine.
2. **Examine Golden Signals**:
   - **Traffic**: Request counts, queries per second.
   - **Errors**: 5xx HTTP response counts, database error rates.
   - **Latency**: Round-trip response latency percentiles (p50, p95, p99).
   - **Saturation**: CPU utilization, memory utilization, connection pool utilization.
3. **Compare Against Baseline**:
   - Compare current 1-hour window against the preceding 1-hour window or 24-hour historical baseline.
   - Any metric shift exceeding +/-20% requires analysis and inclusion in findings.
