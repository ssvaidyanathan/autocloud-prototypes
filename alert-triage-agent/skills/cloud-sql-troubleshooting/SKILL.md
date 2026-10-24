---
name: cloud-sql-troubleshooting
description: Diagnostic procedures for Cloud SQL PostgreSQL, connection pool exhaustion, row lock contention, slow query performance, and IAM database authentication.
---

# Cloud SQL Troubleshooting Skill

## Overview
This skill provides structured procedures for diagnosing database-tier incidents on Google Cloud SQL PostgreSQL instances (`ata-inventory-db`), focusing on connection saturation, lock contention, query latency regressions, and IAM database authentication.

---

## 1. Connection Pool Exhaustion & Backend Saturation

### Symptoms & Signals
- Upstream backend services (`ata-inventory-api`) log database connection errors:
  `remaining connection slots are reserved for non-replication superuser connections` or `OperationalError: connection to server failed: FATAL: too many connections`.
- Cloud Monitoring metric `cloudsql.googleapis.com/database/network/connections` or `database/postgresql/num_backends` approaches or hits `max_connections` limit (typically 100 on `db-f1-micro`).
- Cascade: Cloud Run returns 500/503; Apigee returns 502/504.

### Telemetry Queries & Verification
1. **Connection Metric**:
   Query `cloudsql.googleapis.com/database/network/connections` for `resource.labels.database_id = "cloud-assist-fde-2:ata-inventory-db"`.
2. **Database Log Inspection**:
   ```
   resource.type = "cloudsql_database"
   resource.labels.database_id = "cloud-assist-fde-2:ata-inventory-db"
   textPayload =~ "FATAL: too many connections"
   ```

---

## 2. Row Lock Contention & Transaction Blocks

### Symptoms & Signals
- API requests hanging until timeout (Apigee 504 Gateway Timeout or Cloud Run 504).
- Cloud Monitoring metric for transaction lock wait time or active query duration spiking.
- Queries blocked waiting on locks held by long-running or leaked uncommitted transactions (e.g. `SELECT ... FOR UPDATE` held open).

### Diagnostic Steps
1. Inspect Cloud SQL query logs for statements waiting on locks:
   ```
   resource.type = "cloudsql_database"
   textPayload =~ "process .* still waiting for .* on relation"
   ```
2. Identify blocking sessions holding locks without committing:
   Long-running transaction states (`idle in transaction`).

---

## 3. Query Insights & CPU Saturation

### Diagnostic Steps
1. **Query Insights Analysis**:
   Inspect normalized SQL statements contributing the highest load (measured in Average Active Sessions / AAS).
2. **Wait Event Breakdown**:
   Identify whether database time is spent in `CPU`, `Lock`, `IO`, or `ClientWrite`.
   - High `Lock`: Row lock contention or uncommitted transactions.
   - High `CPU`: Missing indexes leading to sequential table scans.
   - High `IO`: Memory pressure forcing buffer spills to disk.

---

## 4. IAM Database Authentication Regressions

### Symptoms & Signals
- Cloud Run startup or connection failure with `PAM authentication failed for user` or `password authentication failed for user "ata-inventory-sa"`.
- Cause: IAM database user missing `CLOUD_IAM_SERVICE_ACCOUNT` binding, instance flag `cloudsql.iam_authentication` toggled off, or Cloud SQL Client OAuth2 token expiration.
- Remediation: Confirm database user exists in Cloud SQL and verify IAM role `roles/cloudsql.client` is assigned to caller service account.
