# Project Log: Phase 4 Terraform Infrastructure Stack & Container Packaging

**Date**: 2026-10-20  
**Phase**: Phase 4: Terraform Infrastructure & Container Packaging  
**Agent**: `ard-dev-p4`  
**Branch**: `feat/agent-registry-demo`  

## 1. Summary of Changes

Implemented Phase 4 Terraform infrastructure stack and container packaging for the Agent Registry Demo prototype under `agent-registry-demo/`:

1. **`agent-registry-demo/Dockerfile`**:
   - Authored production multi-stage Dockerfile based on `python:3.12-slim`.
   - Builder stage compiles dependencies using wheel packaging from `pyproject.toml`.
   - Final runtime stage runs as non-root user `appuser` (UID 10001).
   - Core architectural principle enforced: zero build-time skill copying (`skills/` directory omitted), upholding dynamic retrieval from GCP Agent Registry.
   - Entrypoint runs `uvicorn app.main:app --host 0.0.0.0 --port 8080`.

2. **`agent-registry-demo/infra/terraform/` Root Stack**:
   - `versions.tf`: Specifies Terraform `>= 1.5.0`, provider `hashicorp/google >= 5.0.0`, and remote backend `gcs`.
   - `variables.tf`: Defines project configuration with defaults (`project_id = "cloud-assist-fde-7"`, `region = "us-central1"`, `environment = "demo"`, `name_prefix = "ard"`), configurable CPU/memory limits, timeouts, and `agent_registry_endpoint`.
   - `outputs.tf`: Exports `agent_service_url`, `calculator_tool_service_url`, `artifact_registry_repository`, `agent_sa_email`, `calculator_tool_sa_email`, `agent_registry_service_id`, and `agent_registry_resource`.
   - `main.tf`: Connects all submodules, configures labels, and wires outputs and dependencies across modules.
   - `backend.hcl.example` & `terraform.tfvars.example`: Example configurations for GCS backend state and project variables.

3. **Submodules under `modules/`**:
   - **`modules/apis/`**: Enables required Google Cloud APIs (`run.googleapis.com`, `agentregistry.googleapis.com`, `aiplatform.googleapis.com`, `artifactregistry.googleapis.com`, `cloudresourcemanager.googleapis.com`, `logging.googleapis.com`, `iamcredentials.googleapis.com`).
   - **`modules/artifact_registry/`**: Manages Docker repository (`${var.name_prefix}-docker-repo`) for agent and tool container images.
   - **`modules/iam/`**: Creates least-privilege service accounts:
     - `ard-agent-sa`: Granted `roles/aiplatform.user`, `roles/logging.logWriter`, and `roles/agentregistry.viewer`.
     - `ard-calculator-sa`: Granted `roles/logging.logWriter` only (no admin/owner permissions).
   - **`modules/cloud_run/`**:
     - `ard-agent-service`: Cloud Run v2 service configured with 1 vCPU, 1GiB RAM, `/health` startup/liveness probes, and runtime environment variables (`PROJECT_ID`, `REGION`, `AGENT_REGISTRY_ENDPOINT`, `TOOL_SERVICE_URL`).
     - `ard-calculator-service`: Cloud Run v2 service configured with 1 vCPU, 512MiB RAM, port 8080, and `/health` probes.
     - `google_cloud_run_v2_service_iam_member`: Restricts invocation of `ard-calculator-service` by granting `roles/run.invoker` strictly to `ard-agent-sa`.
   - **`modules/agent_registry/`**:
     - Provisions `google_agent_registry_service` "calculator" in GCP Agent Registry with `mcp_server_spec { type = "NO_SPEC" }` and `interfaces { protocol_binding = "HTTP_JSON", url = "${var.calculator_service_url}/mcp" }`.

## 2. Verification Results

- **Terraform Initialization**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/bin/terraform init -backend=false` initialized modules and plugins cleanly.
- **Terraform Validation**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/bin/terraform validate` succeeded with `Success! The configuration is valid.`
- **Terraform Formatting**: `/scion-volumes/scratchpad/projects/gha-deploy-investigator/bin/terraform fmt -check` passed with 0 formatting discrepancies.
- **Automated Regression Tests**: 34 / 34 existing unit tests passed via `pytest tests/`.

## 3. Architecture Decisions & Next Steps

- **Least Privilege Service Invocation**: Bound `roles/run.invoker` directly on the `ard-calculator-service` Cloud Run resource to `ard-agent-sa`, rather than granting broad project-level invoker permissions.
- **Strict Isolation**: Ensured zero overlap with existing `gdi-*` resources or state.
- **Handoff to Phase 5**: With infrastructure and container packaging defined and validated, Phase 5 will implement demo automation tooling (`publish_skill.sh`, `register_tool.sh`, `run_demo.sh`), end-to-end integration tests, and repository documentation.
