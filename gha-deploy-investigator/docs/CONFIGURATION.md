# Configuration Reference: GitHub Actions Deployment Failure Investigator

This document provides a comprehensive configuration reference for `gha-deploy-investigator`, including environment variables, GitHub App manifest and setup instructions, and ServiceNow integration settings.

---

## 1. Environment Variables Reference

### 1.1 GDI Bridge (`bridge`)

| Variable | Type | Default | Description |
|---|---|---|---|
| `PORT` | Integer | `8080` | Port the HTTP service listens on |
| `ENVIRONMENT` / `APP_ENV` | String | `production` | Deployment environment (`production`, `staging`, `dev`, `local`) |
| `NAME_PREFIX` | String | `gdi` | Resource name prefix applied to GCP resources |
| `GCP_PROJECT` | String | *(ADC / Metadata)* | Target Google Cloud Platform project ID |
| `GCP_REGION` | String | `us-central1` | GCP region for Cloud Run and Cloud Tasks |
| `TASKS_QUEUE` | String | `investigations` | Cloud Tasks queue name for asynchronous task dispatch |
| `TASKS_LOCATION` | String | `us-central1` | Region where the Cloud Tasks queue resides |
| `TASKS_INVOKER_SA` | String | `""` | Service account email used for Cloud Tasks OIDC invocation |
| `AGENT_SERVICE_URL` | String | `""` | URL of the internal `gdi-agent` Cloud Run service |
| `CALLBACK_URL` | String | `""` | Public URL where `gdi-agent` posts completed investigation results |
| `GITHUB_WEBHOOK_SECRET` | String (Secret) | `None` | Shared secret key used to verify `X-Hub-Signature-256` HMAC headers |
| `GITHUB_TOKEN` | String (Secret) | `None` | Optional personal access token (used if GitHub App not active) |
| `GITHUB_API_URL` | String | `https://api.github.com` | Base URL of the GitHub REST API |
| `ENABLE_GITHUB_WRITEBACK` | Boolean | `true` | Whether to create/update GitHub Check Runs and PR comments |
| `SERVICENOW_INSTANCE_URL` | String | `None` | ServiceNow base URL (e.g. `https://dev12345.service-now.com`) |
| `SERVICENOW_USERNAME` | String | `None` | ServiceNow integration user account |
| `SERVICENOW_PASSWORD` | String (Secret) | `None` | ServiceNow integration user password |
| `SERVICENOW_WEBHOOK_TOKEN` | String (Secret) | `None` | Optional token for ServiceNow inbound webhooks |
| `SERVICENOW_IN_PROGRESS_STATE` | String | `"2"` | State integer value assigned to newly created incidents |

### 1.2 GDI Agent (`agent`)

| Variable | Type | Default | Description |
|---|---|---|---|
| `PORT` | Integer | `8080` | Port the internal HTTP service listens on |
| `ENVIRONMENT` / `APP_ENV` | String | `production` | Deployment environment |
| `NAME_PREFIX` | String | `gdi` | Resource name prefix |
| `GCP_PROJECT` | String | *(ADC / Metadata)* | Target Google Cloud Platform project ID |
| `BRIDGE_SERVICE_URL` | String | `""` | URL of the `gdi-bridge` service for posting results |
| `GITHUB_API_URL` | String | `https://api.github.com` | GitHub REST API base URL |
| `GITHUB_APP_ID` | String | `None` | Numerical GitHub App ID |
| `GITHUB_APP_PRIVATE_KEY` | String (Secret) | `None` | PEM-encoded RSA private key for GitHub App authentication |
| `GITHUB_TOKEN` | String (Secret) | `None` | Optional fallback personal access token |

---

## 2. GitHub App Setup Guide (Personal App for `ssvaidyanathan`)

Per project architecture (Amendment 7), the GitHub integration runs via a personal GitHub App owned by user `ssvaidyanathan`.

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
gcloud secrets versions add gdi-servicenow-password --data-file=<(printf "%s" "YOUR_SERVICENOW_PASSWORD") --project="${PROJECT_ID}"
```

### 3.2 Required ServiceNow User Roles
The integration user specified in `SERVICENOW_USERNAME` must possess the following roles:
- `itil`: Grants permissions to create and update records on the `incident` table.
- `soap_ecc` or attachment permissions: Allows uploading files via the Attachment API (`POST /api/now/attachment/file`).

### 3.3 Created Incident Attributes
- **Table**: `incident`
- **Short Description**: `CI/CD Failure: <repository> - <job_name> (<stage>)`
- **Category**: `Software`
- **Subcategory**: `CI/CD Pipeline`
- **State**: `2` (In Progress)
- **Work Notes**: Formatted markdown analysis with probable root cause, evidence table, and suggested remediation commands.
- **Attachment**: `investigation-report.html` containing interactive styling, failure timeline, and specialist telemetry breakdown.


---

## 4. Terraform Secret Configuration & Naming Overrides

Both Terraform stacks support customizable Secret Manager resource IDs to facilitate seamless adoption of pre-existing secrets (e.g. console-created secrets in `cloud-assist-fde-7` per AMENDMENT 8).

### 4.1 Bridge Stack Secret Variables (`bridge/infra/terraform/variables.tf`)

| Variable | Default Value | Description |
|---|---|---|
| `github_webhook_secret_id` | `""` (resolves to `${name_prefix}-github-webhook-secret`) | Secret Manager secret ID for GitHub webhook HMAC secret |
| `servicenow_password_secret_id` | `""` (resolves to `${name_prefix}-servicenow-password`) | Secret Manager secret ID for ServiceNow password (e.g. `gdi-bridge-servicenow-password`) |
| `servicenow_webhook_token_secret_id` | `""` (resolves to `${name_prefix}-servicenow-webhook-token`) | Secret Manager secret ID for ServiceNow webhook token (e.g. `gdi-bridge-servicenow-webhook-token`) |
| `servicenow_username_secret_id` | `""` | Optional Secret Manager secret ID for ServiceNow username (e.g. `gdi-bridge-servicenow-username`). When specified, the username is mounted directly from Secret Manager into Cloud Run rather than passed via plain text variable. |

### 4.2 Agent Stack Secret Variables (`agent/infra/terraform/variables.tf`)

| Variable | Default Value | Description |
|---|---|---|
| `github_app_private_key_secret_id` | `""` (resolves to `${name_prefix}-github-app-private-key`) | Secret Manager secret ID for GitHub App RSA private key |
