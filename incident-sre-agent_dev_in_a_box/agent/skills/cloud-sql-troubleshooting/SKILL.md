---
name: cloud-sql-troubleshooting
description: Procedures for debugging Cloud SQL connection exhaustion, disk saturation, CPU locks, and replication lag.
---

# Cloud SQL Troubleshooting Skill

## Overview
Diagnostic runbook for managed Cloud SQL instances (PostgreSQL, MySQL).

## Key Failure Scenarios
1. **Connection Pool Exhaustion**:
   - `max_connections` reached, new client requests receive "too many connections" or timeout.
   - Investigate application connection pooling settings (e.g. PgBouncer, HikariCP, SQLAlchemy pool size).
2. **Disk Full / Storage Auto-Resize Delay**:
   - Disk saturation halts write operations and triggers emergency read-only status.
   - Check `cloudsql.googleapis.com/database/disk/utilization`.
3. **CPU & Memory Saturation**:
   - Long-running unindexed queries holding exclusive locks or consuming full CPU resources.
   - Check `cloudsql.googleapis.com/database/cpu/utilization`.
4. **Replication Lag**:
   - Read replicas falling behind the primary instance, causing stale reads or read timeouts.
