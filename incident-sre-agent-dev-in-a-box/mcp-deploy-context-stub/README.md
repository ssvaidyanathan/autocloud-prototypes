# Standalone Deployment Context MCP Server Stub Prototype

A standalone, generic Model Context Protocol (FastMCP) server prototype providing alert topology resolution and recent workload deployment context.

This service implements the generic deployment context contract, allowing SRE agents and automated triage bots to correlate incident indicators with infrastructure metadata without direct dependency on vendor-specific CMDBs.

---

## Features

- **FastMCP Standard Protocol:** Exposes standard MCP tools over Streamable HTTP at `/mcp` using the official Python SDK (`mcp[cli]`).
- **Liveness Probes:** Dedicated unauthenticated `/health` endpoint for Cloud Run and Kubernetes container health monitoring.
- **Hybrid Fixture Ingestion:**
  - Bundled sanitized template fixtures in `fixtures/` for local testing and CI.
  - Runtime GCS synchronization via `FIXTURES_GCS_URI` for loading real incident datasets dynamically without committing sensitive identifiers to Git.
- **Service-to-Service Security:** Google Cloud OIDC token verification with parameterized authorized caller service accounts (`AUTHORIZED_CALLER_SERVICE_ACCOUNTS`).
- **Resilient Empty Responses:** Typed empty responses (`found: false` / `[]`) for unmatched queries preventing JSON-RPC faults.
- **Infrastructure as Code:** Ready-to-deploy Terraform module in `infra/terraform/` for Google Cloud Run with internal ingress.

---

## Directory Structure

```text
mcp-deploy-context-stub/
├── CONTRACT.md                       # Standalone protocol specification
├── README.md                         # Service documentation and runbook
├── pyproject.toml                    # Package dependencies and configuration
├── Dockerfile                        # Multi-stage production container image
├── schemas/                          # JSON Schema contract validation definitions
│   ├── lookup_alert_context_request.json
│   ├── lookup_alert_context_response.json
│   ├── get_recent_deployments_request.json
│   └── get_recent_deployments_response.json
├── fixtures/                         # Sanitized synthetic template datasets
│   └── scenario-helm-rollout.json
├── app/                              # Server application package
│   ├── __init__.py
│   ├── main.py                       # Application bootstrap and entrypoint
│   ├── server.py                     # FastMCP tools and ASGI routing
│   ├── models.py                     # Pydantic data schemas
│   ├── indexer.py                    # In-memory fixture repository and search
│   ├── auth.py                       # Google OIDC token verification middleware
│   └── config.py                     # Settings and environment loader
├── tests/                            # Unit and conformance test suite
│   ├── __init__.py
│   ├── conftest.py                   # Pytest fixtures and test harnesses
│   ├── test_models.py                # Schema and alias unit tests
│   ├── test_indexer.py               # Repository and search unit tests
│   ├── test_server.py                # Tool execution and ASGI route tests
│   └── test_auth.py                  # Authentication middleware tests
└── infra/
    └── terraform/                    # Standalone Cloud Run deployment module
        ├── main.tf
        ├── variables.tf
        ├── outputs.tf
        └── versions.tf
```

---

## Configuration & Environment Variables

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `PORT` | integer | `8080` | Port for the HTTP server to listen on |
| `HOST` | string | `0.0.0.0` | Bind host address |
| `FIXTURES_DIR` | string | `fixtures` | Relative or absolute path to local bundled JSON fixtures |
| `FIXTURES_GCS_URI` | string | `""` | Optional GCS URI (`gs://bucket/prefix`) to sync fixtures at startup |
| `AUTH_DISABLED` | boolean | `false` | Set to `true` to disable OIDC authentication during local testing |
| `AUTHORIZED_CALLER_SERVICE_ACCOUNTS` | string | `""` | Comma-separated list of authorized caller service account emails |
| `EXPECTED_AUDIENCE` | string | `""` | Optional target URL audience for OIDC token verification |

---

## Local Development & Testing

### 1. Setup Virtual Environment

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Run Test Suite

Run the full automated test suite using `pytest`:

```bash
pytest tests/ -v
```

### 3. Run Locally

Start the local server with authentication disabled:

```bash
export AUTH_DISABLED=true
export PORT=8080
python -m app.main
```

Verify health endpoint:

```bash
curl -s http://localhost:8080/health
```

---

## Contract & Schemas

Detailed protocol specifications and tool input/output formats are documented in [CONTRACT.md](CONTRACT.md).
Formal JSON Schemas are stored in the [schemas/](schemas/) directory.

---

## Fixtures & Sanitization

The server is designed to operate safely without committing sensitive customer identifiers, private IPs, or internal cluster names to Git:
- **Bundled Synthetic Fixture (`fixtures/scenario-helm-rollout.json`):** Contains fully sanitized mock data (e.g. cluster `customer-cluster-1`, namespace `sample-app-ns`, workload `sample-workload`, IP `10.10.0.1`).
- **Dynamic GCS Loading:** In production or live staging environments, set `FIXTURES_GCS_URI` (e.g. `gs://<bucket>/fixtures`). At startup, the server automatically downloads and indexes external JSON datasets into memory without persisting them to disk or source control.
- **Strict Guard Conformance:** All bundled code and fixtures pass repository guard checks (`check-no-literal-projects.sh`, `gitleaks`, and zero forbidden literals).

---

## Deployment (Cloud Run via Terraform)

A complete, production-ready Terraform deployment module is provided in [infra/terraform/](infra/terraform/).

### Deploy Steps
1. Navigate to the terraform module:
   ```bash
   cd infra/terraform
   ```
2. Initialize and configure variables:
   ```bash
   terraform init
   terraform apply \
     -var="project_id=your-target-project-id" \
     -var="region=us-central1" \
     -var="image=us-central1-docker.pkg.dev/your-project/mcp-deploy-context-stub/mcp-deploy-context-stub:latest" \
     -var="authorized_caller_service_accounts=hitl-chatbot-sa@your-project.iam.gserviceaccount.com"
   ```
3. The module provisions:
   - Dedicated service account `mcp-deploy-stub-sa`
   - Internal-and-cloud-load-balancing Cloud Run service
   - IAM `roles/run.invoker` bindings for authorized caller service accounts

---

## How to Swap in a Real Server

The stub server implements a generic FastMCP deployment-context protocol. To transition from this in-memory prototype stub to a live enterprise CMDB, ArgoCD, Helm, or Spinnaker deployment context service:
1. **Preserve the Contract:** Ensure the production service implements the exact JSON-RPC schemas defined in [CONTRACT.md](CONTRACT.md) for `lookup_alert_context` and `get_recent_deployments`.
2. **Replace Data Ingestion:** Replace `app/indexer.py`'s in-memory fixture lookup with direct database or API queries against your live deployment system (e.g., Kubernetes API, ArgoCD REST API, GitLab/GitHub Deployments API, or CMDB).
3. **Keep Authentication Parity:** Retain Google Cloud OIDC token verification (`app/auth.py`) or map to your organization's mTLS / API gateway auth standard.
4. **Update Consumers:** The consuming agents (`hitl-chatbot` and `incident-sre-agent-dev-in-a-box`) communicate with this service purely over HTTP Streamable MCP (`/mcp`). Swapping the backend requires only updating the `MCP_SERVER_URL` environment variable on the callers.

