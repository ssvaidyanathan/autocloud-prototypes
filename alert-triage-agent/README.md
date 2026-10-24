# Alert Triage Agent (ATA)

Autonomous SRE diagnostic and incident triage system for Google Cloud Platform environments. ATA ingests Cloud Monitoring alerts, correlates related alerts into unified incident groups, orchestrates deterministic telemetry specialists across GCP services, synthesizes evidence-grounded root cause findings, and creates/updates ServiceNow incident records.

## Architecture: Thin Bridge & Agent-Owned Triage

The system is organized around a lean, stateless bridge and an autonomous agent core:

```
[Cloud Monitoring Alerts]
            │ Webhook (HTTP Basic Auth)
            ▼
+───────────────────────────+             +───────────────────────────+
│   Cloud Run: ata-bridge   │ ──────────> │     Cloud Tasks Queue     │
+─────────────┬─────────────+             │     ata-triage-queue      │
              ▲       │                   +─────────────┬─────────────+
              │       │ Instructions                    │
              │       │ (POST /instructions/ticket/*)   ▼
              │       │                   +───────────────────────────+
              │       +────────────────── │   Cloud Run: ata-agent    │ <───+
              │                           │   - Grouping & OKF Store  │     │
              │                           │   - Telemetry Specialists │     │
              │                           +─────────────┬─────────────+     │
              ▼                                         │                   │
+───────────────────────────+                           │ Timers            │ Wake
│   External ServiceNow     │                           ▼                   │
│   Table & Attachment API  │             +───────────────────────────+     │
+───────────────────────────+             │     Cloud Tasks Queue     │ ────+
                                          │     ata-timer-queue       │
                                          │     (delayed settle/cool) │
                                          +───────────────────────────+
```

1. **`ata-bridge`**: Lean ingestion and adapter service. Performs HTTP Basic Auth validation, validates alert scope, enqueues raw alerts directly to Cloud Tasks (HTTP 202 `accepted`), and exposes authenticated ServiceNow instruction endpoints (`POST /instructions/ticket/*`). Completely stateless with zero in-memory windowing or alert suppression.
2. **`ata-agent`**: Owns all incident domain logic. Correlates alerts, manages Open Knowledge Files (OKF) in GCS with `if_generation_match` atomic preconditions, schedules delayed timers on `ata-timer-queue`, evaluates stale-wake deadlines at `/timer/wake`, fans out read-only telemetry specialists, synthesizes evidence-based root cause reports, and calls Bridge Instruction API.
3. **`ata-tools`**: FastMCP read-only telemetry and topology service providing safe, structured inspection of GCP Cloud Run, Cloud SQL, Apigee X, and Cloud Logging.

---

## 5 Evaluation Scenarios (S1–S5)

The system is evaluated against 5 realistic failure scenarios:

| Scenario | Title | Target Infrastructure | Expected Incident Groups & Tickets | Root Cause Origin |
| :---: | :--- | :--- | :---: | :--- |
| **S1** | **Backend 5xx Cascade** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | `ata-inventory-api` |
| **S2** | **Backend Latency** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | `ata-inventory-api` (Upstream origin via DAG) |
| **S3** | **Concurrent Disjoint Incidents** | Apigee X & Cloud Run | 2 Groups, 2 Tickets | `ata-orders-v1` and `ata-inventory-api` (Disjoint groups) |
| **S4** | **DB Connection Exhaustion** | Cloud SQL (`ata-inventory-db`) | 1 Group, 1 Ticket | `ata-inventory-db` |
| **S5** | **Bad Deploy / Config Failure** | Cloud Run (`ata-inventory-api`) | 1 Group, 1 Ticket | Specific Cloud Run revision (e.g. `ata-inventory-api-00062-6pj`) |

---

## Key Design Guarantees

- **Agent-Owned Suppression & Grouping**: Grouping state lives exclusively in GCS OKFs (`gs://{incidents_bucket}/knowledge/incident-manager/incident-{group_id}.md`).
- **Distributed Concurrency**: All OKF updates use GCS generation preconditions (`if_generation_match`). Concurrent writes retry with exponential backoff and jitter, preventing lock clobbering.
- **Stale-Wake Pattern**: Delayed timers are scheduled on `ata-timer-queue`. When waking at `/timer/wake`, the agent compares current time against the OKF `settle_deadline`. Stale wakes (due to subsequent alert extension) are ignored without ticket creation.
- **Evidence-Bound Synthesis**: Root causes cite observed metrics, logs, and deployment revisions. Zero fabricated literals; zero "N+1" phrasing.
- **Security**: All inter-service calls use Google-signed OIDC ID tokens validated by `verify_agent_caller`. Webhook endpoints require HTTP Basic Auth verification. Service accounts operate under strict least-privilege boundaries.

---

## Testing & Verification

Run the full automated test suite:

```bash
# In virtual environment
/workspace/venv/bin/pytest
```

Run the authentic scenario replay harness:

```bash
/workspace/venv/bin/pytest tests/test_scenario_grouping_replay.py -v
```

---

## Documentation

- [DEMO.md](file:///workspace/alert-triage-agent/DEMO.md): Operator guide for executing and verifying failure scenarios S1 through S5.
- [DEPLOY.md](file:///workspace/alert-triage-agent/DEPLOY.md): Complete deployment procedures for Terraform, Cloud Run, Cloud Tasks, and Apigee.
- [AGENTS.md](file:///workspace/alert-triage-agent/AGENTS.md): Architecture reference, telemetry specialists, and synthesis pipeline.
- [CONFIGURATION.md](file:///workspace/alert-triage-agent/CONFIGURATION.md): Environment variables, secrets, and Cloud Tasks queue specs.
