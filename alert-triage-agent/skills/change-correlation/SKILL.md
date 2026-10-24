---
name: change-correlation
description: Methodologies for correlating Cloud Run revisions and Apigee deployments with alert onset curves and distinguishing change-induced regressions from organic incidents.
---

# Change Correlation Skill

## Overview
This skill provides analytical methods for correlating infrastructure deployments, proxy revisions, and configuration updates with the temporal onset of Cloud Monitoring alerts.

---

## 1. Temporal Alignment & Windowing Rules

When evaluating an alert $A$ against recent system changes:
- **Correlation Window**: Inspect all deployments and configuration changes occurring within $[-15\text{ minutes}, +5\text{ minutes}]$ of the initial alert onset timestamp $t_{\text{alert\_onset}}$.
- **Score Calculation**:
  - **Identical Deployment Event ($S_{\text{change}} = 1.0$)**: The alerting service or proxy had a revision deployed within the correlation window.
  - **Related Upstream/Downstream Deploy ($S_{\text{change}} = 0.5$)**: A directly connected neighbor service in the topology DAG was deployed within the correlation window.
  - **No Shared Change ($S_{\text{change}} = 0.0$)**: No deployments detected within the correlation window or changes occurred well outside the window.

---

## 2. Alert Onset Curve Classification

| Onset Curve Shape | Telemetry Signature | Typical Underlying Failure Mechanism |
| :--- | :--- | :--- |
| **Step Function (Immediate)** | Error rate jumps from 0% to >50% within 1–2 minutes of deployment. | • Code regression in newly deployed revision.<br/>• Invalid target backend configuration in Apigee proxy revision.<br/>• Missing environment variable or invalid IAM database user. |
| **Linear / Ramp (Gradual)** | Metric increases monotonically over 5–20 minutes. | • Memory leak leading eventually to OOM-killer (exit code 137).<br/>• Database connection pool leak saturating `max_connections`.<br/>• Slow cache degradation. |
| **Exponential / Spike** | Sharp transient peak correlating with traffic burst. | • SpikeArrest rate limit too strict during traffic surge.<br/>• Concurrency bottleneck on container instances. |

---

## 3. Disambiguating Concurrent Independent Changes

When multiple changes occur in close proximity (e.g., simultaneous deployment on Cloud Run and a new Apigee proxy revision):
1. **Trace Causal Chains**: Verify if the failure type matches the specific change:
   - Apigee 429 SpikeArrest corresponds to proxy revision changes, not database configuration.
   - Cloud Run 500 database auth error corresponds to backend revision changes, not Apigee policies.
2. **Topology Verification**: Disjoint branches in the DAG with different change histories indicate independent root causes and must be grouped into separate incident tickets.

---

## 4. Querying Change History via `get_change_timeline`

Use the `get_change_timeline(window_hours=2)` tool to retrieve:
- Cloud Run revision creation timestamps, container image tags, and author identities.
- Apigee proxy deployment start times and revision identifiers in environment `prod`.
- Compare revision timestamps against the first alert onset timestamp in the incident group.
