# Deployment Guide: GitHub Actions Deployment Failure Investigator

This guide details the complete, step-by-step process for provisioning, configuring, and deploying the **GitHub Actions Deployment Failure Investigator (`gha-deploy-investigator`)** onto Google Cloud Platform using Terraform and Cloud Run.

---

## 1. Prerequisites & Required Deployer IAM Roles

To allow a newcomer or automation pipeline to deploy `gha-deploy-investigator` end-to-end, the deploying identity (user or CI/CD deployment service account) requires the following **least-privilege IAM roles** derived directly from the Terraform manifests. **Do not rely on `roles/editor`**, as it lacks IAM administration, Workload Identity Pool administration, and Cloud Build permissions.

### Deployer IAM Roles on Target Project
- **`roles/iam.workloadIdentityPoolAdmin`**: Creates and manages Workload Identity Pools and Providers (`google_iam_workload_identity_pool`, `google_iam_workload_identity_pool_provider`).
- **`roles/artifactregistry.admin`**: Creates Artifact Registry Docker repositories and manages repository IAM policies (`google_artifact_registry_repository`, `google_artifact_registry_repository_iam_member`).
- **`roles/run.admin`**: Deploys and manages Cloud Run v2 services and ingress policies (`google_cloud_run_v2_service`, `google_cloud_run_v2_service_iam_member`).
- **`roles/iam.serviceAccountAdmin`**: Creates application runtime service accounts (`google_service_account`).
- **`roles/iam.serviceAccountUser`**: Allows deploying Cloud Run services using the created runtime service accounts.
- **`roles/secretmanager.admin`**: Provisions Secret Manager secret containers and grants secret accessor permissions (`google_secret_manager_secret`, `google_secret_manager_secret_iam_member`).
- **`roles/cloudtasks.admin`**: Provisions Cloud Tasks queues and grants task enqueuer permissions (`google_cloud_tasks_queue`, `google_cloud_tasks_queue_iam_member`).
- **`roles/resourcemanager.projectIamAdmin`**: Binds project-level IAM roles (`roles/aiplatform.user`, `roles/logging.viewer`, `roles/run.viewer`) to service accounts.
- **`roles/cloudbuild.builds.editor`**: Submits container image builds via Google Cloud Build.
- **`roles/serviceusage.serviceUsageAdmin`**: Enables required Google Cloud APIs if not already activated.
- **`roles/storage.admin`**: Creates and configures the GCS remote state bucket (`scripts/bootstrap-tfstate.sh`).

> [!NOTE]
> **Least Privilege vs. Project Owner**:
> In enterprise environments where `roles/owner` cannot be granted to CI/CD pipelines or development personnel, the 11 granular roles listed above provide the exact least-privilege boundary required to provision the infrastructure, deploy containers, configure WIF, and bind runtime IAM policies without granting arbitrary project mutation or billing administration authority.

#### One-Shot Provisioning Script for Deployer Roles
```bash
# Grant least-privilege operator roles to the deploying identity (User or Deployment SA)
OPERATOR_PRINCIPAL="user:you@example.com" # or "serviceAccount:deployer@${PROJECT_ID}.iam.gserviceaccount.com"

OPERATOR_ROLES=(
    "roles/serviceusage.serviceUsageAdmin"
    "roles/resourcemanager.projectIamAdmin"
    "roles/iam.serviceAccountAdmin"
    "roles/iam.serviceAccountUser"
    "roles/iam.workloadIdentityPoolAdmin"
    "roles/artifactregistry.admin"
    "roles/secretmanager.admin"
    "roles/cloudtasks.admin"
    "roles/run.admin"
    "roles/cloudbuild.builds.editor"
    "roles/storage.admin"
)

for role in "${OPERATOR_ROLES[@]}"; do
    gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
        --member="${OPERATOR_PRINCIPAL}" \
        --role="${role}" \
        --condition=None
done
```

> [!NOTE]
> **Post-Deployment Least-Privilege Role Revocation**:
> Privileged bootstrapping roles granted to the deploying principal specifically for initial infrastructure provisioning (such as `roles/iam.workloadIdentityPoolAdmin` and `roles/artifactregistry.admin`) should be revoked after initial setup is complete:
> ```bash
> # Revoke temporary provisioning privileges from deployer SA
> gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
>     --member="serviceAccount:${DEPLOYER_SA_EMAIL}" \
>     --role="roles/iam.workloadIdentityPoolAdmin" \
>     --condition=None
> 
> gcloud projects remove-iam-policy-binding "${PROJECT_ID}" \
>     --member="serviceAccount:${DEPLOYER_SA_EMAIL}" \
>     --role="roles/artifactregistry.admin" \
>     --condition=None
> ```

> [!NOTE]
> For enterprise organization policy constraints (domain-restricted sharing, Cloud Run ingress, Workload Identity restrictions), see [README Section 12: Enterprise Organization Policy Considerations](../README.md#12-enterprise-organization-policy-considerations).

### Required Google Cloud APIs
Ensure the following APIs are enabled in your target GCP project:
```bash
gcloud services enable \
    run.googleapis.com \
    secretmanager.googleapis.com \
    cloudtasks.googleapis.com \
    artifactregistry.googleapis.com \
    iam.googleapis.com \
    iamcredentials.googleapis.com \
    sts.googleapis.com \
    aiplatform.googleapis.com \
    logging.googleapis.com \
    cloudresourcemanager.googleapis.com \
    cloudbuild.googleapis.com \
    --project="${PROJECT_ID}"
```

*(Note: `iamcredentials.googleapis.com` and `sts.googleapis.com` are strictly required for GitHub Actions Workload Identity Federation OIDC token exchange).*

### Tools & Environment Variables
- **Google Cloud SDK (`gcloud`)** `>= 480.0.0`
- **Terraform (`>= 1.5.0`)**
- **Docker** or **Cloud Build**
- **GitHub CLI (`gh`)**

```bash
export PROJECT_ID="your-target-project-id"
export PROJECT_NUMBER="123456789012" # Obtain via: gcloud projects describe ${PROJECT_ID} --format="value(projectNumber)"
export REGION="us-central1"
export NAME_PREFIX="gdi"
export GITHUB_OWNER="your-github-org-or-user"
export DEMO_REPO_NAME="gdi-sample-pipeline-app"
```

---

## 2. Runtime Service Accounts & Architectural Roles

Terraform creates five dedicated least-privilege service accounts:

| Service Account ID | Display Name / Component | Roles & Permissions |
| :--- | :--- | :--- |
| `${NAME_PREFIX}-agent-sa` | GDI Agent Runtime SA | `roles/aiplatform.user` (Gemini model invocation)<br>`roles/logging.viewer` (Workflow log reading)<br>`roles/run.viewer` (Cloud Run inspection)<br>`roles/secretmanager.secretAccessor` on `${NAME_PREFIX}-github-app-private-key` |
| `${NAME_PREFIX}-bridge-sa` | GDI Bridge Runtime SA | `roles/cloudtasks.enqueuer` on `${NAME_PREFIX}-tasks-queue`<br>`roles/iam.serviceAccountUser` on `${NAME_PREFIX}-tasks-invoker-sa`<br>`roles/secretmanager.secretAccessor` on all 4 bridge secrets and `${NAME_PREFIX}-github-app-private-key` (write-back) |
| `${NAME_PREFIX}-tasks-invoker-sa` | Cloud Tasks OIDC Invoker SA | `roles/run.invoker` on `${NAME_PREFIX}-agent` internal Cloud Run service |
| `${NAME_PREFIX}-demo-deployer-sa` | Demo Pipeline CI/CD SA | `roles/run.admin` on project<br>`roles/artifactregistry.writer` on demo Artifact Registry<br>`roles/iam.serviceAccountUser` on `${NAME_PREFIX}-demo-app-sa` (and on itself)<br>`roles/iam.workloadIdentityUser` bound to GitHub OIDC subject |
| `${NAME_PREFIX}-demo-app-sa` | Demo App Cloud Run Runtime SA | Minimal runtime execution identity for Cloud Run demo service |

---

## 3. Deployment Sequencing & Secret Management Invariant

> [!IMPORTANT]
> **Cloud Run Secret Dependency Invariant**: In Google Cloud Platform, a Cloud Run service mounting a Secret Manager secret via `secret_key_ref` (referencing `version = "latest"`) **fails to deploy** if the secret container exists but has no enabled versions.
> 
> To maintain the **Zero Secret Values in Git/Terraform State** invariant, Terraform manages only the Secret Manager resource definitions and IAM role bindings. It never manages versions or payload values.
> 
> Therefore, deployment must proceed in three strictly sequenced stages:
> 1. **Stage A**: Terraform provisions the 5 Secret Manager secret containers, IAM accessors, and Cloud Tasks queue.
> 2. **Stage B**: The administrator populates secret values/versions using `gcloud secrets versions add` or the GCP Console.
> 3. **Stage C**: Container images are published and Cloud Run services are deployed.

---

## 4. Stage A: Provision Secret Containers, SAs & Queues via Terraform

The standard deployment defines 5 Secret Manager secrets across the two stacks:
- **Agent Stack (1 secret)**: `${NAME_PREFIX}-github-app-private-key`
- **Bridge Stack (4 secrets)**:
  - `${NAME_PREFIX}-github-webhook-secret`
  - `${NAME_PREFIX}-servicenow-username`
  - `${NAME_PREFIX}-servicenow-password`
  - `${NAME_PREFIX}-servicenow-webhook-token`

Provision these secret containers and their IAM bindings before deploying Cloud Run:

```bash
cd gha-deploy-investigator

# 0. Bootstrap GCS remote state bucket and generate backend.hcl
./scripts/bootstrap-tfstate.sh --project-id "${PROJECT_ID}" --name-prefix "${NAME_PREFIX}" --region "${REGION}"

# 1. Initialize Terraform stacks
terraform -chdir=agent/infra/terraform init -backend-config=backend.hcl
terraform -chdir=bridge/infra/terraform init -backend-config=backend.hcl
```

> [!NOTE]
> **Migrating Existing Local Deployments**: If migrating from existing local `terraform.tfstate` files rather than initializing fresh stacks, run `terraform init -migrate-state -backend-config=backend.hcl` in each stack directory and verify with `terraform plan` (see [README.md Section 11](../README.md#migrating-existing-local-state)).

> [!WARNING]
> **Do not delete local state files prematurely**:
> Do not delete or remove local `terraform.tfstate` or `terraform.tfstate.backup` files until `terraform plan` reports `No changes` across all stacks and state integrity is verified. Keep local backups until the remote backend is fully verified in your team workflow.

```bash
# 2. Provision Agent stack secret container and service accounts
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

# 3. Provision Bridge stack secret containers, service account, and Cloud Tasks queue
terraform -chdir=bridge/infra/terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -target=google_secret_manager_secret.github_webhook_secret \
    -target=google_secret_manager_secret.servicenow_username \
    -target=google_secret_manager_secret.servicenow_password \
    -target=google_secret_manager_secret.servicenow_webhook_token \
    -target=google_service_account.bridge_sa \
    -target=google_cloud_tasks_queue.tasks_queue \
    -target=google_cloud_tasks_queue_iam_member.bridge_task_enqueuer \
    -target=google_secret_manager_secret_iam_member.bridge_gh_secret_access \
    -target=google_secret_manager_secret_iam_member.bridge_snow_username_access \
    -target=google_secret_manager_secret_iam_member.bridge_snow_pwd_access \
    -target=google_secret_manager_secret_iam_member.bridge_snow_token_access \
    -target=google_secret_manager_secret_iam_member.bridge_gh_key_access
```

---

## 5. Stage B: Populate Secret Values

Once the secret resources exist in Secret Manager, populate enabled versions for all 5 secrets. Secret values are never entered into Terraform or committed to source control.

```bash
# 1. GitHub App Private Key (Agent)
gcloud secrets versions add "${NAME_PREFIX}-github-app-private-key" \
    --data-file=/path/to/github-app-private-key.pem \
    --project="${PROJECT_ID}"

# 2. GitHub Webhook Secret (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-github-webhook-secret" \
    --data-file=<(printf "%s" "your-github-webhook-secret-token") \
    --project="${PROJECT_ID}"

# 3. ServiceNow Username (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-servicenow-username" \
    --data-file=<(printf "%s" "your-servicenow-username") \
    --project="${PROJECT_ID}"

# 4. ServiceNow Password (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-servicenow-password" \
    --data-file=<(printf "%s" "your-servicenow-password") \
    --project="${PROJECT_ID}"

# 5. ServiceNow Webhook Token (Bridge)
gcloud secrets versions add "${NAME_PREFIX}-servicenow-webhook-token" \
    --data-file=<(printf "%s" "your-servicenow-token") \
    --project="${PROJECT_ID}"
```

---

## 6. Stage C: Build & Publish Container Images

Create the Artifact Registry Docker repository and build container images using Cloud Build:

```bash
# 1. Create containers repository
gcloud artifacts repositories create "${NAME_PREFIX}-containers" \
    --repository-format=docker \
    --location="${REGION}" \
    --project="${PROJECT_ID}" || true

# 2. Build and publish gdi-agent
gcloud builds submit agent \
    --tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-containers/${NAME_PREFIX}-agent:latest" \
    --project="${PROJECT_ID}"

# 3. Build and publish gdi-bridge
gcloud builds submit bridge \
    --tag="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-containers/${NAME_PREFIX}-bridge:latest" \
    --project="${PROJECT_ID}"
```

---

## 7. Stage D: Deploy Cloud Run Services via Terraform

### 1. Deploy Agent Cloud Run Service (Internal Ingress)
```bash
cd agent/infra/terraform

terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -var="repository_id=${NAME_PREFIX}-containers" \
    -var="github_app_id=your-github-app-id"

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
    -var="repository_id=${NAME_PREFIX}-containers" \
    -var="agent_service_url=${AGENT_URL}" \
    -var="agent_service_account_email=${AGENT_SA}" \
    -var="tasks_invoker_service_account_email=${TASKS_INVOKER_SA}" \
    -var="github_app_id=your-github-app-id" \
    -var="servicenow_instance_url=https://your-instance.service-now.com"

BRIDGE_URL=$(terraform output -raw bridge_url)
cd ../../..
```

---

## 8. Stage E: Demo App Infrastructure & Workload Identity Federation (WIF)

To test the end-to-end pipeline against a dedicated customer-style repository without service account keys:

### 1. Provision Demo WIF Stack via Terraform
```bash
cd demo/infra/terraform
terraform init -backend-config=backend.hcl
terraform apply \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}"
```

The WIF stack provisions:
- Artifact Registry repo: `us-central1-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-demo-app`
- Deployer Service Account: `${NAME_PREFIX}-demo-deployer-sa@${PROJECT_ID}.iam.gserviceaccount.com`
- App Runtime Service Account: `${NAME_PREFIX}-demo-app-sa@${PROJECT_ID}.iam.gserviceaccount.com` (least-privilege runtime SA for Cloud Run)
- Workload Identity Pool: `${NAME_PREFIX}-demo-pool`
- Workload Identity Provider: `${NAME_PREFIX}-demo-gh-provider`
- **Attribute Condition**: Strictly restricted to:
  ```
  assertion.repository == '${GITHUB_OWNER}/${DEMO_REPO_NAME}' && assertion.ref == 'refs/heads/main'
  ```

> [!IMPORTANT]
> **Project Number Requirement**: Workload Identity Federation provider resource names require the **numeric Google Cloud Project Number** in their path (e.g. `projects/123456789012/locations/global/workloadIdentityPools/...`). Do not use the string project ID in the provider path.

### 2. Configure GitHub Repository Secrets
Because GitHub repository variables (`vars.*`) are not masked in public repository logs, configure the following **6 repository secrets** on your demo GitHub repository:

```bash
gh secret set GCP_PROJECT_ID --body "${PROJECT_ID}" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
gh secret set GCP_WIF_PROVIDER --body "$(terraform output -raw wif_provider_name)" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
gh secret set GCP_SA_EMAIL --body "$(terraform output -raw service_account_email)" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
gh secret set GCP_APP_SA_EMAIL --body "$(terraform output -raw app_service_account_email)" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
gh secret set GAR_REPOSITORY --body "$(terraform output -raw artifact_registry_repo)" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
gh secret set GAR_LOCATION --body "${REGION}-docker.pkg.dev" --repo "${GITHUB_OWNER}/${DEMO_REPO_NAME}"
```

### 3. Pipeline Event Gating
Notice that `.github/workflows/pipeline.yml` in the demo application automatically gates Google Cloud authentication, image push, and Cloud Run deployment with:
```yaml
if: github.event_name == 'push' && github.ref == 'refs/heads/main'
```
This ensures pull requests and branch builds run linting, unit testing, and local container builds without failing on WIF authentication.

---

## 9. Stage F: Wire GitHub App & ServiceNow Webhook Exclusion

### 1. GitHub App Webhook Configuration
1. Open your GitHub App settings (e.g. via GitHub Developer Settings).
2. Set **Webhook URL** to:
   ```
   https://${BRIDGE_URL}/webhook/github
   ```
3. Set **Webhook Secret** to the secret string populated into `${NAME_PREFIX}-github-webhook-secret`.
4. Permissions required:
   - **Repository Permissions**:
     - Checks: Read & write
     - Pull requests: Read & write
     - Actions: Read-only
5. Event Subscriptions:
   - `workflow_run`
   - `workflow_job`
   - `check_run`
6. Install the App on `${GITHUB_OWNER}/${DEMO_REPO_NAME}`.

### 2. ServiceNow Webhook Exclusion Filter
To prevent the prototype's created incidents from causing a feedback loop or triggering external SRE incident webhooks:
- Every incident created by `gha-deploy-investigator` contains:
  - `correlation_display = "gha-deploy-investigator"`
  - `correlation_id = "gdi:<owner>/<repo>:<run_id>:<run_attempt>"`
- In your ServiceNow Business Rule or outbound webhook filter condition, configure:
  ```
  Correlation display [IS NOT] gha-deploy-investigator
  AND
  Correlation ID [DOES NOT START WITH] gdi:
  ```

---

## 10. Post-Deployment Verification

Verify healthy endpoints and signature authentication:

```bash
# 1. Health check probe
curl -s -f "${BRIDGE_URL}/health" | jq .
# Returns: {"status":"healthy","service":"gdi-bridge"}

# 2. Unsigned request check (HMAC SHA-256 validation)
curl -i -X POST "${BRIDGE_URL}/webhook/github" \
    -H "Content-Type: application/json" \
    -H "X-GitHub-Event: ping" \
    -d '{"zen": "Practicality beats purity."}'
# Returns: HTTP/2 401 Unauthorized

# 3. Signed ping check
# Returns: HTTP/2 200 OK {"status":"pong","message":"GitHub webhook ping received successfully"}
```
