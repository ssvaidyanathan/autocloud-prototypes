# Key GCP Metric Descriptors

## Cloud Run
- **Request Count**: `run.googleapis.com/request_count`
  - Labels: `response_code_class` (e.g. "5xx", "4xx", "2xx"), `response_code`, `service_name`
- **Request Latencies**: `run.googleapis.com/request_latencies` (distribution)
- **Container Memory Utilization**: `run.googleapis.com/container/memory/utilizations`
- **Container CPU Utilization**: `run.googleapis.com/container/cpu/utilizations`
- **Container Instance Count**: `run.googleapis.com/container/instance_count`

## GKE (Kubernetes Engine)
- **Container Memory Used**: `kubernetes.io/container/memory/used_bytes`
- **Container Memory Limit**: `kubernetes.io/container/memory/limit_bytes`
- **Container CPU Usage**: `kubernetes.io/container/cpu/core_usage_time`
- **Container Restart Count**: `kubernetes.io/container/restart_count`

## Cloud SQL
- **CPU Utilization**: `cloudsql.googleapis.com/database/cpu/utilization`
- **Memory Utilization**: `cloudsql.googleapis.com/database/memory/utilization`
- **Disk Utilization**: `cloudsql.googleapis.com/database/disk/utilization`
- **Active Connections**: `cloudsql.googleapis.com/database/network/connections`
