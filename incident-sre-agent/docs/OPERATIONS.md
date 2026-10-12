# Incident SRE Agent — Day-2 Operations & Runbook Guide

This guide provides operational procedures, observability guidelines, alerting rules, troubleshooting runbooks, skill authoring instructions, and lifecycle management workflows for maintaining the Incident SRE Agent in production.

---

## 1. Observability & Monitoring

The system produces structured telemetry across Google Cloud Monitoring and Google Cloud Logging.

### 1.1 Key Metrics & Dashboards

#### Cloud Run Metrics (`incident-bridge` and `sre-agent`)
Monitor these metrics under **Google Cloud Monitoring > Metrics Explorer**:
- **Request Count by Response Code**:
  - Metric: `run.googleapis.com/request_count`
  - Group by: `response_code_class`, `service_name`
  - Baseline: Bridge 2xx/202s dominant; Agent 200s dominant.
- **Request Latency Percentiles**:
  - Metric: `run.googleapis.com/request_latencies`
  - Group by: `service_name` (Aligner: 50th, 95th, 99th percentiles)
  - Baseline: Bridge p95 < 800ms; Agent p95 ~ 180s–450s (due to multi-agent LLM analysis).
- **Container Utilization**:
  - Metrics: `run.googleapis.com/container/cpu/utilizations`, `run.googleapis.com/container/memory/utilizations`
  - Baseline: Normal spikes during investigation rounds; should never saturate memory limits (2 GiB).
- **Active Instance Count**:
  - Metric: `run.googleapis.com/container/instance_count`
  - Group by: `service_name`, `state` (active, idle)

#### Cloud Tasks Queue Metrics (`investigations`)
- **Queue Task Backlog Size**:
  - Metric: `cloudtasks.googleapis.com/queue/task_attempt_delays` and `cloudtasks.googleapis.com/queue/depth`
  - Baseline: Queue depth should rapidly drain towards 0.
- **Task Execution Rate & Retries**:
  - Metric: `cloudtasks.googleapis.com/queue/attempt_count`
  - Filter: `response_status != "OK"`

### 1.2 Cloud Logging Queries

To locate and trace investigations in **Logs Explorer**:

```text
# 1. Locate all completed SRE investigation reports
resource.type="cloud_run_revision"
jsonPayload.type="sre_report"

# 2. Trace an investigation across both services by incident ID
jsonPayload.incident_id="INC0019482" OR textPayload=~"INC0019482"

# 3. Find errors or tool timeouts in the SRE Agent
resource.type="cloud_run_revision"
resource.labels.service_name="sre-agent"
severity>=ERROR

# 4. Monitor webhook authentication failures on the Bridge
resource.type="cloud_run_revision"
resource.labels.service_name="incident-bridge"
httpRequest.status>=400
```

---

## 2. Recommended Alerting Policies

Configure the following alerting policies in Google Cloud Monitoring:

| Alert Name | Condition | Window | Severity | Action |
|---|---|---|---|---|
| **Bridge 5xx Error Spike** | `run.googleapis.com/request_count` (Bridge, code=5xx) > 1% of total | 5 min | P1 | Check Secret Manager access, downstream Cloud Tasks quota, and ticketing network connectivity. |
| **Tasks Queue Backlog** | `cloudtasks.googleapis.com/queue/depth` > 5 for > 15m | 15 min | P2 | Check `sre-agent` instance capacity, concurrency limits, or stuck investigations. |
| **SRE Agent Timeout Rate** | `run.googleapis.com/request_count` (Agent, code=504 or code=408) > 0 | 15 min | P2 | Review telemetry query time windows; check if large log volumes exceeded 15m deadline. |
| **Vertex AI 429 Exhausted** | `aiplatform.googleapis.com` quota error rate > 0 | 5 min | P2 | Request Vertex AI quota increase for Gemini models in the deployment region. |
| **Callback Delivery Failure** | `incident-bridge` `/callback/report` 5xx rate > 0 | 5 min | P1 | Verify OIDC audience, caller service account permissions, and ticketing API tokens. |

---

## 3. Troubleshooting Runbooks

### Runbook 1: Inbound Webhook Returns HTTP 401 Unauthorized / 403 Forbidden
- **Symptoms**: ServiceNow Flow Designer or Jira Automation reports 401 or 403 when posting to `/webhook/...`.
- **Diagnostic Steps**:
  1. Inspect Cloud Logging for `incident-bridge`:
     ```text
     resource.type="cloud_run_revision"
     resource.labels.service_name="incident-bridge"
     httpRequest.status=401
     ```
  2. Verify that secret versions are populated in Secret Manager:
     ```bash
     gcloud secrets versions list bridge-servicenow-webhook-token --project="${PROJECT_ID}"
     gcloud secrets versions list bridge-jira-webhook-token --project="${PROJECT_ID}"
     ```
  3. Verify the caller header:
     - ServiceNow: `Authorization: Bearer <token>` or valid Basic auth.
     - Jira: `X-Webhook-Token: <token>`.
     - Generic: `X-Hub-Signature-256: sha256=<hmac>` using `bridge-generic-hmac-key`.
- **Resolution**:
  - Re-run `bridge/scripts/set-secrets.sh` to update secret values, then restart Cloud Run revisions if secrets caching is enabled.

### Runbook 2: Tasks Stuck in Cloud Tasks Queue or Execution Retrying
- **Symptoms**: Tasks queue depth grows; investigations do not complete.
- **Diagnostic Steps**:
  1. Inspect Cloud Tasks queue status:
     ```bash
     gcloud tasks queues describe investigations --location="${REGION}" --project="${PROJECT_ID}"
     ```
  2. Check task attempt errors in Cloud Logging:
     ```text
     resource.type="cloud_tasks_queue"
     jsonPayload.taskStatus.lastAttemptStatus.responseStatus!="OK"
     ```
  3. Check IAM permissions: verify `tasks-invoker-sa` has `roles/run.invoker` on `sre-agent`:
     ```bash
     gcloud run services get-iam-policy sre-agent --region="${REGION}" --project="${PROJECT_ID}"
     ```
- **Resolution**:
  - If permissions are missing, re-apply agent Terraform: `cd agent/infra/terraform && terraform apply`.
  - If agent instances are maxed out, increase Cloud Run `max_instance_count`.

### Runbook 3: SRE Agent Reaches 15-Minute Timeout
- **Symptoms**: Cloud Run terminates agent execution at 900 seconds (`Container timed out`).
- **Diagnostic Steps**:
  1. Find the timed-out execution in Cloud Logging:
     ```text
     resource.type="cloud_run_revision"
     resource.labels.service_name="sre-agent"
     "The request has been terminated because it has reached the maximum request timeout"
     ```
  2. Check specialist logs: identify which specialist took the longest time (typically large log scans spanning 24 hours).
- **Resolution**:
  - Tighten query filters in `agent/app/specialists/logs.py`.
  - Ensure the incident opened timestamp is provided in the webhook payload; without an `opened_at`, the default fallback window may be overly broad.

### Runbook 4: Callback Delivery Returns 401 Unauthorized
- **Symptoms**: Agent completes investigation, but log displays `Callback failed with HTTP 401: Unauthorized`.
- **Diagnostic Steps**:
  1. Inspect bridge callback error logs:
     ```text
     resource.type="cloud_run_revision"
     resource.labels.service_name="incident-bridge"
     httpRequest.requestUrl=~"callback/report"
     ```
  2. Check token verification failure reason:
     - **Audience Mismatch**: Token was minted for `https://incident-bridge-xyz.a.run.app` but received at custom domain or IP.
     - **Caller Email Mismatch**: Calling identity was not `agent-sa@<project>.iam.gserviceaccount.com`.
- **Resolution**:
  - Verify that `agent_service_account_email` and `callback_url` variables in `bridge/infra/terraform` match the actual agent service account and bridge URL.

### Runbook 5: Vertex AI Returns 429 Quota Exceeded
- **Symptoms**: Agent fails during synthesis step with `429 ResourceExhausted: Quota exceeded for aiplatform.googleapis.com`.
- **Diagnostic Steps**:
  1. Check Vertex AI quota usage in Cloud Console under **IAM & Admin > Quotas**.
  2. Review current requests per minute (RPM) and tokens per minute (TPM) limits for `gemini-2.5-flash`.
- **Resolution**:
  - Switch model configuration to fallback or increase quota via GCP Console.
  - SRE Agent built-in heuristic fallback will output raw specialist findings even when synthesis is degraded.

---

## 4. Authoring and Extending SRE Runbook Skills

SRE skills empower the agent with service-specific domain knowledge, SLO definitions, diagnostic queries, and remediation runbooks.

### Step 1: Create the Skill Directory & Document
Create a new directory under `agent/skills/<service-name>/` with a `SKILL.md` file:

```markdown
---
name: service-payment-gateway
service: payment-gateway
version: "1.0.0"
description: Runbook and diagnostic recipes for the Payment Gateway service.
tags:
  - payments
  - checkout
  - pci
slos:
  availability: "99.99%"
  p95_latency: "250ms"
---

# Payment Gateway SRE Runbook

## 1. Service Topology & Dependencies
- Upstream: `shop-api`
- Downstream: External payment processor API (`https://api.processor.com`)
- Cloud Run Service: `payment-gateway`

## 2. Failure Modes & Signatures

| Failure Mode | Telemetry Signature | Probable Root Cause |
|---|---|---|
| Processor Timeout | Cloud Logging: "ConnectTimeout to processor" & 504 status | Downstream partner outage |
| Secret Rotation Failure | Cloud Logging: "401 Unauthorized from payment gateway API" | Stale payment secret |

## 3. Diagnostic Queries
```text
resource.type="cloud_run_revision"
resource.labels.service_name="payment-gateway"
severity>=ERROR
```

## 4. Recommended Remediation Playbooks
- Check external payment provider status page.
- Rotate credentials via Secret Manager: `gcloud secrets versions add payment-api-key --data-file=new_key.txt`.
```

### Step 2: Register the Skill in `agent/skills/skills.yaml`
Add the new skill entry to `agent/skills/skills.yaml`:

```yaml
skills:
  - id: payment-gateway
    name: service-payment-gateway
    path: service-payment-gateway/SKILL.md
    service: payment-gateway
```

### Step 3: Local Validation & Linting
Run the automated skill test suite to verify YAML frontmatter schema, Markdown syntax, and parser compatibility:

```bash
pytest agent/tests/test_skills_loader.py
```

---

## 5. Lifecycle Management

### Upgrading Service Versions
1. Update application code or dependencies.
2. Build and push new container image with a unique tag or git SHA:
   ```bash
   ./scripts/deploy-all.sh --project-id <PROJECT_ID> --image-tag "v1.2.0"
   ```
3. Cloud Run automatically performs zero-downtime blue/green traffic migration to the new revision.

### Rolling Back Deployments
To immediately revert to a known good Cloud Run revision:
```bash
# Rollback SRE Agent
gcloud run services update-traffic sre-agent \
  --to-revisions=sre-agent-00012-abc=100 \
  --region="${REGION}" --project="${PROJECT_ID}"

# Rollback Incident Bridge
gcloud run services update-traffic incident-bridge \
  --to-revisions=incident-bridge-00008-xyz=100 \
  --region="${REGION}" --project="${PROJECT_ID}"
```

### Secret Rotation Routine
When rotating ServiceNow or Jira API tokens:
1. Generate the new credentials in ServiceNow / Jira.
2. Execute `bridge/scripts/set-secrets.sh` to add the new secret version.
3. Deploy a new revision or restart the Cloud Run instances to flush in-memory cached credentials.

### Post-Deployment & Maintenance Verification
After applying service upgrades, rolling back revisions, or rotating secrets, run the automated smoke test suite to verify system integrity without affecting production telemetry:

```bash
./scripts/smoke-test.sh \
  --bridge-url "${BRIDGE_SERVICE_URL}" \
  --agent-url "${AGENT_SERVICE_URL}" \
  --generic-hmac-key "${GENERIC_HMAC_KEY}"
```

For complete negative security verification, multi-layer test architectures, and the test run results recording template, consult the [Testing Guide (`docs/TESTING.md`)](TESTING.md).

---

## 6. Infrastructure Teardown Procedures

To decommission provisioned infrastructure resources in a target Google Cloud project:

```bash
# Configure target environment variables
export PROJECT_ID="your-project-id"
export REGION="us-central1"
export NAME_PREFIX="your-prefix"
export TFSTATE_BUCKET="${PROJECT_ID}-${NAME_PREFIX}-tfstate"

# 1. Teardown Demo Stack (if deployed)
cd demo/infra/terraform
terraform init -backend-config="bucket=${TFSTATE_BUCKET}" -backend-config="prefix=${NAME_PREFIX}/demo"
terraform destroy -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}"

# 2. Teardown Incident Bridge Stack
cd ../../../bridge/infra/terraform
terraform init -backend-config="bucket=${TFSTATE_BUCKET}" -backend-config="prefix=${NAME_PREFIX}/bridge"
terraform destroy -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}"

# 3. Teardown SRE Agent Stack
cd ../../../agent/infra/terraform
terraform init -backend-config="bucket=${TFSTATE_BUCKET}" -backend-config="prefix=${NAME_PREFIX}/agent"
terraform destroy -auto-approve \
  -var="project_id=${PROJECT_ID}" \
  -var="region=${REGION}" \
  -var="name_prefix=${NAME_PREFIX}"

# 4. Remove Artifact Registry Repositories & Container Images
gcloud artifacts repositories delete "${NAME_PREFIX}-incident-demo" --location="${REGION}" --project="${PROJECT_ID}" --quiet || true
gcloud artifacts repositories delete "${NAME_PREFIX}-incident-bridge" --location="${REGION}" --project="${PROJECT_ID}" --quiet || true
gcloud artifacts repositories delete "${NAME_PREFIX}-sre-agent" --location="${REGION}" --project="${PROJECT_ID}" --quiet || true

# 5. Remove Terraform Remote State Bucket
gcloud storage rm --recursive "gs://${TFSTATE_BUCKET}/**" || true
gcloud storage rm "gs://${TFSTATE_BUCKET}" || true
```
