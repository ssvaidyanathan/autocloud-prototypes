# Incident Bridge Deployment Guide

This document describes how to deploy `incident-bridge` to Google Cloud Platform.

---

## 1. Prerequisites

- Google Cloud SDK (`gcloud`) installed and configured.
- Terraform >= 1.5.0.
- Target GCP project with billing enabled.
- SRE Agent already deployed in `agent/` (outputs needed for bridge tfvars).

---

## 2. Infrastructure Setup (Terraform)

All resources are prefixed with `name_prefix` (default `"isa"`, configurable e.g. `"acme"`) and tagged with common labels (`app`, `component`, `env`, `prefix`).

1. Navigate to `bridge/infra/terraform`:
   ```bash
   cd bridge/infra/terraform
   ```
2. Populate `terraform.tfvars` from `terraform.tfvars.example` and the outputs of the `agent/` terraform stack:
   ```hcl
   name_prefix                         = "isa"
   project_id                          = "your-project-id"
   region                              = "us-central1"
   tasks_queue_name                    = "isa-investigations"
   tasks_invoker_service_account_email = "isa-tasks-invoker-sa@your-project-id.iam.gserviceaccount.com"
   agent_service_url                   = "https://isa-sre-agent-xyz-uc.a.run.app"
   agent_service_account_email         = "isa-agent-sa@your-project-id.iam.gserviceaccount.com"
   ```
3. Initialize and apply base infrastructure (APIs, IAM, Artifact Registry, Secret containers):
   ```bash
   terraform init
   terraform apply -target=module.apis -target=module.iam -target=module.artifact_registry -target=module.secrets
   ```

---

## 3. Populate Secrets

Run `bridge/scripts/set-secrets.sh` to populate Secret Manager versions:
```bash
export PROJECT_ID="your-project-id"
export SERVICENOW_USERNAME="svc_sre_agent"
export SERVICENOW_PASSWORD="your-servicenow-password"
export SERVICENOW_WEBHOOK_TOKEN="your-secure-token"
export JIRA_EMAIL="sre-agent@example.com"
export JIRA_API_TOKEN="your-jira-api-token"
export JIRA_WEBHOOK_TOKEN="your-jira-webhook-token"

bash bridge/scripts/set-secrets.sh
```

---

## 4. Build and Push Container Image

```bash
bash bridge/scripts/build-push.sh
```

---

## 5. Deploy Cloud Run Service

Apply the full Terraform stack to deploy the Cloud Run service:
```bash
terraform apply
```

Note the output `bridge_service_url`. Use this URL to configure webhook endpoints in ServiceNow and Jira according to:
- `bridge/integrations/servicenow/SETUP.md`
- `bridge/integrations/jira/SETUP.md`
