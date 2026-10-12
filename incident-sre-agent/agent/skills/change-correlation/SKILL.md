---
name: change-correlation
description: Methodologies for correlating deployments, IAM edits, and configuration updates with incident onset.
---

# Change Correlation Skill

## Overview
Over 75% of production outages are triggered by a recent deployment, configuration change, or IAM permission modification. This skill guides the systematic correlation of administrative changes with incident onset times.

## Temporal Correlation Window
- Define symptom onset timestamp $T_0$ from ticket `opened_at` or first metric alert.
- Search window: $T_0 - 60\text{ min} \le t \le T_0 + 15\text{ min}$.
- Changes occurring within 15 minutes prior to symptom onset are considered primary suspects.

## Change Categories to Inspect
1. **Application Deployments**:
   - New Cloud Run revisions or container image tag updates.
   - Cloud Build completions preceding the outage.
2. **Configuration & Environment**:
   - Updates to environment variable names, secrets, or memory/CPU allocations.
3. **IAM & Security Policies**:
   - Service account role bindings removed or altered.
4. **Infrastructure Edits**:
   - Network firewall changes, Cloud SQL tier or flag modifications.
