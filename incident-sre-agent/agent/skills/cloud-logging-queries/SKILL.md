---
name: cloud-logging-queries
description: Logging filter cookbook, syntax patterns, and query construction for Cloud Logging.
---

# Cloud Logging Queries

## Overview
This skill guides the construction of precise, cost-effective Cloud Logging queries for SRE investigations.

## Best Practices
1. **Always Scope by Resource**:
   - Cloud Run: `resource.type = "cloud_run_revision" AND resource.labels.service_name = "<service>"`
   - GKE: `resource.type = "k8s_container" AND resource.labels.container_name = "<container>"`
   - Cloud SQL: `resource.type = "cloudsql_database" AND resource.labels.database_id = "<project>:<instance>"`
2. **Filter by Severity First**:
   - `severity >= ERROR` or `severity = ("ERROR" OR "CRITICAL" OR "ALERT" OR "EMERGENCY")`
3. **Narrow Time Bounds**:
   - Always bound the query with `timestamp >= "..." AND timestamp <= "..."`
4. **Target HTTP 5xx Statuses**:
   - `httpRequest.status >= 500`

## Common Query Patterns
- **Cloud Run Application Errors**:
  ```text
  resource.type = "cloud_run_revision"
  resource.labels.service_name = "shop-api"
  severity >= ERROR
  ```
- **Error Reporting Unhandled Exceptions**:
  ```text
  logName =~ "cloudaudit.googleapis.com%2Factivity" OR jsonPayload.stack_trace:*
  ```
