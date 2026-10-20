# Commit 386bfa7: Phase 4 Terraform Infrastructure Stack & Container Packaging — Review

## Executive Summary
Phase 4 implements the complete Terraform infrastructure stack and production container packaging for the Agent Registry Demo prototype under `agent-registry-demo/`. The implementation adheres strictly to the approved architecture (`design.md`), satisfies all 7 Phase 4 acceptance criteria, and enforces rigorous cloud security and least-privilege IAM controls. Zero modifications or overlaps with existing prototype state (`gdi-*`) were introduced. Automated validation (`terraform validate`, `terraform fmt -check`, regression test suite 38/38 passed) completed cleanly.

- **Verdict**: **APPROVE**
- **Risk Level**: **LOW** (0 Critical, 0 High, 0 Medium, 1 Low, 3 Info)

---

## Review Scope & Summary of Changes
- **Target Commit**: `386bfa7` (`feat(agent-registry-demo): Phase 4 Terraform infrastructure stack`)
- **Author**: `ard-dev-p4`
- **Reviewer**: `ard-rev-p4` (Role: Security Auditor)
- **Repository Path**: `/scion-volumes/scratchpad/projects/agent-registry-demo/repo`
- **Reviewed Files**:
  - `agent-registry-demo/Dockerfile`: Production multi-stage Dockerfile based on `python:3.12-slim` running as non-root UID 10001 (`appuser`), omitting local skills directory.
  - `agent-registry-demo/infra/terraform/versions.tf`: Specifies Terraform `>= 1.5.0`, provider `hashicorp/google >= 5.0.0`, and parameterized remote GCS backend.
  - `agent-registry-demo/infra/terraform/main.tf`: Root module orchestrating all submodules with common labels and resource naming conventions.
  - `agent-registry-demo/infra/terraform/variables.tf`: Root variables defining project, region, resource naming prefix (`ard`), Cloud Run sizing, timeouts, and Agent Registry endpoint.
  - `agent-registry-demo/infra/terraform/outputs.tf`: Exports service URLs, service account emails, and Agent Registry MCP resource descriptors.
  - `agent-registry-demo/infra/terraform/backend.hcl.example`: Example backend configuration with isolated state prefix `agent-registry-demo/default.tfstate`.
  - `agent-registry-demo/infra/terraform/terraform.tfvars.example`: Example deployment variables.
  - `agent-registry-demo/infra/terraform/modules/apis/`: Enables required GCP APIs with `disable_on_destroy = false`.
  - `agent-registry-demo/infra/terraform/modules/artifact_registry/`: Provisions Docker repository for container images.
  - `agent-registry-demo/infra/terraform/modules/iam/`: Provisions dedicated least-privilege service accounts (`ard-agent-sa`, `ard-calculator-sa`).
  - `agent-registry-demo/infra/terraform/modules/cloud_run/`: Provisions Cloud Run v2 services for agent and calculator with resource limits, timeouts, `/health` probes, and scoped invoker IAM binding.
  - `agent-registry-demo/infra/terraform/modules/agent_registry/`: Provisions `google_agent_registry_service` calculator MCP server integration.
  - `agent-registry-demo/.design/project-log/2026-10-20-phase-4-terraform.md`: Phase 4 delivery log.

---

## Verification of Acceptance Criteria

| # | Acceptance Criterion | Status | Evidence / Verification Method |
|---|----------------------|--------|--------------------------------|
| 1 | `terraform validate` passes cleanly | **PASS** | Executed `terraform -chdir=... init -backend=false && terraform -chdir=... validate`: returned `Success! The configuration is valid.` |
| 2 | `terraform fmt -check` passes cleanly | **PASS** | Executed `terraform -chdir=... fmt -check -recursive`: exited 0 with 0 formatting discrepancies. |
| 3 | Strict isolation: zero modifications or references to existing `gdi-*` state or resources. Distinct state prefix `agent-registry-demo/default.tfstate`. Resource prefix `ard-`. | **PASS** | Inspected all files: state prefix in `backend.hcl.example` is `agent-registry-demo/default.tfstate`. All resource names and labels default to `name_prefix = "ard"`. Zero references to `gdi-*`. |
| 4 | IAM least privilege: Service accounts (`ard-agent-sa`, `ard-calculator-sa`) have strictly bounded permissions (`roles/aiplatform.user`, `roles/logging.logWriter`, `roles/agentregistry.viewer`, no owner/editor roles). | **PASS** | Inspected `modules/iam/main.tf`: `agent_roles` limited to `aiplatform.user`, `logging.logWriter`, `agentregistry.viewer`. `calculator_roles` limited strictly to `logging.logWriter`. |
| 5 | Tool service IAM: `roles/run.invoker` bound to `agent-sa`. | **PASS** | Inspected `modules/cloud_run/main.tf:133-139`: `google_cloud_run_v2_service_iam_member.agent_invokes_calculator` binds `roles/run.invoker` on `calculator` directly to `agent_sa_email`. |
| 6 | Agent Registry service registration defined cleanly via `google_agent_registry_service` (or fallback). | **PASS** | Inspected `modules/agent_registry/main.tf:1-16`: declares `google_agent_registry_service.calculator` with `mcp_server_spec { type = "NO_SPEC" }` and `interfaces { protocol_binding = "HTTP_JSON", url = ... }`. |
| 7 | Cloud Run definitions configure CPU, memory, port, and `/health` probe. | **PASS** | Inspected `modules/cloud_run/main.tf`: both `agent` and `calculator` configure `cpu`, `memory`, `container_port = 8080`, `startup_probe` (`/health`), and `liveness_probe` (`/health`). |

---

## Security Audit Report

### Summary
- **Critical**: 0
- **High**: 0
- **Medium**: 0
- **Low**: 1
- **Info / Nit**: 3

---

### Findings

#### [LOW] [Security / Network] Public Ingress Default on Internal Tool Provider Microservice
- **Location**: [agent-registry-demo/infra/terraform/modules/cloud_run/main.tf:5](file:///scion-volumes/scratchpad/projects/agent-registry-demo/repo/agent-registry-demo/infra/terraform/modules/cloud_run/main.tf#L5) and [agent-registry-demo/infra/terraform/variables.tf:86-88](file:///scion-volumes/scratchpad/projects/agent-registry-demo/repo/agent-registry-demo/infra/terraform/variables.tf#L86-L88)
- **Description**: The root variable `ingress` defaults to `"INGRESS_TRAFFIC_ALL"` and is passed identically to both the public-facing agent service and the backend `calculator` tool service:
  ```hcl
  resource "google_cloud_run_v2_service" "calculator" {
    project  = var.project_id
    location = var.region
    name     = var.calculator_service_name
    ingress  = var.ingress
    ...
  }
  ```
- **Impact**: While unauthorized calls to `calculator` are blocked at the application/IAM layer by Cloud Run (since `roles/run.invoker` is granted strictly to `agent-sa` and not `allUsers`), configuring `INGRESS_TRAFFIC_ALL` exposes the tool service endpoint directly to the public internet at the network layer. In production environments, internal microservices should employ network-level isolation as defense-in-depth against credential replay or unexpected network exposure.
- **Proof of Concept**: Any external client can reach the HTTPS endpoint of `calculator-tool-service-<hash>.run.app` directly over the public internet, forcing Cloud Run to process TLS termination and IAM authentication checks rather than rejecting the traffic at the perimeter.
- **Recommendation**: Allow distinct ingress configurations for the agent and calculator services, defaulting the tool service to `INGRESS_TRAFFIC_INTERNAL_ONLY` (or `INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER` when within a VPC/VPC-SC perimeter):
  ```hcl
  variable "agent_ingress" {
    description = "Ingress setting for the Agent Service"
    type        = string
    default     = "INGRESS_TRAFFIC_ALL"
  }

  variable "tool_ingress" {
    description = "Ingress setting for the Calculator Tool Service"
    type        = string
    default     = "INGRESS_TRAFFIC_INTERNAL_ONLY" # or INGRESS_TRAFFIC_ALL for serverless demo
  }
  ```

---

#### [INFO] [Security / Container] Omission of Dockerfile Container-Level `HEALTHCHECK` Directive
- **Location**: [agent-registry-demo/Dockerfile:18-38](file:///scion-volumes/scratchpad/projects/agent-registry-demo/repo/agent-registry-demo/Dockerfile#L18-L38)
- **Description**: The Dockerfile specifies multi-stage compilation and runtime parameters, but omits an explicit container-level `HEALTHCHECK` directive:
  ```dockerfile
  USER appuser

  EXPOSE 8080

  CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
  ```
- **Impact**: In Google Cloud Run, container-level `HEALTHCHECK` instructions are ignored in favor of the infrastructure-level `startup_probe` and `liveness_probe` configured in Terraform (`modules/cloud_run/main.tf:35-53, 112-130`), so production liveness monitoring is fully operational. However, when running the container locally or in Docker-based CI smoke tests (`docker run`), container health cannot be inspected via standard `docker ps` status health indicators without this instruction.
- **Recommendation**: Consider adding an optional lightweight `HEALTHCHECK` directive for local runtime parity:
  ```dockerfile
  HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://localhost:8080/health').read()" || exit 1
  ```

---

#### [INFO] [Security / Data Protection] Artifact Registry Automated Vulnerability Scanning & Retention Policies
- **Location**: [agent-registry-demo/infra/terraform/modules/artifact_registry/main.tf:1-8](file:///scion-volumes/scratchpad/projects/agent-registry-demo/repo/agent-registry-demo/infra/terraform/modules/artifact_registry/main.tf#L1-L8)
- **Description**: The `google_artifact_registry_repository` is declared with default format `DOCKER` without explicit cleanup policies or vulnerability scanning declarations:
  ```hcl
  resource "google_artifact_registry_repository" "repo" {
    project       = var.project_id
    location      = var.region
    repository_id = var.repository_id
    description   = "Docker repository for Agent Registry Demo images"
    format        = "DOCKER"
    labels        = var.labels
  }
  ```
- **Impact**: While acceptable for a prototype, production repositories benefit from automated lifecycle cleanup policies (e.g. retaining the last N versions to prevent storage bloat) and automatic vulnerability scanning via Google Container Analysis.
- **Recommendation**: Consider configuring a basic retention cleanup policy for untagged or stale images when hardening for staging/production environments.

---

#### [INFO] [Correctness / IAM] Service Account ID Substring Clamping
- **Location**: [agent-registry-demo/infra/terraform/main.tf:8-9](file:///scion-volumes/scratchpad/projects/agent-registry-demo/repo/agent-registry-demo/infra/terraform/main.tf#L8-L9)
- **Description**: The root module derives service account IDs using `substr`:
  ```hcl
  agent_sa_account_id           = substr("${var.name_prefix}-agent-sa", 0, 30)
  calculator_tool_sa_account_id = substr("${var.name_prefix}-calculator-sa", 0, 30)
  ```
  GCP enforces a 6–30 character constraint on service account IDs (`^[a-z][a-z0-9-]{4,28}[a-z0-9]$`). With `var.name_prefix` validated to $\le 10$ characters (`variables.tf:7`), `ard-agent-sa` (12 chars) and `ard-calculator-sa` (17 chars) are well within the limit. However, if a custom 10-character prefix ending in a hyphen were truncated, it could leave a trailing hyphen, which GCP rejects.
- **Recommendation**: The regex validation on `var.name_prefix` (`^[a-z]([a-z0-9-]{0,8}[a-z0-9])?$`) currently prevents trailing hyphens in the prefix itself, which provides good guardrails. Adding an explicit minimum length assertion or using `trimsuffix(..., "-")` would offer defense-in-depth against malformed truncated strings.

---

## Positive Observations
1. **Scoped Least-Privilege IAM**: Rather than granting project-wide `roles/run.invoker` to `ard-agent-sa`, the permission is bound specifically to the target Cloud Run service via `google_cloud_run_v2_service_iam_member.agent_invokes_calculator`.
2. **Strict Isolation from Reference Stacks**: Zero overlap with `gdi-*` resources, services, or storage buckets. State files use an isolated prefix `agent-registry-demo/default.tfstate`.
3. **Defense-in-Depth Container Build**: Multi-stage build isolates build dependencies (`build-essential`) in the builder stage. The final runtime image runs as an explicit non-root user (`appuser`, UID 10001).
4. **Dynamic Registry Principle Enforced**: The Dockerfile intentionally avoids copying `skills/` into the image, upholding the core prototype goal of pure dynamic resolution from GCP Agent Registry.
5. **Safe API Enablement**: `google_project_service` sets `disable_on_destroy = false` across all services, ensuring that tearing down the demo stack does not inadvertently disable shared platform APIs in the host GCP project.
6. **Provider Accuracy**: Uses the native `google_agent_registry_service` resource from `hashicorp/google` provider with correct block types (`mcp_server_spec { type = "NO_SPEC" }` and `interfaces { protocol_binding = "HTTP_JSON" }`).

---

## Test & Validation Summary
1. **Terraform Initialization**: Cleanly initialized with `hashicorp/google v8.7.0` using `/scion-volumes/scratchpad/projects/gha-deploy-investigator/bin/terraform init -backend=false`.
2. **Terraform Validation**: `terraform validate` succeeded with `Success! The configuration is valid.`
3. **Terraform Formatting**: `terraform fmt -check -recursive` verified with zero formatting discrepancies.
4. **Automated Test Suite**: 38 / 38 unit and integration tests passed cleanly in 1.67s via `/scion-volumes/scratchpad/projects/agent-registry-demo/.venv/bin/pytest tests/`.
5. **Static Analysis & Linting**: `ruff check .` passed with 0 errors; `mypy` passed with 0 issues across all source files.

---

## Final Verdict
**APPROVE**

Commit `386bfa7` delivers a clean, secure, and fully verified Terraform infrastructure stack and Docker container packaging meeting all acceptance criteria for Phase 4. Non-blocking findings can be considered during future productionization.
