# SRE Agent Service (`sre-agent`)

The `sre-agent` is an automated, strictly read-only incident investigation service built on Google Cloud. When an incident is received from `incident-bridge` via Cloud Tasks, the agent orchestrates a deterministic multi-agent workflow to inspect Cloud Logging, Cloud Monitoring, Cloud Audit Logs, and Google Cloud resource metadata, compiling its findings into an SRE incident report and delivering it back via an authenticated OIDC callback.

---

## Architecture & Workflow

The agent operates through four logical lifecycle phases coordinated by `Dispatcher`:

```
Incident Request
       │
       ▼
1. Triage Agent (TriagePlaybook)
   - Identifies affected service & classifies symptoms
   - Bounds UTC telemetry window (60m default, 24h hard limit)
   - Formulates InvestigationPlan and selects domain specialists
       │
       ▼
2. Parallel Fan-out (async concurrency)
   ├── LogsSpecialist      (Cloud Logging & Error Reporting)
   ├── MetricsSpecialist   (Cloud Monitoring time series & baseline shifts)
   ├── ChangesSpecialist   (Audit logs, Cloud Run revisions, Cloud Builds)
   └── ResourcesSpecialist (Cloud Run, GKE, Compute Engine, Cloud SQL)
       │
       ▼
3. Review Evaluator (<= 1 round guardrail)
   - Evaluates initial telemetry findings for anomalies/spikes
   - Dispatches targeted follow-up query (e.g., narrow log drill-down around deploy)
       │
       ▼
4. Synthesizer Agent (ReportStyle)
   - Combines evidence, anomalies, timeline, probable root cause, recommendations
   - Handles partial specialist errors gracefully
   - Produces validated Report contract
       │
       ▼
Callback Delivery (CallbackClient)
   - Validates HTTPS and host allowlist (*.run.app, localhost)
   - Attaches Google OIDC token (audience = callback_url)
   - 3x exponential backoff retry on transient 5xx/network errors
   - Emits structured audit log (jsonPayload.type = "sre_report")
```

---

## API Endpoints

### `POST /investigate`
Accepts an `InvestigationRequest` and initiates the investigation workflow.

- **Status Code:** `202 Accepted` (asynchronous background execution) or `200 OK` (when `?sync=true`).
- **Request Body:**
  ```json
  {
    "incident": {
      "id": "inc-1001",
      "source": "servicenow",
      "external_id": "sys_12345",
      "external_key": "INC0010001",
      "title": "Spike in HTTP 500 errors on shop-api",
      "description": "Customers experiencing errors during checkout",
      "severity": "P1",
      "service_hint": "shop-api",
      "labels": ["production", "checkout"],
      "opened_at": "2026-10-09T02:00:00Z"
    },
    "callback_url": "https://incident-bridge-xyz.run.app/callback/report",
    "requested_at": "2026-10-09T02:01:00Z"
  }
  ```
- **Response:**
  ```json
  {
    "status": "accepted",
    "investigation_id": "inv-a1b2c3d4",
    "message": "Investigation accepted and running in background"
  }
  ```

### `GET /health`
Liveness and readiness health probe. Note: `/health` is used because Google Frontend (GFE) edge proxies on Cloud Run intercept `/healthz`.
- **Status Code:** `200 OK`
- **Response:**
  ```json
  {
    "status": "healthy",
    "service": "sre-agent"
  }
  ```

---

## Configuration & Environment Variables

Configured via `AgentConfig` using `pydantic-settings`:

| Environment Variable | Default | Description |
|---|---|---|
| `GCP_PROJECT` | *(Required)* | Target Google Cloud project ID (e.g. `your-project-id`) |
| `GCP_REGION` | `us-central1` | Target Google Cloud region |
| `VERTEX_LOCATION` | `global` | Vertex AI endpoint location for Gemini models |
| `ALLOWED_CALLBACK_HOSTS` | `*.run.app` | Comma-separated allowed hostnames or patterns |
| `INVESTIGATION_TIMEOUT_SECONDS` | `600` | Hard deadline for entire investigation lifecycle |
| `MAX_TOOL_CALLS_PER_SPECIALIST` | `25` | Maximum tool invocations permitted per specialist |
| `MAX_QUERY_WINDOW_HOURS` | `24` | Maximum allowable telemetry search window |

---

## Local Verification & Development

Run all code formatting, linting, and testing locally:

```bash
# 1. Sync contracts
bash scripts/check-contracts.sh

# 2. Linting & Formatting
ruff check agent
ruff format --check agent
mypy agent

# 3. Test execution
pytest -v agent/tests
```
