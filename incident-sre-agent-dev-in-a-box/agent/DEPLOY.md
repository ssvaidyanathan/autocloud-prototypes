# Deploying `sre-agent` to Google Cloud Run

This guide outlines the complete procedure for building, provisioning, and deploying the `sre-agent` service to Google Cloud.

---

## Prerequisites

1. **Google Cloud SDK (`gcloud`)**: Authenticated with appropriate permissions in target project.
2. **Terraform (`>= 1.5.0`)**: For provisioning cloud infrastructure and IAM.
3. **Docker or Google Cloud Build**: To build the container image.
4. **Target GCP Project**: With billing enabled.

---

## Deployment Steps

### 1. Provision Infrastructure with Terraform

The infrastructure lives in `agent/infra/terraform/` and is self-contained. All resources are prefixed with `name_prefix` (default `"isa"`, configurable e.g. `"acme"`) and tagged with common labels (`app`, `component`, `env`, `prefix`).

```bash
cd agent/infra/terraform

# Copy and update variables (set name_prefix and project_id)
cp terraform.tfvars.example terraform.tfvars

# Initialize Terraform
terraform init

# Apply base resources (APIs, IAM, Artifact Registry, Cloud Tasks)
terraform apply -target=module.apis -target=module.iam -target=module.artifact_registry -target=module.tasks_queue
```

### 2. Build and Push Container Image

Using the build-push helper script:

```bash
cd agent
./scripts/build-push.sh
```

Alternatively, submit directly via Cloud Build:

```bash
gcloud builds submit . \
    --project="your-project-id" \
    --config="cloudbuild.yaml" \
    --substitutions="_REGION=us-central1,_TAG_NAME=latest"
```

### 3. Deploy Cloud Run Service

Apply the full Terraform stack including the Cloud Run service:

```bash
cd agent/infra/terraform
terraform apply
```

Or deploy directly via `scripts/deploy.sh`:

```bash
cd agent
./scripts/deploy.sh
```

### 4. Cloud Run Execution Mode & CPU Allocation

- **Background Tasks & CPU Allocation**:
  By default, Cloud Run throttles CPU to near-zero when not actively handling an HTTP request. If `POST /investigate` is invoked asynchronously (returning `202 Accepted` immediately), you must configure Cloud Run with CPU always allocated (`--no-cpu-throttling`) to ensure background investigation tasks continue executing without being starved of CPU or suspended.
- **Recommended Cloud Tasks Configuration**:
  Alternatively, configure the Cloud Tasks queue to invoke `POST /investigate?sync=true`. Because Cloud Tasks supports deadlines up to 15 minutes (and `INVESTIGATION_TIMEOUT_SECONDS` is 10 minutes), running synchronously keeps the HTTP connection open throughout the investigation, eliminating CPU throttling concerns while delivering full execution guarantees.

---

## IAM & Security Architecture

1. **Service Identity (`agent-sa`)**:
   - The service runs under `agent-sa` with strictly read-only permissions:
     - `roles/logging.viewer`
     - `roles/monitoring.viewer`
     - `roles/run.viewer`
     - `roles/compute.viewer`
     - `roles/container.viewer`
     - `roles/cloudsql.viewer`
     - `roles/cloudbuild.builds.viewer`
     - `roles/aiplatform.user`
2. **Ingress & Authentication**:
   - Cloud Run ingress is configured to `internal` (or `all` if invoked across VPC boundaries).
   - Invocations require authentication (`roles/run.invoker` granted exclusively to `tasks-invoker-sa`).
3. **Outbound OIDC Callback**:
   - When posting results back to `incident-bridge`, the agent requests a Google ID token from the metadata server with audience set to the bridge callback URL.
