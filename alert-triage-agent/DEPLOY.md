# Alert Triage Agent: Production Deployment Guide

This guide details the complete, step-by-step deployment procedure for the **Alert Triage Agent (ATA)** system in Google Cloud Platform. It covers Terraform modules, Cloud SQL with IAM database authentication, Apigee X API proxies, Cloud Run microservices, Cloud Tasks single-dispatch queue, Secret Manager credentials, and end-to-end verification.

---

## 1. System Architecture & Prerequisites

### Architecture Overview

```
                                      +------------------------+
                                      |   Client / Consumer    |
                                      +-----------+------------+
                                                  | HTTPS
                                                  v
                                      +------------------------+
                                      |  Apigee X Ingress      |
                                      |  8.233.247.52.nip.io   |
                                      |  - ata-orders-v1       |
                                      |  - ata-inventory-v1    |
                                      +-----------+------------+
                                                  | Google ID Token
                     +----------------------------+----------------------------+
                     |                                                         |
                     v                                                         v
        +--------------------------+                              +--------------------------+
        | Cloud Run: ata-orders-api|                              |Cloud Run: ata-inventory-api
        +------------+-------------+                              +------------+-------------+
                     | Google ID Token                                         | IAM DB Auth (pg8000)
                     +-------------------------------------------------------->+
                                                                               |
                                                                               v
                                                                  +--------------------------+
                                                                  | Cloud SQL PostgreSQL     |
                                                                  | ata-inventory-db         |
                                                                  +--------------------------+
```

```
                                  TRIAGE ENGINE TOPOLOGY (THIN BRIDGE)
                                  ====================================

     [Cloud Monitoring Alerts]
                 | Webhook (HTTP Basic Auth)
                 v
     +-----------------------+           +-----------------------+
     | Cloud Run: ata-bridge | --------> | Cloud Tasks Queue     |
     +-----------+-----------+           | ata-triage-queue      |
                 ^   |                   | (max_concurrent: 1)   |
                 |   |                   +-----------+-----------+
                 |   | Instructions                  |
                 |   | (POST /instructions/ticket/*) v
                 |   |                   +-----------------------+
                 |   +------------------ | Cloud Run: ata-agent  | <---+
                 |                       | (Atomic OKF Store)    |     |
                 |                       +-----------+-----------+     |
                 v                                   |                 |
     +-----------------------+                       | Timers          | Wake
     | External ServiceNow   |                       v                 |
     | REST API Table & Att. |           +-----------------------+     |
     +-----------------------+           | Cloud Tasks Queue     | ----+
                                         | ata-timer-queue       |
                                         | (delayed settle/cool) |
                                         +-----------------------+
```

### Prerequisites

Ensure the following tools and permissions are configured on your operator workstation:

1. **Google Cloud SDK (`gcloud` CLI)** version 480.0.0 or later:
   ```bash
   gcloud version
   gcloud config set project cloud-assist-fde-2
   ```
2. **Terraform CLI** version 1.5.0 or later:
   ```bash
   terraform version
   ```
3. **Python** version 3.11, 3.12, or 3.13 with `pip` and virtual environment support.
4. **Active IAM Role**: `roles/owner` or `roles/editor` with `roles/resourcemanager.projectIamAdmin` on `cloud-assist-fde-2`.
5. **Apigee X Setup**: Existing Apigee organization `cloud-assist-fde-2` with environment `prod` bound to envgroup hostname `8.233.247.52.nip.io`.

> [!IMPORTANT]
> **Resource Labels & Metadata Policy:**
> All newly provisioned or deployed resources in `cloud-assist-fde-2` must carry standard labels (`app=alert-triage-agent`, `component`, `role`, `tier`, `env`, `managed-by`, `owner`). For resources that do not natively support GCP labels (such as Apigee API proxies and Cloud Tasks queues), metadata must be recorded in proxy bundle `<Description>` tags. Refer to [`CONFIGURATION.md`](CONFIGURATION.md#7-labels--metadata) for the complete label specification, allowed values, and agent ingestion rules.

---

## 2. Step 0: Terraform State Bootstrap

Bootstrap the remote Google Cloud Storage backend for Terraform state storage and distributed locking:

```bash
cd alert-triage-agent
./scripts/bootstrap-tfstate.sh
```

This ensures `gs://cloud-assist-fde-2-ata-tfstate/` exists with uniform bucket-level access and versioning enabled.

---

## 3. Step 1: Foundation Module

The foundation module enables required Google Cloud APIs, provisions the primary GCS incidents bucket, creates 6 dedicated service accounts with least-privilege IAM roles, configures Secret Manager secret holders, and deploys Cloud Tasks queues for alert triage and delayed timer evaluation.

```bash
cd alert-triage-agent/infra/terraform/foundation
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

### Resources Created
- **GCS Bucket:** `gs://cloud-assist-fde-2-ata-incidents` (Object versioning enabled, mounted at `/knowledge/incident-manager` using `mount-options=only-dir=knowledge/incident-manager`). Active OKF files reside exclusively under `gs://cloud-assist-fde-2-ata-incidents/knowledge/incident-manager/`, while reset/historical files are moved to `gs://cloud-assist-fde-2-ata-incidents/archive/`.
- **Cloud Tasks Queues:**
  - `ata-triage-queue`: Serialized alert dispatch queue (`max_concurrent_dispatches: 1`, `max_dispatches_per_second: 10`). Failed dispatches are retried for up to 2 hours (`max_retry_duration: 7200s`, backoff `1s` to `300s`). For details, see [CONFIGURATION.md §4](CONFIGURATION.md#4-cloud-tasks-queue-configuration).
  - `ata-timer-queue`: Dedicated delayed timer queue for settle, max-wait, and cool-down deadlines (`max_concurrent_dispatches: 50`, `max_dispatches_per_second: 50`). No `retry_config` is set, so the GCP default retry policy applies.
- **Service Accounts:**
  - `ata-orders-sa`: Identity for `ata-orders-api`
  - `ata-inventory-sa`: Identity for `ata-inventory-api` (Cloud SQL Client & IAM user)
  - `ata-apigee-sa`: Attached to Apigee proxies in `prod`
  - `ata-bridge-sa`: Identity for `ata-bridge`
  - `ata-agent-sa`: Identity for `ata-agent` ADK runtime (granted `roles/cloudtasks.enqueuer` and `roles/iam.serviceAccountUser` on `ata-timer-queue`, and `roles/run.invoker` on `ata-agent` and `ata-bridge`)
  - `ata-tools-sa`: Identity for `ata-tools` FastMCP server
- **Secret Manager Resources:** `ata-webhook-token`, `ata-servicenow-username`, `ata-servicenow-password`. (`servicenow_instance_url` is a Terraform configuration variable, not a secret).

---

## 4. Step 2: Database Module

Provisions a Cloud SQL PostgreSQL 15 instance with IAM database authentication enabled (no static database passwords):

```bash
cd alert-triage-agent/infra/terraform/database
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

### Database Initialization
Apply the schema and seed mock inventory catalog items:
```bash
# Connect using Cloud SQL Auth Proxy or gcloud sql connect
gcloud sql connect ata-inventory-db --user=postgres --database=inventory < demo/db/schema.sql
```

---

## 5. Step 3: Applications Module

Deploys internal demo microservices on Cloud Run with IAM service account identities:

```bash
cd alert-triage-agent/infra/terraform/applications
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

### Services Deployed
- `ata-inventory-api`: Backed by Cloud SQL IAM authentication and in-memory mock fallback.
- `ata-orders-api`: Securely invokes `ata-inventory-api` using ambient Google ID tokens.
- Apigee TargetServers: `ata-orders-target` and `ata-inventory-target`.

---

## 6. Step 4: Apigee API Proxies & Products

Package and deploy the Apigee API proxies to environment `prod`, configure developer credentials, and obtain an API consumer key:

```bash
cd alert-triage-agent
./scripts/deploy-apigee-proxies.sh
```

### Deployment Details
- **Basepaths:**
  - Orders: `/ata/orders/v1` -> `https://8.233.247.52.nip.io/ata/orders/v1`
  - Inventory: `/ata/inventory/v1` -> `https://8.233.247.52.nip.io/ata/inventory/v1`
- **Security Policies:**
  - `VA-VerifyKey-Header` & `VA-VerifyKey-Query`: Enforces valid API key for consumer requests.
  - `SA-SpikeArrest`: Rate limiting set to 60 requests per minute (`60pm`).
  - `ML-CloudLogging`: Structured request/response logging directly to Google Cloud Logging.
- **Output:** The generated API key is stored in `.ata-api-key`.

---

## 7. Step 5: Observability Module

Deploys Cloud Monitoring alert policies and links them to the `ata-bridge` notification channel:

```bash
cd alert-triage-agent/infra/terraform/observability
terraform init -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
```

### Alert Policies Configured
1. `ata-orders-5xx`: Cloud Run `ata-orders-api` 5xx rate > 5% over 60s.
2. `ata-orders-latency`: Cloud Run `ata-orders-api` latency > 2000ms over 60s.
3. `ata-inventory-5xx`: Cloud Run `ata-inventory-api` 5xx rate > 5% over 60s.
4. `ata-inventory-latency`: Cloud Run `ata-inventory-api` latency > 2000ms over 60s.
5. `ata-apigee-orders-5xx`: Apigee proxy `ata-orders-v1` HTTP 500 error count > 1.
6. `ata-apigee-orders-429`: Apigee proxy `ata-orders-v1` HTTP 429 SpikeArrest rate count > 1.
7. `ata-cloudsql-connections-critical`: Cloud SQL `ata-inventory-db` connection count > 20 backends (~80% of db-f1-micro max_connections 25).
8. `ata-cloudsql-cpu-high`: Cloud SQL `ata-inventory-db` CPU utilization > 85%.

---

## 8. Step 6: Triage Tier Services Deployment

Deploy the custom FastMCP tools server, the multi-agent ADK runtime, and the bridge webhook service:

```bash
# 1. Deploy ata-tools FastMCP Server
cd alert-triage-agent/tools
./scripts/deploy.sh

# 2. Deploy ata-agent ADK Runtime
cd alert-triage-agent/agent
./scripts/deploy.sh

# 3. Deploy ata-bridge Webhook Dispatcher
cd alert-triage-agent/bridge
./scripts/deploy.sh
```

> [!TIP]
> **Deployment Parameters:**
> All Cloud Run deployment and bootstrap scripts support configuring target environment and ownership labels via environment variables:
> - `ENVIRONMENT`: Deployment environment label (`demo`, `staging`, `prod`; default: `demo`).
> - `OWNER`: Owning engineering team or contact (`sre-prototypes`, `devops`; default: `sre-prototypes`).
> - `NAME_PREFIX`: Resource name prefix (default: `ata`).
> - `VERIFIER_SA`: Optional service account email authorized to invoke the read-only ticket verification endpoint.
>
> Example: `ENVIRONMENT=staging OWNER=infra-team ./scripts/deploy.sh`

### Read-Only Ticket Verification Endpoint

The bridge exposes a read-only endpoint for verifying incident tickets without distributing or handling ServiceNow credentials:
```bash
# Verify ticket status via the bridge (requires Google OIDC Bearer token from ata-agent-sa or VERIFIER_SA)
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
     "https://${NAME_PREFIX}-bridge-4klvlqnmpa-uw.a.run.app/instructions/ticket/view/${GROUP_ID_OR_SYS_ID}"
```

**Returned Fields (Credentials NEVER Returned):**
- `number`: ServiceNow incident number (e.g. `INC0010482`)
- `state`: Incident lifecycle state (`1` New, `2` In Progress, `6` Resolved, `7` Closed)
- `short_description`: Triage summary
- `caller`: Caller identity / display name
- `work_notes`: Work note journal entries with timestamps
- `attachments`: File names and sizes in bytes

> [!IMPORTANT]
> **Verification Credential Rule:** Ticket verification **never** uses ServiceNow credentials directly. Automated verifiers and auditors authenticate via Google-signed OIDC ID tokens. The bridge serves as a secure boundary, ensuring ServiceNow credentials remain isolated to the bridge runtime.

---

## 9. Step 7: Secret Manager Configuration

Store ServiceNow credentials safely into Google Secret Manager without echoing passwords:

```bash
cd alert-triage-agent
./scripts/set-secrets.sh
```

When prompted:
- **ServiceNow Username:** Enter API integration user (`servicenow-username`).
- **ServiceNow Password:** Securely input password (`servicenow-password`).

> [!NOTE]
> The ServiceNow instance URL is configured as the Terraform variable `servicenow_instance_url` in `terraform.tfvars` (or environment variable `SERVICENOW_INSTANCE_URL`), not stored as a secret in Secret Manager. Webhook authentication credentials (`ata-webhook-token`) are automatically generated and managed in Secret Manager by Terraform.

---

## 10. Step 8: End-to-End Verification

Verify the entire deployed pipeline using the automated validation test suite:

```bash
# 1. Verify demo microservices and Apigee routing
./scripts/verify-demo-infra.sh

# 2. Verify bridge webhook ingestion and GCS OKF generation
./scripts/verify-stage1.sh

# 3. Run full end-to-end scenario verification harness
./scripts/verify-scenarios.sh
```

All 10 automated test suites should report green passing status.
