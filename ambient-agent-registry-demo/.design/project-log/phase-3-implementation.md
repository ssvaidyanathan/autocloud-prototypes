# Project Log: Phase 3 - Infrastructure, Packaging, README & CI Workflow

**Date**: 2026-10-20  
**Phase**: Phase 3: Infrastructure, Packaging, README & CI Workflow  
**Agent**: `ambient-dev-p3`  
**Branch**: `feat/ambient-agent-registry-demo`  

---

## 1. Summary of Changes

Implemented Phase 3 for `ambient-agent-registry-demo` delivering production-grade Terraform infrastructure, container packaging, automated CI workflow, comprehensive documentation, and strict project literal isolation:

### 1. Terraform Infrastructure (`ambient-agent-registry-demo/infra/terraform/`)
- Configured root orchestration module (`main.tf`, `variables.tf`, `outputs.tf`, `versions.tf`):
  - **Resource Prefix**: Default `name_prefix = "ambient"`.
  - **Region**: Default `region = "us-central1"`.
  - **Service Accounts**: Configured `ambient-agent-sa` (16 chars) and `ambient-calc-sa` (15 chars) — strictly compliant with GCP's 30-character account ID limit.
  - **Cloud Run Services**: `ambient-agent-service` and `ambient-calculator-service`.
  - **Artifact Registry**: `ambient-docker-repo`.
  - **Agent Registry Service**: Registered `ambient-calculator` targeting the Calculator Cloud Run service `/mcp` endpoint with JSONRPC protocol binding.
- Implemented modular component hierarchy:
  - `modules/apis/`: Enables `run.googleapis.com`, `agentregistry.googleapis.com`, `aiplatform.googleapis.com`, `artifactregistry.googleapis.com`, `cloudresourcemanager.googleapis.com`, `logging.googleapis.com`, and `iamcredentials.googleapis.com`.
  - `modules/artifact_registry/`: Provisions Docker repository with automated image retention and untagged cleanup policies.
  - `modules/iam/`: Least-privilege IAM service accounts with `roles/aiplatform.user`, `roles/logging.logWriter`, and `roles/agentregistry.viewer`.
  - `modules/cloud_run/`: Provisions Cloud Run v2 services with HTTP `/health` startup/liveness probes, container limits, service-to-service IAM invoker grants, and container environment bindings.
  - `modules/agent_registry/`: Provisions `google_agent_registry_service` tool registration.
- Configured backend and variables templates:
  - `backend.hcl.example`: GCS remote state configuration with prefix `ambient-agent-registry-demo/default.tfstate`.
  - `terraform.tfvars.example`: Example variables template with `<PROJECT_ID>` placeholders and zero hardcoded literals.

### 2. Container Packaging (`ambient-agent-registry-demo/Dockerfile`)
- Authored multi-stage production Dockerfile based on `python:3.12-slim`:
  - Builder stage builds wheel and dependencies into `/install`.
  - Runner stage sets up non-root user `appuser` (UID 10001), copies installed packages to `/usr/local`, exposes port 8080.
  - Configures container HEALTHCHECK targeting `http://localhost:8080/health`.
  - Starts FastAPI application using `uvicorn app.main:app --host 0.0.0.0 --port 8080`.

### 3. GitHub Actions CI Workflow (`.github/workflows/ambient-agent-registry-demo-ci.yml`)
- Trigger configuration:
  - Runs on `push` and `pull_request` targeting `main` branch.
  - Filtered paths: `ambient-agent-registry-demo/**` and `.github/workflows/ambient-agent-registry-demo-ci.yml`.
- Configured 4 automated verification jobs:
  - `no-literals-check`: Executes `scripts/check-no-literal-projects.sh` scanning for forbidden environment literals and un-externalized Gemini models.
  - `lint`: Sets up Python 3.12 via `astral-sh/setup-uv`, installs dependencies, and runs `ruff check .`.
  - `test`: Runs the automated pytest test suite (`pytest tests/`).
  - `terraform-validate`: Sets up Terraform 1.9.8, runs `terraform fmt -check`, `terraform init -backend=false`, and `terraform validate` across `infra/terraform/`.

### 4. Comprehensive Documentation (`ambient-agent-registry-demo/README.md`)
- Complete architectural blueprint and operations guide:
  - High-level component architecture diagram (ASCII & Mermaid sequence diagram).
  - Detailed explanation of Google ADK native synchronous `/run` runtime (`RunAgentRequest` -> `list[Event]`).
  - Dynamic GCP Agent Registry discovery and in-memory ZIP package loading (zero container disk persistence).
  - Hot-reloading engine via `@app.post("/admin/cache/invalidate")`.
  - FastMCP remote tool execution, IAM boundaries, and zero local fallback computation policy.
  - Local development, unit testing, and offline mock server guide (`MOCK_REGISTRY=true`).
  - Step-by-step GCP deployment walkthrough using Terraform and registration scripts.
  - Live demonstration walkthrough and teardown instructions.

---

## 2. Verification Results

- **Terraform Formatting Check**:
  - `terraform -chdir="ambient-agent-registry-demo/infra/terraform" fmt -check`: **PASS (Clean)**.
- **Terraform Module Initialization & Validation**:
  - `terraform -chdir="ambient-agent-registry-demo/infra/terraform" init -backend=false`: **PASS**.
  - `terraform -chdir="ambient-agent-registry-demo/infra/terraform" validate`: **PASS (Success! The configuration is valid.)**.
- **GitHub Actions Workflow Syntax**:
  - Validated via YAML parser: **PASS** (`jobs: ['no-literals-check', 'lint', 'test', 'terraform-validate']`).
- **Forbidden Literal Identifier Scan**:
  - Verified zero occurrences of environment-specific project literals across all created Phase 3 files.
- **File Ownership & Boundary Verification**:
  - Only Phase 3 assigned files were created; zero existing prototype or sibling project files were touched.
