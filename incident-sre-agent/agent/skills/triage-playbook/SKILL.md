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
