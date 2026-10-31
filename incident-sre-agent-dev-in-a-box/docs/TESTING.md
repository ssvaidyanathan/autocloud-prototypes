# Incident SRE Agent — Comprehensive Testing & Quality Assurance Guide

This document provides the definitive guide to testing the Incident SRE Agent platform across all stages of development and deployment: from fast, deterministic local unit tests to multi-container local integration, post-deployment smoke tests, negative security verification, and simulated production failure scenarios.

---

## Table of Contents

- [1. Testing Strategy & Principles](#1-testing-strategy--principles)
- [2. Test Layer Architecture (The 5 Test Layers)](#2-test-layer-architecture-the-5-test-layers)
  - [Layer 1: Unit & Component Tests (309+ pytest Tests)](#layer-1-unit--component-tests-309-pytest-tests)
  - [Layer 2: Local Multi-Container E2E Testing with Fake Ticket Server](#layer-2-local-multi-container-e2e-testing-with-fake-ticket-server)
  - [Layer 3: Per-Step Post-Deployment Verification Checks](#layer-3-per-step-post-deployment-verification-checks)
  - [Layer 4: Negative & Security Verification Checks](#layer-4-negative--security-verification-checks)
  - [Layer 5: End-to-End Scenario Testing with Results Template](#layer-5-end-to-end-scenario-testing-with-results-template)
- [3. LLM Stub vs Real Gemini Execution](#3-llm-stub-vs-real-gemini-execution)
  - [Dual Execution Mode Philosophy](#dual-execution-mode-philosophy)
  - [Stubbed / Heuristic Mode](#stubbed--heuristic-mode)
  - [Real Vertex AI Gemini Mode](#real-vertex-ai-gemini-mode)
  - [Switching Instructions (Local, Docker, Cloud)](#switching-instructions-local-docker-cloud)
  - [Automatic Fallback Resiliency](#automatic-fallback-resiliency)
- [4. Automated Post-Deployment Smoke Test Script (`scripts/smoke-test.sh`)](#4-automated-post-deployment-smoke-test-script-scriptssmoke-testsh)
- [5. Running Tests Locally (CLI Command Reference)](#5-running-tests-locally-cli-command-reference)

---

## 1. Testing Strategy & Principles

The Incident SRE Agent relies on an asynchronous, multi-agent architecture integrating public ticketing platforms with private cloud infrastructure. Testing follows four core principles:

1. **Deterministic Quality Gates**: Local test suites and CI pipelines execute completely offline without requiring active GCP credentials, cloud infrastructure, or external network calls.
2. **Byte-Identical Contract Integrity**: Data schemas shared between `incident-bridge` and `sre-agent` are validated with byte-level precision (`scripts/check-contracts.sh`) to eliminate interface drift.
3. **Defense-in-Depth Security Verification**: Every trust boundary (webhook authentication, Cloud Tasks dispatch, Cloud Run private ingress, and callback delivery) undergoes negative testing against forgery, tampering, and privilege escalation.
4. **Safety-Guaranteed Production Probes**: Post-deployment smoke tests and fault simulations are strictly read-only against production GCP telemetry; only isolated probe incidents are dispatched.

---

## 2. Test Layer Architecture (The 5 Test Layers)

```
┌────────────────────────────────────────────────────────────────────────┐
│ Layer 5: End-to-End Failure Scenarios & Results Tracking               │
│  • 5 realistic failure modes (checkout errors, latency, bad release)   │
│  • Full diagnostic lifecycle: injection -> triage -> synthesis -> ack │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Layer 4: Negative & Security Verification Checks                       │
│  • Webhook token / HMAC rejection (HTTP 401)                           │
│  • OIDC token forgery, wrong audience, wrong caller SA (HTTP 401/403)  │
│  • Private Cloud Run ingress protection, SSRF callback allowlist       │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Layer 3: Per-Step Post-Deployment Verification (Deployment Steps)      │
│  • Step-by-step verification commands for every deployed GCP component│
│  • Automated validation via scripts/smoke-test.sh                      │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Layer 2: Local Multi-Container E2E Testing with Fake Ticket Server     │
│  • docker-compose: bridge + sre-agent + fake-ticket-server             │
│  • Full ServiceNow / Jira REST lifecycle with in-memory task queue     │
└───────────────────────────────────┬────────────────────────────────────┘
                                    │
┌───────────────────────────────────▼────────────────────────────────────┐
│ Layer 1: Unit & Component Tests (309+ Pytest Tests)                    │
│  • bridge/tests/ (95 tests): adapters, deduplication, callback auth    │
│  • agent/tests/ (175 tests): specialists, ADK wiring, fail-fast, ADC   │
│  • demo/tests/ (39 tests): shop-api, inventory-svc, loadgen, scenarios │
└────────────────────────────────────────────────────────────────────────┘
```

---

### Layer 1: Unit & Component Tests (309+ pytest Tests)

The repository contains 309+ pytest tests covering contract enforcement, adapter parsing, telemetry inspection tools, multi-agent dispatching, timeout budgets, failure isolation, ADC client factories, production fail-fast assertions, and Google ADK multi-agent wiring:

```bash
pytest -v bridge agent demo
# Result: 309 passed, 4 deselected in ~5.9s
```

#### Test Suite Breakdown

| Directory | Test Suite | Tests | Key Behaviors Verified |
|---|---|:---:|---|
| `bridge/tests/` | [`test_contracts.py`](../bridge/tests/test_contracts.py) | 16 | Incident, Request, Report, Findings, Result Pydantic v2 schemas; UTC normalization; extra field rejection. |
| `bridge/tests/` | [`test_adapters.py`](../bridge/tests/test_adapters.py) | 12 | ServiceNow, Jira, and Generic webhook adapters; HMAC-SHA256 signature verification; ticket normalization; HTML and ADF report rendering. |
| `bridge/tests/` | [`test_tasks_dedupe.py`](../bridge/tests/test_tasks_dedupe.py) | 3 | Deterministic task ID generation (`inv-<sha256>`); Cloud Tasks 409 conflict handling; in-memory deduplication fallback. |
| `bridge/tests/` | [`test_callback_auth.py`](../bridge/tests/test_callback_auth.py) | 8 | OIDC Bearer token verification; audience matching; caller service account validation; rejection of mock tokens in production. |
| `bridge/tests/` | [`test_config.py`](../bridge/tests/test_config.py) | 3 | Configuration loading, environment overrides, singleton caching. |
| `bridge/tests/` | [`test_e2e.py`](../bridge/tests/test_e2e.py) | 4 | In-memory end-to-end simulation of ServiceNow and Jira incident lifecycles, duplicate webhooks, and failure reporting. |
| `agent/tests/` | [`test_contracts.py`](../agent/tests/test_contracts.py) | 16 | Agent-side verification of byte-identical data contracts. |
| `agent/tests/` | [`test_config.py`](../agent/tests/test_config.py) | 3 | Agent settings, model names (`gemini-3.8-flash`), execution timeouts, callback allowlists. |
| `agent/tests/` | [`test_triage.py`](../agent/tests/test_triage.py) | 4 | Dynamic time window calculation (`[opened_at - 60m, min(opened_at + 30m, now)]`); 24-hour clamping; specialist selection. |
| `agent/tests/` | [`test_tool_registry.py`](../agent/tests/test_tool_registry.py) | 4 | Tool registry immutability; verification that only read-only GCP tools are registered; mutating APIs strictly blocked. |
| `agent/tests/` | [`test_tools.py`](../agent/tests/test_tools.py) | 10 | Cloud Logging queries, error group summarization, Cloud Monitoring metrics, Cloud Audit Logs, and Cloud Run env var redaction. |
| `agent/tests/` | [`test_specialists.py`](../agent/tests/test_specialists.py) | 5 | Logs, Metrics, Changes, and Resources specialists; isolation of single-specialist crashes. |
| `agent/tests/` | [`test_review.py`](../agent/tests/test_review.py) | 3 | ReviewEvaluator heuristic rules; triggering targeted follow-up query round (strictly capped at 1 round); merging findings. |
| `agent/tests/` | [`test_synthesizer.py`](../agent/tests/test_synthesizer.py) | 2 | SynthesizerAgent report compilation, timeline generation, root cause scoring, and partial specialist failure handling. |
| `agent/tests/` | [`test_skills_loader.py`](../agent/tests/test_skills_loader.py) | 4 | SRE skill discovery, YAML frontmatter parsing, rejecting scripts directories, schema validation. |
| `agent/tests/` | [`test_dispatcher.py`](../agent/tests/test_dispatcher.py) | 3 | Multi-agent lifecycle (Triage -> Fan-out -> Review -> Synthesis); timeout budget enforcement; exception containment. |
| `agent/tests/` | [`test_callback.py`](../agent/tests/test_callback.py) | 8 | Google ID token minting, SSRF host allowlist validation, structured logging, 5xx retry with backoff, immediate abort on 4xx. |
| `agent/tests/` | [`test_investigate_api.py`](../agent/tests/test_investigate_api.py) | 5 | `/health`, synchronous `/investigate` endpoint, Cloud Tasks 15m compliance, failure callback delivery. |
| `agent/tests/` | [`test_production_failfast.py`](../agent/tests/test_production_failfast.py) | 3 | Fail-fast assertions in production mode (`APP_ENV != 'local'`); prevents silent simulation trap when credentials or clients are missing; offline local fallback. |
| `agent/tests/` | [`test_real_client_factories.py`](../agent/tests/test_real_client_factories.py) | 3 | ADC client factories for Cloud Logging, Monitoring, Run, Compute, GKE, Cloud SQL Admin, and Cloud Build; instance caching and cache reset. |
| `agent/tests/` | [`test_adk_wiring.py`](../agent/tests/test_adk_wiring.py) | 6 | Google ADK `BaseAgent` Dispatcher hierarchy, `LlmAgent` Triage and Synthesizer schemas, `ParallelAgent` fan-out, tool segregation per specialist, and `SkillToolset` integration. |
| `demo/tests/` | [`test_shop_api.py`](../demo/tests/test_shop_api.py) | 10 | Shop API storefront endpoints, health probes, and fault injection triggers for Scenarios 1–5. |
| `demo/tests/` | [`test_inventory_svc.py`](../demo/tests/test_inventory_svc.py) | 7 | Stock reservation, inventory tracking, and Cloud Run IAM service account authentication enforcement. |
| `demo/tests/` | [`test_loadgen.py`](../demo/tests/test_loadgen.py) | 2 | Async traffic load generator, throughput statistics, and latency recording. |
| `demo/tests/` | [`test_create_incident.py`](../demo/tests/test_create_incident.py) | 5 | Incident payload building for ServiceNow, Jira, and Generic webhooks; command-line execution. |
| `demo/tests/` | [`test_scenarios_e2e.py`](../demo/tests/test_scenarios_e2e.py) | 5 | Diagnostic verification for all 5 failure scenarios against the multi-agent pipeline. |

---

### Layer 2: Local Multi-Container E2E Testing with Fake Ticket Server

For local multi-service testing, `docker-compose.yml` spins up the complete platform along with a mock ticketing server:

```
┌─────────────────┐       POST /webhook/servicenow       ┌─────────────────┐
│                 ├─────────────────────────────────────►│                 │
│                 │                                      │ incident-bridge │
│                 │◄─────────────────────────────────────┤  (Port 8080)    │
│                 │       Update ticket & attach HTML    └────────┬────────┘
│                 │                                               │
│   fake-ticket-  │                                               │ In-Memory Task
│     server      │                                               │ / Cloud Tasks
│   (Port 8082)   │                                               ▼
│                 │                                      ┌─────────────────┐
│                 │       POST /callback/report          │    sre-agent    │
│                 │◄─────────────────────────────────────┤  (Port 8081)    │
└─────────────────┘                                      └─────────────────┘
```

#### What the Fake Ticket Server Simulates (`bridge/tests/fake_ticket_server.py`)
- **ServiceNow Table API**: `PATCH /api/now/table/incident/{sys_id}` (updates state to "In Progress" and appends work notes).
- **ServiceNow Attachment API**: `POST /api/now/attachment/file` (uploads standalone HTML report attachment).
- **Jira Cloud REST API v3 Comments**: `POST /rest/api/3/issue/{key}/comment` (posts ADF structured diagnostic comments).
- **Jira Cloud Transitions**: `GET` and `POST /rest/api/3/issue/{key}/transitions` (transitions issue state to "In Progress").
- **Jira Cloud Attachments**: `POST /rest/api/3/issue/{key}/attachments` (uploads standalone HTML report attachment).
- **Inspection & Reset APIs**: `GET /health`, `GET /api/now/table/incident/{sys_id}`, `GET /rest/api/3/issue/{key}`, and `POST /test/reset`.

#### Step-by-Step Local E2E Execution

1. **Start the multi-container stack**:
   ```bash
   docker-compose up --build
   ```

2. **Verify all services are healthy**:
   ```bash
   curl -s http://localhost:8080/health   # incident-bridge
   curl -s http://localhost:8081/health   # sre-agent
   curl -s http://localhost:8082/health   # fake-ticket-server
   ```

3. **Trigger a ServiceNow Incident Webhook**:
   ```bash
   curl -X POST http://localhost:8080/webhook/servicenow \
     -H "Authorization: Bearer ${SERVICENOW_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{
       "sys_id": "snow-local-101",
       "number": "INC0010101",
       "short_description": "High 500 error rates on shop-api",
       "description": "Checkout failure spike observed",
       "priority": "1",
       "cmdb_ci": "shop-api",
       "opened_at": "2026-10-09 10:00:00"
     }'
   ```
   *Expected Response:* HTTP 202 Accepted with `task_id: "inv-..."`.

4. **Verify Ticket Acknowledgement in Fake Ticket Server**:
   ```bash
   curl -s http://localhost:8082/api/now/table/incident/snow-local-101
   # Observe: "state": "2" (In Progress), work notes contain "Investigation underway"
   ```

5. **Trigger a Jira Cloud Webhook**:
   ```bash
   curl -X POST http://localhost:8080/webhook/jira \
     -H "X-Webhook-Token: ${JIRA_TOKEN}" \
     -H "Content-Type: application/json" \
     -d '{
       "issue": {
         "id": "30001",
         "key": "OPS-101",
         "fields": {
           "summary": "shop-api latency degradation",
           "priority": {"name": "High"},
           "components": [{"name": "shop-api"}],
           "created": "2026-10-09T10:00:00.000+0000"
         }
       }
     }'
   ```
   *Expected Response:* HTTP 202 Accepted with `task_id: "inv-..."`.

6. **Verify Jira Comment & Transition**:
   ```bash
   curl -s http://localhost:8082/rest/api/3/issue/OPS-101
   # Observe: status="In Progress", comments contain "Investigation underway"
   ```

7. **Clean Up**:
   ```bash
   docker-compose down -v
   ```

---

### Layer 3: Per-Step Post-Deployment Verification Checks

When deploying to Google Cloud Platform, execute the following verification commands after each step matching standard deployment procedures:

#### Step 1: Pre-Flight Tooling & Environment Verification
```bash
# Verify CLI binaries
which gcloud terraform uv

# Check data contracts byte-synchronization
bash scripts/check-contracts.sh

# Verify GCP authentication
gcloud auth list
gcloud config get-value project
```

#### Step 2: SRE Agent Deployment Verification
```bash
# 1. Verify required Google APIs enabled
gcloud services list --enabled --project="${PROJECT_ID}" \
  --filter="NAME:(run.googleapis.com OR cloudtasks.googleapis.com OR aiplatform.googleapis.com OR logging.googleapis.com OR monitoring.googleapis.com)"

# 2. Verify agent-sa has read-only IAM viewer roles
gcloud projects get-iam-policy "${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --format="table(bindings.role)" \
  --filter="bindings.members:agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Expected roles: roles/logging.viewer, roles/monitoring.viewer, roles/run.viewer, roles/compute.viewer, roles/container.viewer, roles/cloudsql.viewer, roles/cloudbuild.builds.viewer, roles/aiplatform.user

# 3. Verify Cloud Tasks queue configuration
gcloud tasks queues describe investigations \
  --location="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="yaml(state,rateLimits,retryConfig)"

# 4. Verify SRE Agent Cloud Run service deployed with private ingress
gcloud run services describe sre-agent \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="yaml(status.url,status.conditions,spec.template.spec.timeoutSeconds)"

# 5. Verify tasks-invoker-sa has roles/run.invoker on sre-agent
gcloud run services get-iam-policy sre-agent \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --flatten="bindings[].members" \
  --filter="bindings.role:roles/run.invoker"
```

#### Step 3: Incident Bridge Deployment Verification
```bash
# 1. Verify Secret Manager secrets exist and have active versions
for secret in bridge-servicenow-webhook-token bridge-jira-webhook-token bridge-generic-hmac-key; do
  gcloud secrets versions list "${secret}" --project="${PROJECT_ID}" --filter="STATE=ENABLED"
done

# 2. Verify Incident Bridge Cloud Run service deployed
gcloud run services describe incident-bridge \
  --region="${REGION}" \
  --project="${PROJECT_ID}" \
  --format="yaml(status.url,status.conditions)"

# 3. Bridge liveness check
BRIDGE_URL=$(gcloud run services describe incident-bridge --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")
curl -f -s "${BRIDGE_URL}/health"
```

#### Step 4: Ticketing Webhook Connectivity Verification
```bash
# ServiceNow: Test Flow Designer connection alias
# Send test incident from ServiceNow UI or via bridge test script:
python3 demo/scripts/create_incident.py \
  --target snow \
  --bridge-url "${BRIDGE_URL}" \
  --scenario 1

# Jira Cloud: Verify Jira Automation rule execution in Jira Automation Audit Log:
# Check status: "SUCCESS: Web request sent to incident-bridge"
```

#### Step 5: Demo Stack Verification (If Deployed)
```bash
SHOP_URL=$(gcloud run services describe shop-api --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)")
curl -f -s "${SHOP_URL}/health"
curl -f -s "${SHOP_URL}/readyz"

# Verify load generator running
ps aux | grep loadgen.py
```

#### Step 6: Automated Smoke Test Verification
Run the master smoke test script (see [Section 4](#4-automated-post-deployment-smoke-test-script-scriptssmoke-testsh)):
```bash
./scripts/smoke-test.sh \
  --bridge-url "${BRIDGE_URL}" \
  --agent-url "${AGENT_URL}" \
  --generic-hmac-key "${GENERIC_HMAC_KEY}"
```

---

### Layer 4: Negative & Security Verification Checks

Negative testing confirms that all security guardrails, authentication filters, and least-privilege boundaries actively reject unauthorized requests:

#### 1. Inbound Webhook Authentication Rejection
```bash
# Test 1A: ServiceNow missing Authorization header -> HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/webhook/servicenow" \
  -H "Content-Type: application/json" -d '{"number":"INC001"}'
# Expect: 401

# Test 1B: ServiceNow invalid Bearer token -> HTTP 401
INVALID_TOKEN="invalid-token"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/webhook/servicenow" \
  -H "Authorization: Bearer ${INVALID_TOKEN}" \
  -H "Content-Type: application/json" -d '{"number":"INC001"}'
# Expect: 401

# Test 1C: Jira missing webhook token header -> HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/webhook/jira" \
  -H "Content-Type: application/json" -d '{"issue":{"key":"TEST-1"}}'
# Expect: 401

# Test 1D: Generic webhook with missing / forged HMAC signature -> HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/webhook/generic" \
  -H "X-Hub-Signature-256: sha256=badsignature00000000000000000000000000000000" \
  -H "Content-Type: application/json" -d '{"id":"gen-1"}'
# Expect: 401
```

#### 2. Callback Mutual Authentication Rejection
```bash
# Test 2A: Missing Authorization header -> HTTP 401
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/callback/report" \
  -H "Content-Type: application/json" -d '{"investigation_id":"inv-1"}'
# Expect: 401

# Test 2B: Forged Bearer token -> HTTP 401
FORGED_TOKEN="forged-token"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/callback/report" \
  -H "Authorization: Bearer ${FORGED_TOKEN}" \
  -H "Content-Type: application/json" -d '{"investigation_id":"inv-1"}'
# Expect: 401

# Test 2C: Mock token rejected in production environment -> HTTP 401
# (Bridge checks APP_ENV != "local"; if production, rejects mock tokens)
MOCK_TOKEN="mock-oidc-token-for-testing"
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/callback/report" \
  -H "Authorization: Bearer ${MOCK_TOKEN}" \
  -H "Content-Type: application/json" -d '{"investigation_id":"inv-1"}'
# Expect: 401
```

#### 3. Private SRE Agent Invocation Protection
```bash
# Direct public invocation of private agent must be rejected by Cloud Run IAM proxy
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${AGENT_URL}/investigate" \
  -H "Content-Type: application/json" -d '{}'
# Expect: 401 or 403 (Cloud Run IAM enforcement)
```

#### 4. Data Contract Boundary Protection
```bash
# Submitting incident with invalid severity -> Rejected by Pydantic validation (HTTP 400)
HMAC_SIG=$(python3 -c "import hmac, hashlib; print(hmac.new(b'test-hmac-key', b'{\"id\":\"1\",\"severity\":\"CRITICAL\"}', hashlib.sha256).hexdigest())")
curl -s -o /dev/null -w "%{http_code}\n" -X POST "${BRIDGE_URL}/webhook/generic" \
  -H "X-Hub-Signature-256: sha256=${HMAC_SIG}" \
  -H "Content-Type: application/json" -d '{"id":"1","severity":"CRITICAL"}'
# Expect: 400 (severity must be one of P1, P2, P3, P4)
```

#### 5. Telemetry Secret Redaction Verification
When the `ResourcesTool` inspects container specifications, it redacts any environment variables matching sensitive keywords:
- Key names containing `KEY`, `SECRET`, `TOKEN`, `PASSWORD`, `AUTH` are converted to `[REDACTED]`.
- Verified in `agent/tests/test_tools.py::test_describe_cloud_run_service_redacts_env_values`.

---

### Layer 5: End-to-End Scenario Testing with Results Template

The platform includes 5 production failure scenarios implemented in `demo/scripts/` and specified in `demo/SCENARIOS.md`:

| Scenario ID | Name | Trigger Script | Injected Fault | Expected Root Cause Diagnostic |
|---|---|---|---|---|
| **Scenario 1** | Checkout Error Spike | `bash demo/scripts/inject_fault.sh 1` | 40% HTTP 500 error rate on `/checkout` | Database connection pool exhaustion at `shop-db:5432` (`active: 100, max: 100`). |
| **Scenario 2** | Catalog Latency Spike | `bash demo/scripts/inject_fault.sh 2` | 3,000ms artificial delay on `/products` | Unindexed database query on product catalog; P99 latency degraded to >3.2s. |
| **Scenario 3** | Bad Release Regression | `bash demo/scripts/inject_fault.sh 3` | Deploy revision with missing `DB_PASSWORD` env var | `KeyError: 'DB_PASSWORD'` causing CrashLoop / 503 errors on startup. |
| **Scenario 4** | Memory Leak / OOM | `bash demo/scripts/inject_fault.sh 4` | Unbounded memory allocation per request | Container memory limit (2 GiB) exceeded; Cloud Run container terminated with SIGKILL (OOM). |
| **Scenario 5** | IAM Dependency Failure | `bash demo/scripts/inject_fault.sh 5` | Revoke `roles/run.invoker` from `shop-api` on `inventory-svc` | HTTP 403 Forbidden on inter-service inventory reservation calls. |

#### Scenario Test Execution Procedure

1. Inject the fault:
   ```bash
   bash demo/scripts/inject_fault.sh 1
   ```
2. Trigger the automated incident investigation:
   ```bash
   python3 demo/scripts/create_incident.py \
     --target snow \
     --bridge-url "${BRIDGE_URL}" \
     --scenario 1
   ```
3. Observe initial ticket acknowledgement (< 5 seconds):
   - Ticket status transitions to "In Progress".
   - Work note posted: `"SRE Agent: Investigation underway..."`.
4. Observe diagnostic dossier delivery (< 5 minutes):
   - Correlated root cause posted into work notes.
   - Chronological investigation timeline rendered.
   - Remediation playbooks attached.
   - Complete standalone responsive HTML dossier attached (`sre_investigation_report_INCxxxx.html`).
5. Reset the fault:
   ```bash
   bash demo/scripts/reset.sh
   ```

#### Scenario Results Recording Template

Use this markdown table template to record and archive end-to-end test execution runs:

| Test Run ID | Execution Date (UTC) | Scenario ID & Description | Ticketing Target | Ticket Key | Ack Latency | Report Latency | Root Cause Hypothesis Match | Confidence | HTML Dossier Attached | Result |
|---|---|---|---|---|:---:|:---:|---|:---:|:---:|:---:|
| `RUN-20261009-01` | 2026-10-09 14:00 | **Scenario 1**: Checkout Error Spike | ServiceNow | `INC0019482` | 1.8s | 214s | **MATCH**: DB connection pool exhaustion (`shop-db:5432`) | 0.95 | Yes | **PASS** |
| `RUN-20261009-02` | 2026-10-09 14:15 | **Scenario 2**: Catalog Latency Spike | Jira Cloud | `OPS-2041` | 2.1s | 198s | **MATCH**: Latency degradation in `/products` catalog query | 0.88 | Yes | **PASS** |
| `RUN-20261009-03` | 2026-10-09 14:30 | **Scenario 3**: Bad Release Regression | ServiceNow | `INC0019485` | 1.9s | 230s | **MATCH**: Missing `DB_PASSWORD` env var in revision | 0.95 | Yes | **PASS** |
| `RUN-20261009-04` | 2026-10-09 14:45 | **Scenario 4**: Memory Leak / OOM | Jira Cloud | `OPS-2045` | 2.4s | 245s | **MATCH**: Memory saturation exceeding 2 GiB container limit | 0.85 | Yes | **PASS** |
| `RUN-20261009-05` | 2026-10-09 15:00 | **Scenario 5**: IAM Dependency Failure | Generic | `GEN-501` | 1.2s | 185s | **MATCH**: HTTP 403 on `inventory-svc` reservation call | 0.92 | Yes | **PASS** |

---

## 3. LLM Stub vs Real Gemini Execution

### Dual Execution Mode Philosophy

The Incident SRE Agent implements a decoupled multi-agent reasoning architecture with dual execution capabilities governed by `APP_ENV`:

1. **Local Offline Mode (`APP_ENV=local`)**:
   - **Purpose**: Fast unit tests, CI/CD runners, and offline local development (`docker-compose`).
   - **GCP Clients**: Telemetry tools without an injected client return deterministic mock/empty responses without attempting live network calls to GCP.
   - **ADK / LLM Agents**: Fall back cleanly to deterministic heuristic triage and report synthesis, marking `synthesizer_mode: "heuristic_fallback"` in report metadata.
   - **Benefits**: Zero latency, zero external API quota, 100% offline, fully reproducible test assertions.
2. **Production Mode (`APP_ENV != 'local'`) with Fail-Fast Assertions**:
   - **Purpose**: Production staging and live incident investigations on Cloud Run.
   - **GCP Clients via ADC**: Tools lazily construct real Google Cloud clients (`google-cloud-logging`, `google-cloud-monitoring`, `google-cloud-run`, `google-cloud-compute`, `google-cloud-container`, `sqladmin`, `google-cloud-build`) using Application Default Credentials (ADC).
   - **Fail-Fast Production Guard**: If a tool or the Dispatcher is invoked in production without valid GCP credentials or client connections, it **MUST raise an explicit `RuntimeError` immediately** (`RuntimeError("... unavailable in production environment")`). It will **never** silently return empty telemetry data, eliminating the simulation trap.
   - **Google ADK Multi-Agent Reasoning**:
     - `Dispatcher`: Custom ADK `BaseAgent` orchestrating the investigation lifecycle.
     - `Triage`: ADK `LlmAgent` formulating a structured `InvestigationPlan`.
     - `Specialists`: Executed in parallel via ADK `ParallelAgent`, each specialist being an `LlmAgent` equipped with strictly segregated read-only `FunctionTool` tools and prompts.
     - `Review`: Coordinates up to 1 targeted follow-up query round based on initial findings.
     - `Synthesizer`: ADK `LlmAgent` with `output_schema=Report` producing root cause diagnosis, timeline, and remediation recommendations.
     - `Skills`: Loaded progressively via ADK `SkillToolset` according to `skills.yaml` with code execution disabled.

---

### Stubbed / Heuristic Mode

In stubbed mode, `SynthesizerAgent` (`agent/app/synthesizer.py`) executes deterministic heuristic correlation over specialist findings without invoking Vertex AI:

- **Root Cause Determination Rules**:
  - `Recent Change + Log Errors`: Identifies deployment regression (`confidence = 0.88`).
  - `Resource Saturation + Log Errors`: Identifies OOM / CPU exhaustion cascading to errors (`confidence = 0.82`).
  - `Metric Anomalies + Log Errors`: Identifies concurrent latency degradation and error spike (`confidence = 0.75`).
  - `Log Errors Only`: Highlights primary application exception (`confidence = 0.65`).
  - `Recent Change Only`: Cites pre-incident deployment (`confidence = 0.55`).
  - `Inconclusive`: Returns baseline diagnostic notice (`confidence = 0.30`).
- **Timeline Reconstruction**: Combines incident open timestamp with timestamped evidence from each specialist, sorted chronologically in UTC.
- **Remediation Recommendations**: Dynamically generates actions based on matched SRE skills (e.g., rolling back revision, inspecting Error Reporting, increasing container memory limits).
- **Metadata Tagging**: Explicitly sets `metadata["synthesizer_mode"] = "heuristic_fallback"`.

---

### Real Vertex AI Gemini Mode

In real execution mode, the agent queries Google Vertex AI Gemini models via `google-adk` and `google-genai`:

- **Dispatcher & Synthesis Reasoning**: `MODEL_DISPATCHER=gemini-3.8-flash` (configurable via `MODEL_DISPATCHER` or `GEMINI_PRO_MODEL`)
  - Formulates dynamic triage plans, parses incident symptoms, selects specialists, and performs final multi-modal synthesis.
- **Specialist Telemetry Analysis**: `MODEL_SPECIALIST=gemini-3.8-flash` (configurable via `MODEL_SPECIALIST` or `GEMINI_FLASH_MODEL`)
  - High-throughput parsing of structured log streams, time series anomaly detection, and Cloud Audit Log filtering.
- **Structured Pydantic Contract Output**: Prompts enforce JSON schema generation strictly matching the canonical `Report` contract.

---

### Switching Instructions (Local, Docker, Cloud)

#### 1. Running Locally with LLM Stubbing (Default)
Local unit tests and mock environments run in stubbed mode by default:
```bash
export APP_ENV=local
pytest agent/tests/test_synthesizer.py
```

#### 2. Running Locally with Real Vertex AI Gemini
To test real Gemini reasoning on your local workstation against your GCP project:
```bash
# 1. Authenticate Application Default Credentials
gcloud auth application-default login

# 2. Set environment variables
export APP_ENV=development
export GCP_PROJECT="your-target-project-id"
export GCP_REGION="us-central1"
export VERTEX_AI_PROJECT="your-target-project-id"
export VERTEX_AI_LOCATION="us-central1"
export MODEL_DISPATCHER="gemini-3.8-flash"
export MODEL_SPECIALIST="gemini-3.8-flash"

# 3. Launch the SRE agent locally
python3 -m uvicorn agent.app.main:app --port 8080
```

#### 3. Switching in Docker Compose
In `docker-compose.yml`, toggle between stubbed and real Gemini:
```yaml
sre-agent:
  environment:
    - APP_ENV=local           # Set to 'production' or 'development' for real Gemini
    - GCP_PROJECT=your-project
    - MODEL_DISPATCHER=gemini-3.8-flash
    - MODEL_SPECIALIST=gemini-3.8-flash
    # For real Gemini locally, mount host GCP credentials:
    # - GOOGLE_APPLICATION_CREDENTIALS=/tmp/keys/sa.json
```

#### 4. Cloud Run Production Deployment
In production Cloud Run, Gemini execution is enabled automatically via Terraform:
- `agent/infra/terraform/variables.tf`:
  ```hcl
  variable "model_dispatcher" {
    default = "gemini-3.8-flash"
  }
  variable "model_specialist" {
    default = "gemini-3.8-flash"
  }
  ```
- IAM Permissions: `agent-sa` is granted `roles/aiplatform.user` on the GCP project.

---

### Automatic Fallback Resiliency

If Vertex AI encounters rate limits (`429 ResourceExhausted`) or transient network partition:
1. The `SynthesizerAgent` catches the exception.
2. The agent transparently falls back to the deterministic heuristic engine.
3. A complete, structured `Report` is generated from raw specialist telemetry rather than dropping the investigation.
4. The incident ticket receives the diagnostic dossier with an explicit confidence flag.

---

## 4. Automated Post-Deployment Smoke Test Script (`scripts/smoke-test.sh`)

The repository includes `scripts/smoke-test.sh` to validate live Cloud Run deployments:

```bash
./scripts/smoke-test.sh \
  --bridge-url "https://incident-bridge-xyz-uc.a.run.app" \
  --agent-url "https://sre-agent-xyz-uc.a.run.app" \
  --generic-hmac-key "your-hmac-secret-key"
```

### Checks Executed by `scripts/smoke-test.sh`

1. **Check 1: Bridge Health Check**:
   - `GET ${BRIDGE_URL}/health` must return HTTP 200 with status healthy.
2. **Check 2: Inbound Webhook Authentication Rejection**:
   - Unauthenticated `POST /webhook/servicenow` returns HTTP 401.
   - Unauthenticated `POST /webhook/jira` returns HTTP 401.
   - Unauthenticated `POST /webhook/generic` returns HTTP 401.
3. **Check 3: Duplicate Webhook Idempotency**:
   - Sends a signed Generic incident payload.
   - Replays the exact same payload; verifies bridge returns HTTP 200 with idempotent acknowledgement without creating a duplicate task.
4. **Check 4: Private SRE Agent Invocation Protection**:
   - (If `--agent-url` provided) Unauthenticated `POST ${AGENT_URL}/investigate` returns HTTP 401 or 403.
   - `GET ${AGENT_URL}/health` returns HTTP 200.
5. **Check 5: Callback Mutual Authentication Rejection**:
   - `POST ${BRIDGE_URL}/callback/report` without token returns HTTP 401.
   - `POST ${BRIDGE_URL}/callback/report` with forged token returns HTTP 401.
6. **Check 6: End-to-End Investigation Flow (Read-Only Test Ticket)**:
   - Dispatches a signed, read-only probe incident to `POST /webhook/generic`.
   - Confirms HTTP 200/202 response with active `task_id` dispatch.

### Safety Guarantee
`scripts/smoke-test.sh` is strictly read-only against production GCP infrastructure. The only write operation is the probe ticket itself into the ticketing adapter / Cloud Tasks queue.

---

## 5. Running Tests Locally (CLI Command Reference)

Always activate the local virtual environment:

```bash
source .venv/bin/activate
```

### Pytest Execution Commands

```bash
# 1. Run all 309+ unit and component tests
pytest -v bridge agent demo

# 2. Run only bridge tests
pytest -v bridge/tests

# 3. Run only agent tests
pytest -v agent/tests

# 4. Run only demo tests
pytest -v demo/tests

# 5. Filter tests by keyword expression
pytest -k "test_scenario" -v
pytest -k "test_callback" -v
pytest -k "test_adapters" -v

# 6. Verbose output showing slowest test durations
pytest -vv --durations=10 bridge agent demo

# 7. Generate terminal coverage report
pytest --cov=bridge/app --cov=agent/app --cov=demo --cov-report=term-missing
```

### Static Analysis & Verification Quality Gates

```bash
# 1. Byte-identical contract check
bash scripts/check-contracts.sh

# 2. No forbidden literals and un-externalized Gemini models scanner
# Reads FORBIDDEN_LITERALS env var (defaulting to environment-specific and cross-tenant identifiers)
bash scripts/check-no-literal-projects.sh

# Optional: scan for custom patterns
FORBIDDEN_LITERALS="custom-pattern1|custom-pattern2" bash scripts/check-no-literal-projects.sh

# 3. De-overfitting, AST root cause, and AST log hint guards
bash scripts/check-no-demo-overfit.sh

# 4. Code linting
ruff check bridge agent demo

# 5. Code formatting check
ruff format --check bridge agent demo

# 6. Strict static type analysis
mypy bridge agent demo

# 7. Terraform format and validation across all modules
(cd agent/infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate)
(cd bridge/infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate)
(cd demo/infra/terraform && terraform fmt -check && terraform init -backend=false && terraform validate)

# 8. Secret scanning
gitleaks detect --source . -v
```

Test fixtures must use obviously fake secret values (for example `fake-api-key-for-test`), never real vendor token prefixes such as `ATATT`, `ghp_`, `xoxb-`, `AKIA`, `AIza`, `glpat-`, or `sk_live_`.

---

### Continuous Integration (CI) Architecture

The subfolder GitHub Actions workflow (`.github/workflows/incident-sre-agent-dev-in-a-box-ci.yml`) executes automated presubmit verification on pushes and pull requests affecting `incident-sre-agent-dev-in-a-box/**`.

> [!NOTE]
> The workflow is strictly CI only (zero deploy jobs, no GCP credentials needed). All unit, component, contract, and guard checks run entirely in offline simulation mode (`APP_ENV=local`).

