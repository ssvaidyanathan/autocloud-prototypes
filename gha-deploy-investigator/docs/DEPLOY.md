# Deployment Guide: GitHub Actions Deployment Failure Investigator

This guide details the step-by-step process for provisioning, configuring, and deploying the **GitHub Actions Deployment Failure Investigator (`gha-deploy-investigator`)** onto Google Cloud Platform using Terraform and Cloud Run.

---

## 1. Prerequisites

Before beginning deployment, ensure you have:
1. **Google Cloud SDK (`gcloud`)** installed and authenticated:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   ```
2. **Terraform (`>= 1.5.0`)** installed:
   ```bash
   terraform version
   ```
3. **Target GCP Project & Permissions**:
   - `roles/owner` or combination of `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/secretmanager.admin`, and `roles/cloudtasks.admin`.
4. **Environment Variables**:
   ```bash
   export PROJECT_ID="your-target-project-id"
   export REGION="us-central1"
   export NAME_PREFIX="gdi"
   ```

---

## 2. Deployment Sequencing & Secret Management Invariant

> [!IMPORTANT]
> **Cloud Run Secret Dependency**: In Google Cloud Platform, a Cloud Run service that mounts a Secret Manager secret via `secret_key_ref` (referencing `version = "latest"`) **fails to deploy** if the secret exists but has no enabled versions.
> 
> To maintain the **Zero Secret Values in Git/Terraform State** invariant, Terraform manages only the Secret Manager resource definitions and IAM role bindings. It never manages versions or payload values.
> 
> Therefore, deployment must proceed in three strictly sequenced stages:
> 1. **Stage A**: Terraform provisions the Secret Manager secret resources, IAM accessors, and Cloud Tasks queue.
> 2. **Stage B**: The administrator populates secret values/versions using `gcloud secrets versions add` or the GCP Console.
> 3. **Stage C**: Container images are published and Cloud Run services are deployed.

---

## 3. Stage A: Provision Secret Resources & IAM via Terraform

The standard deployment defines 4 Secret Manager secret resources across the two stacks:
- **Agent Stack**: `${NAME_PREFIX}-github-app-private-key`
- **Bridge Stack**: `${NAME_PREFIX}-github-webhook-secret`, `${NAME_PREFIX}-servicenow-password`, `${NAME_PREFIX}-servicenow-webhook-token`

To provision these secret resources and their IAM permissions before deploying Cloud Run:

```bash
cd gha-deploy-investigator

# 1. Initialize Terraform stacks
terraform -chdir=agent/infra/terraform init
terraform -chdir=bridge/infra/terraform init

# 2. Provision Agent stack secret resource and service accounts
terraform -chdir=agent/infra/terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -target=google_secret_manager_secret.github_app_private_key \
    -target=google_service_account.agent_sa \
    -target=google_service_account.tasks_invoker_sa \
    -target=google_secret_manager_secret_iam_member.agent_gh_key_access \
    -target=google_project_iam_member.agent_vertex_ai \
    -target=google_project_iam_member.agent_logging_viewer \
    -target=google_project_iam_member.agent_cloud_run_viewer

# 3. Provision Bridge stack secret resources, service account, and Cloud Tasks queue
terraform -chdir=bridge/infra/terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -target=google_secret_manager_secret.github_webhook_secret \
    -target=google_secret_manager_secret.servicenow_password \
    -target=google_secret_manager_secret.servicenow_webhook_token \
    -target=google_service_account.bridge_sa \
    -target=google_cloud_tasks_queue.tasks_queue \
    -target=google_cloud_tasks_queue_iam_member.bridge_task_enqueuer \
    -target=google_secret_manager_secret_iam_member.bridge_gh_secret_access \
    -target=google_secret_manager_secret_iam_member.bridge_snow_pwd_access \
    -target=google_secret_manager_secret_iam_member.bridge_snow_token_access
```

---

## 4. Stage B: Populate Secret Values

Once the secret resources exist in Secret Manager, populate enabled versions for all 4 secrets. Secret values are never entered into Terraform or committed to source control.

### Option 1: Populate via `gcloud` CLI
```bash
# 1. GitHub App Private Key (Agent)
gcloud secrets versions add "${NAME_PREFIX}-github-app-private-key" \
    --data-file=/path/to/github-app-private-key.pem \
    --project="${PROJECT_ID}"

# 2. GitHub Webhook Secret (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-github-webhook-secret" \
    --data-file=<(printf "%s" "your-github-webhook-secret-token") \
    --project="${PROJECT_ID}"

# 3. ServiceNow Password (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-servicenow-password" \
    --data-file=<(printf "%s" "your-servicenow-password") \
    --project="${PROJECT_ID}"

# 4. ServiceNow Webhook Token (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-servicenow-webhook-token" \
    --data-file=<(printf "%s" "your-servicenow-token") \
    --project="${PROJECT_ID}"
```

### Option 2: Replicate ServiceNow Secrets from Existing Deployments
If replicating from an existing `incident-sre-agent` deployment in the same project, use `scripts/copy-secrets.sh`:
```bash
./scripts/copy-secrets.sh --project-id "${PROJECT_ID}" --source-prefix isa --target-prefix "${NAME_PREFIX}"
```

---

## 5. Stage C: Build & Push Container Images

Build and publish container images for both the Agent and Bridge to Artifact Registry:

```bash
# 1. Create Artifact Registry repositories if they do not already exist:
gcloud artifacts repositories create "${NAME_PREFIX}-agent" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" || true

gcloud artifacts repositories create "${NAME_PREFIX}-bridge" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" || true

# 2. Build and push Agent image:
docker build -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-agent/${NAME_PREFIX}-agent:latest" \
    -f agent/Dockerfile .
docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-agent/${NAME_PREFIX}-agent:latest"

# 3. Build and push Bridge image:
docker build -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-bridge/${NAME_PREFIX}-bridge:latest" \
    -f bridge/Dockerfile .
docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-bridge/${NAME_PREFIX}-bridge:latest"
```

---

## 6. Stage D: Deploy Cloud Run Services via Terraform

Now that container images are pushed and Secret Manager secrets have active versions, apply the complete Terraform stacks to deploy the Cloud Run services.

### 1. Deploy Agent Cloud Run Service (Internal Ingress)
```bash
cd agent/infra/terraform

terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -var="github_app_id=123456"

# Capture Terraform outputs:
AGENT_URL=$(terraform output -raw agent_url)
AGENT_SA=$(terraform output -raw agent_service_account_email)
TASKS_INVOKER_SA=$(terraform output -raw tasks_invoker_service_account_email)

cd ../../..
```

### 2. Deploy Bridge Cloud Run Service (Public Ingress)
```bash
cd bridge/infra/terraform

terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -var="agent_service_url=${AGENT_URL}" \
    -var="agent_service_account_email=${AGENT_SA}" \
    -var="tasks_invoker_service_account_email=${TASKS_INVOKER_SA}" \
    -var="servicenow_instance_url=https://your-instance.service-now.com" \
    -var="servicenow_username=gdi_integration_user"

# Capture Bridge public endpoint URL:
BRIDGE_URL=$(terraform output -raw bridge_url)

cd ../../..
```

---

## 7. Stage E: Wire GitHub Webhook & App

1. Open your GitHub App (e.g., owned by `ssvaidyanathan`) or target repository settings.
2. In the **Webhook URL** field, enter:
   ```
   https://${BRIDGE_URL}/webhook/github
   ```
3. Ensure the Secret matches the one set in `${NAME_PREFIX}-github-webhook-secret`.
4. Ensure event subscriptions include `workflow_job`, `workflow_run`, and `check_run`.

---

## 8. Stage F: Post-Deployment Verification

Verify both services are healthy:

```bash
# Verify Bridge health endpoint
curl -s -f "${BRIDGE_URL}/health" | jq .

# Send a test ping to verify HMAC validation
curl -i -X POST "${BRIDGE_URL}/webhook/github" \
    -H "Content-Type: application/json" \
    -H "X-GitHub-Event: ping" \
    -d '{"zen": "Practicality beats purity."}'
# Expect HTTP 401 Unauthorized (since X-Hub-Signature-256 header was omitted)
```
