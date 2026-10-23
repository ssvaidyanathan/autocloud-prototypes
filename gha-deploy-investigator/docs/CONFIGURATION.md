# Configuration Reference: GitHub Actions Deployment Failure Investigator

This document provides a comprehensive configuration reference for `gha-deploy-investigator`, including environment variables, GitHub App manifest and setup instructions, ServiceNow integration settings, and Terraform mappings.

---

## 1. Environment Variables Reference

### 1.1 GDI Bridge (`bridge`)

The bridge service receives GitHub webhook events, verifies HMAC signatures, dispatches async investigations to Cloud Tasks, and performs bi-directional write-back to ServiceNow and GitHub.

| Variable | Type | Default | Required | Terraform Mapping | Description |
|---|---|---|---|---|---|
| `PORT` | Integer | `8080` | No | Cloud Run container port | Port the HTTP service listens on |
| `ENVIRONMENT` / `APP_ENV` | String | `production` | No | `var.environment` | Deployment environment (`production`, `staging`, `dev`, `local`) |
| `NAME_PREFIX` | String | `gdi` | No | `var.name_prefix` | Resource name prefix applied to GCP resources and secrets |
| `SERVICE_NAME` | String | `gdi-bridge` | No | `var.service_name` | Cloud Run service identifier |
| `GCP_PROJECT` | String | *(ADC / Metadata)* | Yes (in GCP) | `var.project_id` | Target Google Cloud Platform project ID (aliases: `PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`) |
| `GCP_REGION` | String | `us-central1` | No | `var.region` | GCP region for Cloud Run and Cloud Tasks |
| `TASKS_QUEUE` | String | `investigations` | Yes (in GCP) | `google_cloud_tasks_queue.tasks_queue.name` (`var.tasks_queue_name`) | Cloud Tasks queue name for asynchronous investigation dispatch |
| `TASKS_LOCATION` | String | `us-central1` | No | `var.region` | Region where the Cloud Tasks queue resides |
| `TASKS_INVOKER_SA` | String | `""` | Yes (in GCP) | `var.tasks_invoker_service_account_email` | Service account email used for Cloud Tasks OIDC invocation of internal agent |
| `AGENT_SERVICE_URL` | String | `""` | Yes (in GCP) | `var.agent_service_url` | Target URL of the internal `gdi-agent` Cloud Run service |
| `AGENT_SA` | String | `""` | No | `var.agent_service_account_email` | Service account email of internal agent for identity verification |
| `CALLBACK_URL` | String | `""` | Yes (in GCP) | `var.callback_url` | Public URL where `gdi-agent` posts completed investigation results |
| `ADAPTER_TIMEOUT_SECONDS` | Float | `60.0` | No | `var.adapter_timeout_seconds` | HTTP request timeout in seconds for outbound ServiceNow and GitHub adapter calls |
| `GITHUB_WEBHOOK_SECRET` | String (Secret) | `None` | Yes (in prod) | Secret `${var.name_prefix}-github-webhook-secret` | Shared secret key used to verify `X-Hub-Signature-256` HMAC inbound webhook headers |
| `GITHUB_TOKEN` | String (Secret) | `None` | No | Secret / Environment | Optional fallback personal access token (used if GitHub App not active) |
| `GITHUB_API_URL` | String | `https://api.github.com` | No | Container default | Base URL of the GitHub REST API |
| `ENABLE_GITHUB_WRITEBACK` | Boolean | `true` | No | Container default | Whether to create/update GitHub Check Runs and PR/commit comments |
| `GITHUB_APP_ID` | String | `None` | Yes (for GitHub App) | `var.github_app_id` | Numeric GitHub App ID installed on target repositories |
| `GITHUB_APP_PRIVATE_KEY` | String (Secret) | `None` | Yes (for GitHub App) | Secret `${var.name_prefix}-github-app-private-key` | PEM-encoded RSA private key for GitHub App authentication and Check Run creation |
| `SERVICENOW_INSTANCE_URL` | String | `None` | Yes (in prod) | `var.servicenow_instance_url` | ServiceNow base URL (e.g. `https://dev12345.service-now.com`) |
| `SERVICENOW_USERNAME` | String (Secret) | `None` | Yes (in prod) | Secret `${var.name_prefix}-servicenow-username` | ServiceNow integration username |
| `SERVICENOW_PASSWORD` | String (Secret) | `None` | Yes (in prod) | Secret `${var.name_prefix}-servicenow-password` | ServiceNow integration user password |
| `SERVICENOW_WEBHOOK_TOKEN` | String (Secret) | `None` | No | Secret `${var.name_prefix}-servicenow-webhook-token` | Optional bearer token for ServiceNow inbound webhooks |
| `SERVICENOW_IN_PROGRESS_STATE` | String | `"2"` | No | Container default | State integer assigned to newly created incidents (`"2"` = In Progress) |
| `SERVICENOW_INCIDENT_STATE_AFTER_REPORT` | String | `"2"` | No | `var.servicenow_incident_state_after_report` | Target incident state integer when findings and report attachments are uploaded (`"2"` = In Progress) |
| `SERVICENOW_CORRELATION_DISPLAY` | String | `"gha-deploy-investigator"` | No | Container default | Correlation display tag set on created ServiceNow incidents for bi-directional exclusion |
| `SERVICENOW_CALLER` | String | `None` (`"admin"`) | No | `var.servicenow_caller` (omitted if `null` to use app default) | Configurable user ID / username assigned to `caller_id` on created incidents (defaults to `"admin"` in app; configurable via `var.servicenow_caller`) |
| `SERVICENOW_ASSIGNMENT_GROUP` | String | `None` | No | Container default | Optional assignment group sys_id or group name for routing created incidents |

---

### 1.2 GDI Agent (`agent`)

The agent service runs behind internal ingress, coordinates autonomous ADK multi-agent specialists, discovers runtime troubleshooting skills, queries Cloud Logging, and synthesizes root-cause diagnoses using Vertex AI Gemini.

| Variable | Type | Default | Required | Terraform Mapping | Description |
|---|---|---|---|---|---|
| `PORT` | Integer | `8080` | No | Cloud Run container port | Port the internal HTTP service listens on |
| `ENVIRONMENT` / `APP_ENV` | String | `production` | No | `var.environment` | Deployment environment |
| `NAME_PREFIX` | String | `gdi` | No | `var.name_prefix` | Resource name prefix |
| `SERVICE_NAME` | String | `gdi-agent` | No | `var.service_name` | Cloud Run service identifier |
| `GCP_PROJECT` | String | *(ADC / Metadata)* | Yes (in GCP) | `var.project_id` | Target Google Cloud Platform project ID (aliases: `PROJECT_ID`, `GOOGLE_CLOUD_PROJECT`) |
| `BRIDGE_SERVICE_URL` | String | `""` | Yes (in GCP) | `var.bridge_service_url` | URL of the `gdi-bridge` callback endpoint for reporting results |
| `GITHUB_API_URL` | String | `https://api.github.com` | No | Container default | GitHub REST API base URL |
| `GITHUB_APP_ID` | String | `None` | Yes (for GitHub App) | `var.github_app_id` | Numeric GitHub App ID used for authenticated repo API queries |
| `GITHUB_APP_PRIVATE_KEY` | String (Secret) | `None` | Yes (for GitHub App) | Secret `${var.name_prefix}-github-app-private-key` | PEM-encoded RSA private key for GitHub App authentication |
| `GITHUB_TOKEN` | String (Secret) | `None` | No | Secret / Environment | Optional fallback personal access token |
| `GEMINI_MODEL` | String | *None* | Yes | `var.gemini_model` | Vertex AI Gemini model name for diagnostic synthesis |
| `VERTEX_AI_LOCATION` | String | `us-central1` | No | `var.vertex_ai_location` | GCP region for Vertex AI Gemini API endpoint |

#### LLM & SDK Inference Settings (Read in `base.py` & Tool Clients)

| Variable | Type | Default | Required | Description |
|---|---|---|---|---|
| `GOOGLE_CLOUD_PROJECT` | String | *(ADC / Metadata)* | Yes (in GCP) | Project ID used for Vertex AI Gemini model initialization and Cloud Logging queries |
| `GOOGLE_GENAI_USE_VERTEXAI` | Boolean String | Auto (`true` in GCP) | No | Set to `"true"` or `"1"` to force Google GenAI SDK to route through Vertex AI endpoints |
| `GOOGLE_GENAI_USE_ENTERPRISE` | Boolean String | `"false"` | No | Set to `"true"` or `"1"` to enable Vertex AI enterprise endpoint routing |
| `GOOGLE_APPLICATION_CREDENTIALS` | Path | `None` | No | Path to service account JSON key file for local testing or non-GCP hosting |
| `GOOGLE_API_KEY` | String (Secret) | `None` | No | Direct Google AI Studio API key (used if running offline/local outside Vertex AI) |
| `GEMINI_API_KEY` | String (Secret) | `None` | No | Alias for `GOOGLE_API_KEY` for Google AI Studio API access |

### 1.3 Terraform Remote State & GCS Backend

All 3 Terraform stacks (`agent`, `bridge`, `demo`) define a partial GCS backend:
```hcl
terraform {
  backend "gcs" {}
}
```
- **State Bucket**: Bootstrapped via `scripts/bootstrap-tfstate.sh` with uniform bucket-level access, public access prevention, object versioning, and common labels (`app=gha-deploy-investigator`, `component=tfstate`, `env=${ENVIRONMENT}`, `prefix=${NAME_PREFIX}`).
- **Backend Configuration**: Provided via `backend.hcl` (`bucket = "..."`, `prefix = "gdi/agent"`) or CLI flag `-backend-config`.
- **CI & Local Validation**: Pre-deployment quality gates and CI use `terraform init -backend=false` to validate syntax and schemas without cloud dependencies.
- **State Migration**: For existing deployments migrating from local state, initialize each stack with `terraform init -migrate-state -backend-config=backend.hcl` and verify with `terraform plan` (see [README.md Section 11](../README.md#migrating-existing-local-state)).
- **Disaster Recovery**: State recovery is enabled by GCS object versioning.

> [!WARNING]
> **Do not delete local state files prematurely**:
> Do not delete or remove local `terraform.tfstate` or `terraform.tfstate.backup` files until `terraform plan` reports `No changes` across all stacks and state integrity is verified. Keep local backups until the remote backend is fully verified in your team workflow.

---

## 2. GitHub App Setup Guide (Personal App for `ssvaidyanathan`)

Per project architecture, the GitHub integration runs via a personal GitHub App owned by user `ssvaidyanathan`.

### 2.1 App Manifest YAML

You can create the GitHub App automatically using GitHub's Manifest flow:

```yaml
name: gdi-deploy-investigator
url: https://github.com/ssvaidyanathan/autocloud-prototypes
description: Autonomous GitHub Actions deployment failure investigator and ServiceNow incident responder.
hook_attributes:
  url: https://gdi-bridge-REPLACE_ME-uc.a.run.app/webhook/github
  active: true
redirect_url: https://github.com/ssvaidyanathan/autocloud-prototypes
public: false
default_permissions:
  actions: read
  checks: write
  pull_requests: write
  contents: read
default_events:
  - workflow_job
  - workflow_run
  - check_run
```

### 2.2 Permissions Summary

| Permission | Access | Justification |
|---|---|---|
| **Actions** | `Read-only` | Download workflow run metadata, job step status, and log archives |
| **Checks** | `Read & write` | Create and update Check Runs with diagnostic summaries and ServiceNow links |
| **Pull requests** | `Read & write` | Post diagnostic findings and ServiceNow incident links on PRs |
| **Contents** | `Read-only` | Fetch repository files, commit diffs, and workflow definitions |
| **Metadata** | `Read-only` | Basic repository metadata (mandatory for all GitHub Apps) |

### 2.3 Post-Creation Configuration
1. **Download Private Key**: Generate a private key (`.pem` format) from the GitHub App settings page.
2. **Retrieve App ID**: Note the numeric `App ID` from the General settings page.
3. **Set Webhook Secret**: Configure a secure random token in the GitHub App webhook secret setting.
4. **Install App**: Install the App on target repositories (e.g. `ssvaidyanathan/gdi-sample-pipeline-app` or organizational repositories).
5. **Populate Secret Manager**:
   ```bash
   gcloud secrets versions add gdi-github-webhook-secret --data-file=<(printf "%s" "YOUR_WEBHOOK_SECRET") --project="${PROJECT_ID}"
   gcloud secrets versions add gdi-github-app-private-key --data-file=/path/to/private-key.pem --project="${PROJECT_ID}"
   ```

---

## 3. ServiceNow Configuration

The reverse write-back flow creates incidents in ServiceNow and uploads full HTML reports as attachments.

### 3.1 ServiceNow Credentials & Secret Manager
Populate the ServiceNow integration credentials into Secret Manager:
```bash
# If copying from existing incident-sre-agent secrets:
./scripts/copy-secrets.sh --project-id "${PROJECT_ID}" --source-prefix isa --target-prefix gdi

# Or set directly:
gcloud secrets versions add gdi-servicenow-username --data-file=<(printf "%s" "YOUR_SERVICENOW_USERNAME") --project="${PROJECT_ID}"
gcloud secrets versions add gdi-servicenow-password --data-file=<(printf "%s" "YOUR_SERVICENOW_PASSWORD") --project="${PROJECT_ID}"
gcloud secrets versions add gdi-servicenow-webhook-token --data-file=<(printf "%s" "YOUR_SERVICENOW_TOKEN") --project="${PROJECT_ID}"
```

### 3.2 Required ServiceNow User Roles
The integration user specified in `SERVICENOW_USERNAME` must possess the following roles:
- `itil`: Grants permissions to create and update records on the `incident` table.
- `soap_ecc` or attachment permissions: Allows uploading files via the Attachment API (`POST /api/now/attachment/file`).

### 3.3 Created Incident Attributes
- **Table**: `incident`
- **Short Description**: `[gdi-demo] [CI/CD Failure] <repository> (<stage>)`
- **Caller (`caller_id`)**: Configurable via `SERVICENOW_CALLER` (defaults to `"admin"`)
- **Category**: `inquiry`
- **Severity / Urgency / Impact**: `3` / `2` / `2` (P3/P2)
- **State**: `2` (In Progress; configurable via `SERVICENOW_IN_PROGRESS_STATE` and `SERVICENOW_INCIDENT_STATE_AFTER_REPORT`)
- **Correlation Display**: `gha-deploy-investigator` (identifies GDI-created tickets for bi-directional exclusion)
- **Correlation ID**: `gdi:<owner>/<repo>:<run_id>:<run_attempt>` (ensures query-before-create idempotency)
- **Description**: Facts-only plain-text summary with repository, branch, short commit SHA, GitHub actor, failing stage, and workflow run URL (root cause, remediation, observations, and pointer lines are excluded; all diagnostic analysis lives strictly in work notes and the attachment).
- **Attachment**: `ci_cd_investigation_report_<INCIDENT_NUMBER>.html` containing interactive styling, failure timeline, and specialist telemetry breakdown.

---

## 4. Terraform-Managed Secret Architecture (5 Secrets)

Terraform manages resource definitions and IAM bindings for 5 canonical secrets across the Agent and Bridge stacks. Secret versions and payload values are never stored in Terraform state or source code.

| Stack | Secret ID | Consumer | Purpose |
|---|---|---|---|
| **Agent (Owner) & Bridge (Reader)** | `${name_prefix}-github-app-private-key` | `gdi-agent-sa`, `gdi-bridge-sa` | PEM-encoded RSA private key for GitHub App authentication (owned and provisioned by Agent stack; Bridge stack references via data source and binds IAM accessor) |
| **Bridge** | `${name_prefix}-github-webhook-secret` | `gdi-bridge-sa` | Shared secret for verifying `X-Hub-Signature-256` inbound webhook HMACs |
| **Bridge** | `${name_prefix}-servicenow-username` | `gdi-bridge-sa` | Integration user for ServiceNow Table and Attachment REST APIs |
| **Bridge** | `${name_prefix}-servicenow-password` | `gdi-bridge-sa` | Basic auth password for ServiceNow REST APIs |
| **Bridge** | `${name_prefix}-servicenow-webhook-token` | `gdi-bridge-sa` | Bearer token for verifying inbound ServiceNow webhook callbacks |

> [!NOTE]
> **Secret Ownership & Deployment Ordering**: The `${name_prefix}-github-app-private-key` secret resource is declared and provisioned exclusively in `agent/infra/terraform`. The `bridge/infra/terraform` stack references it via `data.google_secret_manager_secret` and provisions the `bridge_gh_key_access` IAM binding. This requires that the Agent stack be applied before the Bridge stack on fresh deployments.

