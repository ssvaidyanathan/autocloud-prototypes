# Configuration Guide

This document provides a comprehensive reference for all configuration options across the **Incident SRE Agent** system (`agent`), **Incident Bridge** (`bridge`), and **Demo Environment** (`demo`).

---

## 1. Architecture & Flow

Configuration flows strictly through documented configuration mechanisms with **zero hardcoding** in application code:

- **Local Development**: `.env` file or process environment variables $\rightarrow$ `config.py` (`pydantic-settings`).
- **Production (Cloud Run & GCP)**: `terraform.tfvars` $\rightarrow$ Terraform variables $\rightarrow$ Cloud Run container environment variables & Secret Manager bindings $\rightarrow$ `config.py` (`pydantic-settings`).

```
[terraform.tfvars]
       │
       ▼
[Terraform variables.tf]
       │
       ├─► Cloud Run env vars ──────────────┐
       │                                     ▼
       └─► Secret Manager bindings ──► [config.py (pydantic-settings)]
                                             ▲
[Local: .env / process env] ─────────────────┘
```

### Production Fail-Fast Rule
Outside of local testing (`APP_ENV=local` or `ENVIRONMENT=local`), `GCP_PROJECT` is strictly required. If `GCP_PROJECT` cannot be resolved from the environment, Application Default Credentials (ADC), or the GCP compute metadata server, service startup fails immediately with a descriptive `ValueError`.

### Resource Name Prefix & Common Labels
Every GCP resource name is prefixed by a configurable prefix (`var.name_prefix`, defaulting to `"isa"`, e.g. `"acme"`).
- Composed as `"${var.name_prefix}-${base_name}"` for Cloud Run services, Cloud Tasks queues, Artifact Registry repositories, Secret Manager secrets, and Service Accounts.
- All labelable GCP resources (Cloud Run services, Artifact Registry repos, Secret Manager secrets) automatically receive standard `common_labels`:
```hcl
locals {
  common_labels = {
    app       = "incident-sre-agent"
    component = "agent" # or "bridge" or "demo"
    env       = var.environment
    prefix    = var.name_prefix
  }
}
```

### Vertex AI Location vs GCP Region
- `GCP_REGION`: Defaults to `us-central1` for hosting Cloud Run, Cloud Tasks, and Artifact Registry.
- `VERTEX_LOCATION`: Defaults to `global`. Google Gemini 3.x models (`gemini-3.8-flash`) are served **only** through the global Vertex AI endpoint. Calling `us-central1` returns HTTP 404 for Gemini 3.x models.

### Terraform Remote State & GCS Backend
All 3 Terraform stacks define a partial GCS backend:
```hcl
terraform {
  backend "gcs" {}
}
```
- **State Bucket**: Bootstrapped via `scripts/bootstrap-tfstate.sh` with uniform bucket-level access, public access prevention, object versioning, and common labels (`app=incident-sre-agent`, `component=tfstate`, `env=${ENVIRONMENT}`, `prefix=${NAME_PREFIX}`).
- **Backend Configuration**: Provided via `backend.hcl` (`bucket = "..."`, `prefix = "isa/agent"`) or CLI flag `-backend-config`.
- **CI & Local Validation**: Pre-deployment quality gates and CI use `terraform init -backend=false` to validate syntax and schemas without cloud dependencies.
- **Disaster Recovery**: State recovery is enabled by GCS object versioning.

### Immutable Container Image Tagging
All build scripts (`agent/scripts/build-push.sh`, `bridge/scripts/build-push.sh`), deployment scripts (`agent/scripts/deploy.sh`, `bridge/scripts/deploy.sh`, `scripts/deploy-all.sh`), and Terraform stacks standardize image tagging to default to the git short SHA of the deployment commit (`$(git rev-parse --short HEAD)`), falling back to `latest` only when git metadata is unavailable:
- **Traceability**: Deploys immutable image tags tied directly to source repository commits, deprecating mutable `:latest` deployments so each Cloud Run revision is traceable.
- **Manual Overrides**: Explicit `IMAGE_TAG` or `TAG_NAME` environment variables and `-var="image_tag=..."` Terraform arguments override the default.

### Health Check Endpoints & Edge Routing
Both `incident-bridge` and `sre-agent` expose:
- `GET /health`: Returns `{"status": "healthy", "service": "<service_name>"}`.

> [!IMPORTANT]
> **Google Frontend (GFE) Edge Interception of `/healthz`**:
> When deployed to Google Cloud Run, Google Frontend (GFE) edge proxies reserve and intercept requests matching `/healthz`, returning a Google 404 HTML robot error page before the request ever reaches the container instance. In contrast, `GET /health` routes cleanly through Cloud Run and GFE directly to the application container. Therefore, `/health` is the single designated health check endpoint across smoke tests, load balancers, and external monitoring probes.

---

## 2. SRE Agent Configuration (`agent`)

All agent settings are defined in `agent/app/config.py` via `AgentConfig`. Every field can be supplied via environment variable (case-insensitive) or `.env` file.

| Setting Name | Environment Variable | Purpose | Default Value | Where Set | Required |
|---|---|---|---|---|---|
| `port` | `PORT` | HTTP port for Cloud Run / Uvicorn server | `8080` | Both (`.env`, `tfvars`) | No |
| `app_env` | `APP_ENV` | Application environment (`production`, `staging`, `local`) | `production` | Both (`.env`, `tfvars`) | No |
| `name_prefix` | `NAME_PREFIX` | Prefix applied to all created resource names and labels | `isa` | Both (`.env`, `tfvars`) | No |
| `service_name` | `SERVICE_NAME` | Service identifier reported in health checks and telemetry | `sre-agent` | Both (`.env`, `tfvars`) | No |
| `gcp_project` | `GCP_PROJECT` or `PROJECT_ID` | GCP Project ID for telemetry, Cloud Run, Vertex AI | Dynamic (env / ADC / metadata) | Both (`.env`, `tfvars`) | **Yes** (in prod) |
| `gcp_region` | `GCP_REGION` | Regional location for GCP resources | `us-central1` | Both (`.env`, `tfvars`) | No |
| `vertex_location` | `VERTEX_LOCATION` | Vertex AI regional endpoint for GenAI SDK (`global` for Gemini 3.x) | `global` | Both (`.env`, `tfvars`) | No |
| `model_dispatcher` | `MODEL_DISPATCHER` | Gemini model for top-level investigation coordination | `gemini-3.8-flash` | Both (`.env`, `tfvars`) | No |
| `model_specialist` | `MODEL_SPECIALIST` | Gemini model for domain specialist subagents | `gemini-3.8-flash` | Both (`.env`, `tfvars`) | No |
| `model_synthesizer` | `MODEL_SYNTHESIZER` | Gemini model for root cause synthesis | `gemini-3.8-flash` | Both (`.env`, `tfvars`) | No |
| `gemini_pro_model` | `GEMINI_PRO_MODEL` | Alias override for dispatcher & synthesizer models | `None` | `.env` | No |
| `gemini_flash_model` | `GEMINI_FLASH_MODEL` | Alias override for specialist models | `None` | `.env` | No |
| `allowed_callback_hosts` | `ALLOWED_CALLBACK_HOSTS` | Comma-delimited wildcard hosts allowed for HTTP callbacks | `["*.run.app"]` | Both (`.env`, `tfvars`) | No |
| `timeout_seconds` | `TIMEOUT_SECONDS` | General investigation timeout in seconds | `600` | Both (`.env`, `tfvars`) | No |
| `investigation_timeout_seconds` | `INVESTIGATION_TIMEOUT_SECONDS` | Specific investigation timeout override (if set) | `None` | Both (`.env`, `tfvars`) | No |
| `investigation_budget_seconds` | `INVESTIGATION_BUDGET_SECONDS` | Maximum cumulative time allocated for specialist investigations | `300.0` | `.env` | No |
| `max_review_rounds` | `MAX_REVIEW_ROUNDS` | Maximum review loops in investigation pipeline | `1` | `.env` | No |
| `specialist_query_limit` | `SPECIALIST_QUERY_LIMIT` | Maximum queries permitted per specialist | `5` | `.env` | No |
| `specialist_max_entries` | `SPECIALIST_MAX_ENTRIES` | Maximum telemetry entries fetched per tool query | `50` | `.env` | No |
| `max_tool_calls_per_specialist`| `MAX_TOOL_CALLS_PER_SPECIALIST` | Tool invocation budget per specialist | `25` | `.env` | No |
| `default_lookback_minutes` | `DEFAULT_LOOKBACK_MINUTES` | Default telemetry time window lookback before incident | `60` | `.env` | No |
| `max_query_window_hours` | `MAX_QUERY_WINDOW_HOURS` | Maximum lookback window allowed in hours | `24` | `.env` | No |
| `callback_connect_timeout_seconds` | `CALLBACK_CONNECT_TIMEOUT_SECONDS` | HTTP connect timeout when posting investigation results | `2.0` | `.env` | No |
| `callback_read_timeout_seconds` | `CALLBACK_READ_TIMEOUT_SECONDS` | HTTP read timeout when posting investigation results | `15.0` | `.env` | No |
| `callback_max_retries` | `CALLBACK_MAX_RETRIES` | Maximum retry attempts for result callback posting | `3` | `.env` | No |
| `callback_backoff_factor` | `CALLBACK_BACKOFF_FACTOR` | Exponential backoff multiplier for callback retries | `1.5` | `.env` | No |

---

## 3. Incident Bridge Configuration (`bridge`)

All bridge settings are defined in `bridge/app/config.py` via `BridgeConfig`. Every field can be supplied via environment variable (case-insensitive) or `.env` file.

| Setting Name | Environment Variable | Purpose | Default Value | Where Set | Required |
|---|---|---|---|---|---|
| `port` | `PORT` | HTTP port for Cloud Run / Uvicorn server | `8080` | Both (`.env`, `tfvars`) | No |
| `app_env` | `APP_ENV` | Application environment (`production`, `local`) | `production` | Both (`.env`, `tfvars`) | No |
| `environment` | `ENVIRONMENT` | Environment alias for `app_env` | `production` | Both (`.env`, `tfvars`) | No |
| `name_prefix` | `NAME_PREFIX` | Prefix applied to all created resource names and labels | `isa` | Both (`.env`, `tfvars`) | No |
| `service_name` | `SERVICE_NAME` | Service identifier reported in health checks and telemetry | `incident-bridge` | Both (`.env`, `tfvars`) | No |
| `gcp_project` | `GCP_PROJECT` or `PROJECT_ID` | GCP Project ID | Dynamic (env / ADC / metadata) | Both (`.env`, `tfvars`) | **Yes** (in prod) |
| `gcp_region` | `GCP_REGION` | Regional location for GCP resources | `us-central1` | Both (`.env`, `tfvars`) | No |
| `tasks_queue` | `TASKS_QUEUE` | Cloud Tasks queue name for investigations | Dynamic (`{name_prefix}-investigations`) | Both (`.env`, `tfvars`) | No |
| `tasks_location` | `TASKS_LOCATION` | Cloud Tasks regional location | `us-central1` | Both (`.env`, `tfvars`) | No |
| `tasks_invoker_sa` | `TASKS_INVOKER_SA` | Service account used by Cloud Tasks to invoke SRE agent | Dynamic (`tasks-invoker-sa@{project}...`) | Both (`.env`, `tfvars`) | No |
| `agent_service_url` | `AGENT_SERVICE_URL` | Private Cloud Run HTTPS URL of the SRE Agent service | `""` | Both (`.env`, `tfvars`) | **Yes** (in prod) |
| `agent_sa` | `AGENT_SA` | Identity email of the SRE Agent (for validating incoming callbacks) | Dynamic (`agent-sa@{project}...`) | Both (`.env`, `tfvars`) | No |
| `callback_url` | `CALLBACK_URL` | Callback URL where SRE Agent reports back results | `""` (defaults to bridge self URL) | Both (`.env`, `tfvars`) | No |
| `adapter_timeout_seconds` | `ADAPTER_TIMEOUT_SECONDS` | Timeout for outbound adapter calls to ticketing APIs | `30.0` | `.env` | No |
| `servicenow_chunk_size` | `SERVICENOW_CHUNK_SIZE` | Maximum character chunk size for ServiceNow work notes | `12000` | `.env` | No |
| `jira_chunk_size` | `JIRA_CHUNK_SIZE` | Maximum character chunk size for Jira comments | `32000` | `.env` | No |
| `servicenow_instance_url` | `SERVICENOW_INSTANCE_URL` | ServiceNow instance base URL | `None` | Both (`.env`, Secret) | If SN enabled |
| `servicenow_username` | `SERVICENOW_USERNAME` | ServiceNow basic auth username | `None` | Both (`.env`, Secret) | If SN enabled |
| `servicenow_password` | `SERVICENOW_PASSWORD` | ServiceNow basic auth password | `None` | Both (`.env`, Secret) | If SN enabled |
| `servicenow_webhook_token`| `SERVICENOW_WEBHOOK_TOKEN` | Bearer token verifying incoming ServiceNow webhooks | `None` | Both (`.env`, Secret) | If SN enabled |
| `servicenow_in_progress_state` | `SERVICENOW_IN_PROGRESS_STATE`| State integer string marking incident in progress | `2` | `.env` | No |
| `jira_base_url` | `JIRA_BASE_URL` | Atlassian Jira Cloud base URL | `None` | Both (`.env`, Secret) | If Jira enabled |
| `jira_email` | `JIRA_EMAIL` | Jira user email for basic auth | `None` | Both (`.env`, Secret) | If Jira enabled |
| `jira_api_token` | `JIRA_API_TOKEN` | Jira API token for basic auth | `None` | Both (`.env`, Secret) | If Jira enabled |
| `jira_webhook_token` | `JIRA_WEBHOOK_TOKEN` | Bearer token verifying incoming Jira webhooks | `None` | Both (`.env`, Secret) | If Jira enabled |
| `jira_in_progress_transition` | `JIRA_IN_PROGRESS_TRANSITION` | Transition name to move issue to In Progress | `In Progress` | `.env` | No |
| `generic_hmac_key` | `GENERIC_HMAC_KEY` | HMAC SHA256 shared secret for generic webhooks | `None` | Both (`.env`, Secret) | If Generic webhook |

### Callback Deduplication Registry (`CallbackDedupRegistry`)
The bridge maintains an in-memory thread-safe TTL deduplication cache (`CallbackDedupRegistry` in `bridge/app/core/dedup.py`) to prevent duplicate ticket updates upon agent retry attempts:
- Default TTL: 600.0 seconds (10 minutes).
- Deduplication Key: `(incident_id, status)` tuple.
- Note: For multi-instance Cloud Run scaling (`max_instance_count > 1`), configurable TTL externalization (`BRIDGE_CALLBACK_DEDUP_TTL_SECONDS`) and persistent distributed caching (Google Cloud Memorystore Redis or Firestore) are tracked in the engineering backlog.

---

## 4. Demo Environment Configuration (`demo`)

The demo microservices simulate a production e-commerce stack with failure injection scenarios.

| Setting Name | Environment Variable | Purpose | Default Value | Where Set | Required |
|---|---|---|---|---|---|
| `shop_api_port` | `PORT` | HTTP port for shop-api | `8080` | Both (`.env`, `tfvars`) | No |
| `inventory_svc_port` | `PORT` | HTTP port for inventory-svc | `8081` | Both (`.env`, `tfvars`) | No |
| `inventory_service_url` | `INVENTORY_SERVICE_URL` | Endpoint of inventory-svc called by shop-api | `http://localhost:8081` | Both (`.env`, `tfvars`) | No |
| `payment_gateway_key` | `PAYMENT_GATEWAY_KEY` | API secret key passed to payment gateway | `demo-live-payment-key-xyz` | Both (`.env`, `tfvars`) | No |
| `inventory_service_token`| `INVENTORY_SERVICE_TOKEN` | Bearer token used when calling inventory-svc | `valid-invoker-token` | Both (`.env`, `tfvars`) | No |
| `require_auth` | `REQUIRE_AUTH` | When true, inventory-svc verifies bearer token | `false` | Both (`.env`, `tfvars`) | No |
| `simulate_auth_failure` | `SIMULATE_AUTH_FAILURE` | When true, inventory-svc returns HTTP 401 | `false` | Both (`.env`, `tfvars`) | No |
| `bridge_url` | `BRIDGE_URL` | Target Incident Bridge endpoint for incident creation script | `http://localhost:8080` | `.env` / CLI args | No |
| `concurrency` | `CONCURRENCY` | Concurrency level for load generator | `5` | `.env` / CLI args | No |
| `rate` | `RATE` | Target requests per second for load generator | `10.0` | `.env` / CLI args | No |
| `duration` | `DURATION` | Load test duration in seconds (0 = infinite) | `0.0` | `.env` / CLI args | No |
| `report_interval` | `REPORT_INTERVAL` | Interval in seconds between loadgen metric summaries | `5.0` | `.env` / CLI args | No |
| `shop_api_cpu` | - | CPU limit for shop-api Cloud Run service | `1` | `tfvars` | No |
| `shop_api_memory` | - | Memory limit for shop-api Cloud Run service | `1Gi` | `tfvars` | No |

---

## 5. Terraform Variables Reference

Every infrastructure component exposes configurable sizing, resource names, and timeouts without hardcoded defaults.

### Agent Terraform (`agent/infra/terraform/variables.tf`)
- `name_prefix`: Prefix applied to all created resource names and labels (e.g. `"isa"`, `"acme"`) (Default: `"isa"`).
- `project_id`: GCP Project ID (**Required**).
- `region`: GCP region for Cloud Run, Cloud Tasks, and Artifact Registry (Default: `"us-central1"`).
- `vertex_location`: Vertex AI endpoint location (Default: `"global"`).
- `environment`: Environment tag (Default: `"production"`).
- `image_tag`: Container image tag for Cloud Run (Default: `"latest"` in Terraform; deployment scripts default to git short SHA).
- `repository_id`: Artifact Registry repository base ID (Default: `"sre-agent"`).
- `queue_name`: Cloud Tasks queue base name (Default: `"investigations"`).
- `service_name`: Cloud Run service base name (Default: `"sre-agent"`).
- `agent_service_account_id`: Service account base ID (Default: `"agent-sa"`).
- `tasks_invoker_service_account_id`: Service account base ID (Default: `"tasks-invoker-sa"`).
- `cloud_run_timeout_seconds`: Cloud Run request timeout (Default: `900`).
- `cloud_run_cpu`: CPU allocation (Default: `"2"`).
- `cloud_run_memory`: Memory allocation (Default: `"2Gi"`).
- `queue_max_attempts`: Cloud Tasks delivery retry count (Default: `2`).
- `model_dispatcher`: Gemini model for top-level investigation coordination (Default: `"gemini-3.8-flash"`).
- `model_synthesizer`: Gemini model for root cause synthesis (Default: `"gemini-3.8-flash"`).
- `model_specialist`: Gemini model for domain specialist subagents (Default: `"gemini-3.8-flash"`).
- `allowed_callback_hosts`: Comma-delimited wildcard hosts allowed for HTTP callbacks (Default: `"*.run.app"`).

### Bridge Terraform (`bridge/infra/terraform/variables.tf`)
- `name_prefix`: Prefix applied to all created resource names and labels (e.g. `"isa"`, `"acme"`) (Default: `"isa"`).
- `project_id`: GCP Project ID (**Required**).
- `region`: GCP region (Default: `"us-central1"`).
- `environment`: Environment tag (Default: `"production"`).
- `image_tag`: Container image tag for Cloud Run (Default: `"latest"` in Terraform; deployment scripts default to git short SHA).
- `tasks_queue_name`: Cloud Tasks queue name (Default: `"investigations"`, prefixed by `name_prefix` if un-prefixed).
- `tasks_invoker_service_account_email`: Email of Tasks invoker SA (**Required**, from agent outputs).
- `agent_service_url`: HTTPS URL of private SRE Agent Cloud Run (**Required**, from agent outputs).
- `agent_service_account_email`: Email of SRE Agent SA (**Required**, from agent outputs).
- `callback_url`: Override callback URL (Default: `""`).
- `repository_id`: Artifact Registry repo base ID (Default: `"incident-bridge"`).
- `service_name`: Cloud Run service base name (Default: `"incident-bridge"`).
- `bridge_service_account_id`: Service account base ID (Default: `"bridge-sa"`).
- `cloud_run_timeout_seconds`: Cloud Run timeout (Default: `300`).
- `cloud_run_cpu`: CPU allocation (Default: `"1"`).
- `cloud_run_memory`: Memory allocation (Default: `"512Mi"`).

### Demo Terraform (`demo/infra/terraform/variables.tf`)
- `name_prefix`: Prefix applied to all created resource names and labels (e.g. `"isa"`, `"acme"`) (Default: `"isa"`).
- `environment`: Environment tag (Default: `"demo"`).
- `project_id`: GCP Project ID (**Required**).
- `region`: GCP region (Default: `"us-central1"`).
- `image_tag`: Container image tag for demo services (Default: `"latest"` in Terraform; deployment scripts default to git short SHA).
- `shop_api_image`: Container image for shop-api (Default: regional Artifact Registry image tagged with `var.image_tag`).
- `inventory_svc_image`: Container image for inventory-svc (Default: regional Artifact Registry image tagged with `var.image_tag`).
- `shop_api_service_name`: Cloud Run service base name (Default: `"shop-api"`).
- `inventory_svc_service_name`: Cloud Run service base name (Default: `"inventory-svc"`).
- `shop_api_sa_account_id`: Service account base ID (Default: `"shop-api-sa"`).
- `cloud_run_timeout_seconds`: Timeout in seconds (Default: `300`).
- `cloud_run_cpu`: CPU allocation (Default: `"1"`).
- `cloud_run_memory`: Memory allocation (Default: `"512Mi"`).
- `payment_gateway_key`: Sensitive API key passed to shop-api (Default: `"demo-live-payment-key-xyz"`).
- `shop_api_cpu`: CPU limit for shop-api Cloud Run service (Default: `"1"`).
- `shop_api_memory`: Memory limit for shop-api Cloud Run service (Default: `"1Gi"`).
