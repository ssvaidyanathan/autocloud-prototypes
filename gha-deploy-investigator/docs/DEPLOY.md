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

## 2. Step 1: Replicate and Provision Secrets

The prototype requires Secret Manager secrets for GitHub webhooks, GitHub App private keys, and ServiceNow credentials. Use `scripts/copy-secrets.sh` to copy values from existing prototype secrets without exposing values on stdout:

```bash
cd gha-deploy-investigator

# Copy existing ServiceNow secrets from incident-sre-agent (isa- prefix) to gdi- prefix:
./scripts/copy-secrets.sh --project-id "${PROJECT_ID}" --source-prefix isa --target-prefix "${NAME_PREFIX}"

# Add GitHub credentials to Secret Manager:
gcloud secrets versions add "${NAME_PREFIX}-github-webhook-secret" \
    --data-file=<(printf "%s" "your-github-webhook-secret-token") \
    --project="${PROJECT_ID}"

gcloud secrets versions add "${NAME_PREFIX}-github-app-private-key" \
    --data-file=/path/to/github-app-private-key.pem \
    --project="${PROJECT_ID}"
```

---

## 3. Step 2: Build & Push Container Images

Build and publish container images for both the Agent and Bridge:

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

## 4. Step 3: Deploy Agent Infrastructure (Internal Service)

Deploy the private Agent Cloud Run service:

```bash
cd agent/infra/terraform

terraform init

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

---

## 5. Step 4: Deploy Bridge Infrastructure (Public Ingress Service)

Deploy the Bridge Cloud Run service and Cloud Tasks queue, wiring them to the Agent outputs:

```bash
cd bridge/infra/terraform

terraform init

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

## 6. Step 5: Wire GitHub Webhook & App

1. Open your GitHub App (e.g., owned by `ssvaidyanathan`) or target repository settings.
2. In the **Webhook URL** field, enter:
   ```
   https://${BRIDGE_URL}/webhook/github
   ```
3. Ensure the Secret matches the one set in `${NAME_PREFIX}-github-webhook-secret`.
4. Ensure event subscriptions include `workflow_job`, `workflow_run`, and `check_run`.

---

## 7. Step 6: Post-Deployment Verification

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
