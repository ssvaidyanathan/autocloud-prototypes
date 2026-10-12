# Master Deployment Guide

This guide describes how to deploy the entire Incident SRE Agent system across Google Cloud Platform, using either the master deployment orchestrator script (`scripts/deploy-all.sh`) or individual Terraform stacks (`agent/`, `bridge/`, `demo/`).

---

## 1. Configurable Resource Name Prefix & Common Labels

Every GCP resource created across all stacks uses a configurable naming prefix:
- **Variable**: `name_prefix` (Terraform) / `NAME_PREFIX` (Environment variable).
- **Default**: `"isa"`.
- **Custom Prefix**: e.g. `"acme"`, configured via `-var="name_prefix=acme"` or `NAME_PREFIX="acme"`.
- **Validation**:
  - Must start with a lowercase letter (`[a-z]`).
  - Alphanumeric lowercase characters and hyphens only (`[a-z0-9-]`).
  - Cannot end with a hyphen.
  - Maximum 10 characters length (regex: `^[a-z]([a-z0-9-]{0,8}[a-z0-9])?$`).

### Resource Composition
Resource names are uniformly composed as `"${var.name_prefix}-${base_name}"`:
- **Cloud Run Services**: `${var.name_prefix}-sre-agent`, `${var.name_prefix}-incident-bridge`, `${var.name_prefix}-shop-api`, `${var.name_prefix}-inventory-svc`.
- **Cloud Tasks Queue**: `${var.name_prefix}-investigations`.
- **Artifact Registry Repositories**: `${var.name_prefix}-sre-agent`, `${var.name_prefix}-incident-bridge`, `${var.name_prefix}-incident-demo`.
- **Service Accounts**: `${var.name_prefix}-agent-sa`, `${var.name_prefix}-tasks-invoker-sa`, `${var.name_prefix}-bridge-sa`, `${var.name_prefix}-shop-api-sa` (strictly <= 30 characters).
- **Secret Manager Secret IDs**: `${var.name_prefix}-bridge-servicenow-username`, `${var.name_prefix}-bridge-jira-api-token`, etc.

### Common Labels
All labelable resources (Cloud Run services, Artifact Registry repositories, Secret Manager secrets) automatically receive standard labels:
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

---

## 2. Terraform State Management in GCS

The infrastructure stacks (`agent/`, `bridge/`, `demo/`) use a partial GCS backend:
```hcl
terraform {
  backend "gcs" {}
}
```
Stack configurations do not hardcode bucket names. Instead, backend configuration is supplied via `backend.hcl` or CLI flags during initialization.

### Step 0b: Bootstrap State Bucket (`scripts/bootstrap-tfstate.sh`)

Before applying Terraform stacks in a cloud environment, bootstrap the dedicated GCS state bucket:

```bash
# Dry run to inspect bucket bootstrap settings:
./scripts/bootstrap-tfstate.sh --project-id "your-project-id" --name-prefix "isa" --dry-run

# Create / verify the bucket:
./scripts/bootstrap-tfstate.sh --project-id "your-project-id" --name-prefix "isa" --region "us-central1"
```

The bootstrap script enforces enterprise security guardrails:
- **Uniform Bucket-Level Access**: Disables per-object ACLs in favor of IAM.
- **Public Access Prevention**: Enforces `enforced` public access prevention on the bucket.
- **Object Versioning**: Enables object versioning for automatic state snapshotting and point-in-time recovery.
- **Resource Labels**: Tags the bucket with `app=incident-sre-agent`, `component=tfstate`, `env=prod`, and `prefix=${NAME_PREFIX}`.
- **Idempotency**: Safe to re-run; verifies existing bucket settings and updates policies without touching state objects.

### Backend Configuration (`backend.hcl`)

Each Terraform directory includes a `backend.hcl.example` template:
```hcl
bucket = "your-project-id-isa-tfstate"
prefix = "isa/agent" # or "isa/bridge", "isa/demo"
```

To configure a stack manually:
```bash
cp agent/infra/terraform/backend.hcl.example agent/infra/terraform/backend.hcl
# Edit backend.hcl with your actual GCS bucket name and desired prefix
terraform -chdir=agent/infra/terraform init -backend-config=backend.hcl
```
`backend.hcl` is excluded from git in `.gitignore`.

### CI Compatibility & Local State

In CI pipelines or pre-deployment validation gates, initialize with `-backend=false`:
```bash
terraform -chdir=agent/infra/terraform init -backend=false
terraform -chdir=agent/infra/terraform validate
```
This enables full schema validation and formatting checks without requiring GCS bucket credentials or cloud access.

### State Recovery from Object Versioning

Because object versioning is enabled on the state bucket, previous state versions are retained across all operations:

1. **List State Versions**:
   ```bash
   gcloud storage objects list --show-version-in-preview "gs://${TFSTATE_BUCKET}/isa/agent/"
   ```
2. **Recover Historical State Generation**:
   ```bash
   # Retrieve a specific generation:
   gcloud storage cp "gs://${TFSTATE_BUCKET}/isa/agent/default.tfstate#<GENERATION_NUMBER>" ./recovered.tfstate

   # Push recovered state back to the active bucket:
   terraform -chdir=agent/infra/terraform state push ./recovered.tfstate
   ```

---

## 3. Automated Master Deployment (`scripts/deploy-all.sh`)

The recommended method for complete end-to-end deployment is the master orchestrator script:

```bash
# Dry run to preview deployment steps:
./scripts/deploy-all.sh \
  --project-id "your-project-id" \
  --name-prefix "isa" \
  --tfstate-bucket "your-project-id-isa-tfstate" \
  --dry-run

# Live deployment with optional demo environment:
./scripts/deploy-all.sh \
  --project-id "your-project-id" \
  --region "us-central1" \
  --name-prefix "isa" \
  --tfstate-bucket "your-project-id-isa-tfstate" \
  --with-demo
```

### Options
- `-p, --project-id <id>`: GCP Project ID (**Required**).
- `-r, --region <region>`: GCP region (Default: `us-central1`).
- `-n, --name-prefix <pfx>`: Resource name prefix (Default: `isa`).
- `-b, --tfstate-bucket <bkt>`: GCS bucket for Terraform remote state.
- `--no-backend`: Disable remote GCS backend (initialize with `-backend=false`).
- `-t, --image-tag <tag>`: Container image tag (Default: git short SHA of HEAD, e.g. `$(git rev-parse --short HEAD)`, falling back to `latest` if git is unavailable).
- `--with-demo`: Deploy optional demo microservices (`shop-api` & `inventory-svc`).
- `--skip-build`: Skip container image builds and pushes.
- `--dry-run`: Preview deployment plan without applying changes.

### Immutable Image Tagging & Traceability
All build and deployment scripts (`agent/scripts/build-push.sh`, `agent/scripts/deploy.sh`, `bridge/scripts/build-push.sh`, `bridge/scripts/deploy.sh`, `scripts/deploy-all.sh`) standardise `IMAGE_TAG` to default to the git short SHA of the deployment commit (`$(git rev-parse --short HEAD)`), falling back to `latest` only when git metadata is unavailable:
- **Traceability**: Cloud Run revisions are tied directly to immutable git commits rather than mutable `:latest` tags.
- **Manual Overrides**: Explicit `IMAGE_TAG` or `TAG_NAME` environment variables and the `-t / --image-tag` flag override the default whenever a custom tag is required.

---

## 4. Manual Stack-by-Stack Deployment

### Step 0: Bootstrap State Bucket
```bash
./scripts/bootstrap-tfstate.sh --project-id "your-project-id" --name-prefix "isa"
```

### Step 1: Deploy Agent Infrastructure (`agent/infra/terraform`)

```bash
cd agent/infra/terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
# Update terraform.tfvars and backend.hcl with project_id, name_prefix, and tfstate bucket

terraform init -backend-config=backend.hcl
terraform apply -target=module.apis -target=module.iam -target=module.artifact_registry -target=module.tasks_queue
```

Build and push the agent image:
```bash
cd ../../..
env PROJECT_ID="your-project-id" NAME_PREFIX="isa" bash agent/scripts/build-push.sh
```

Apply the Cloud Run service:
```bash
cd agent/infra/terraform
terraform apply
```

Capture the outputs:
```bash
AGENT_SERVICE_URL=$(terraform output -raw agent_service_url)
AGENT_SA_EMAIL=$(terraform output -raw agent_service_account_email)
TASKS_INVOKER_SA_EMAIL=$(terraform output -raw tasks_invoker_service_account_email)
TASKS_QUEUE_NAME=$(terraform output -raw tasks_queue_name)
```

### Step 2: Deploy Bridge Infrastructure (`bridge/infra/terraform`)

```bash
cd ../../../bridge/infra/terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl
# Populate terraform.tfvars with agent outputs, name_prefix, and backend.hcl bucket

terraform init -backend-config=backend.hcl
terraform apply -target=module.apis -target=module.iam -target=module.artifact_registry -target=module.secrets
```

Populate secrets in Secret Manager:
```bash
cd ../../..
env PROJECT_ID="your-project-id" NAME_PREFIX="isa" bash bridge/scripts/set-secrets.sh
```

Build and push the bridge image:
```bash
env PROJECT_ID="your-project-id" NAME_PREFIX="isa" bash bridge/scripts/build-push.sh
```

Apply the bridge Cloud Run service:
```bash
cd bridge/infra/terraform
terraform apply
```

Capture the bridge URL:
```bash
BRIDGE_SERVICE_URL=$(terraform output -raw bridge_service_url)
```

### Step 3 (Optional): Deploy Demo Stack (`demo/infra/terraform`)

```bash
cd ../../../demo/infra/terraform
cp terraform.tfvars.example terraform.tfvars
cp backend.hcl.example backend.hcl

terraform init -backend-config=backend.hcl
terraform apply
```

---

## 5. Post-Deployment Verification (`scripts/smoke-test.sh`)

Validate the deployment with the automated smoke test suite:

```bash
./scripts/smoke-test.sh \
  --name-prefix "isa" \
  --bridge-url "${BRIDGE_SERVICE_URL}" \
  --agent-url "${AGENT_SERVICE_URL}" \
  --generic-hmac-key "your-hmac-key"
```

The smoke test checks:
1. Bridge Health Check (`GET /health` -> 200). Note: `/health` is used because Google Frontend (GFE) edge proxies reserve and intercept `/healthz` on Cloud Run, returning a Google 404 HTML error page before requests reach container instances.
2. Webhook Ingestion Authentication Rejection (401 on unauthenticated calls).
3. Duplicate Webhook Idempotency (Deterministic deduplication).
4. Private SRE Agent Invocation Protection (401/403 unauthenticated, direct `/health` probe).
5. Callback Mutual Authentication Rejection (401 for invalid OIDC tokens).
6. End-to-End Investigation Flow (Read-only probe ticket dispatch).
