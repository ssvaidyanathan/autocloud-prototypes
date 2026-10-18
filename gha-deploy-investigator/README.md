# GitHub Actions Deployment Failure Investigator

[![CI Pipeline](https://github.com/ssvaidyanathan/autocloud-prototypes/actions/workflows/gha-deploy-investigator-ci.yml/badge.svg)](https://github.com/ssvaidyanathan/autocloud-prototypes/actions/workflows/gha-deploy-investigator-ci.yml)
[![License: Apache 2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](../LICENSE)
[![Python: 3.12+](https://img.shields.io/badge/Python-3.12%2B-blue.svg)](https://www.python.org/)
[![Terraform: >=1.5.0](https://img.shields.io/badge/Terraform-%3E%3D1.5.0-623ce4.svg)](https://www.terraform.io/)

An autonomous SRE diagnostic platform for continuous integration and delivery. When a GitHub Actions workflow job or run fails, `gha-deploy-investigator` ingests the event via secure webhooks, triages the failed pipeline stage, correlates build logs and runtime telemetry using Vertex AI Gemini reasoning models, opens an enriched ServiceNow incident record, and writes comprehensive diagnostic findings and ticket links directly back to GitHub Check Runs and Pull Requests.

---

## Table of Contents

- [1. Overview & Key Capabilities](#1-overview--key-capabilities)
- [2. Architecture Summary](#2-architecture-summary)
- [3. Quick Start & Local Testing](#3-quick-start--local-testing)
- [4. Multi-Stage Pipeline Scenarios](#4-multi-stage-pipeline-scenarios)
- [5. Configuration](#5-configuration)
- [6. Deployment](#6-deployment)
- [7. Documentation Index](#7-documentation-index)

---

## 1. Overview & Key Capabilities

When CI/CD pipelines fail, developers and platform engineers often lose hours wading through verbose log streams, attempting to reproduce environment-specific issues, or manually transferring failure data into enterprise ticketing systems.

`gha-deploy-investigator` automates this entire diagnostic lifecycle:
- **Instant Webhook Ingestion**: Receives GitHub `workflow_job` and `workflow_run` completion events with HMAC SHA-256 signature verification and delivery deduplication.
- **Multi-Stage Diagnostic Coverage**: Specialized diagnostic engines analyze failures across:
  - **Lint / Static Analysis**: Syntax errors, unformatted code, unused imports (ruff, flake8, black).
  - **Unit / Integration Tests**: Pinpoints failing test assertions, expected vs. actual values, and tracebacks.
  - **Build & Packaging**: Identifies missing build context files, Dockerfile syntax defects, and package installation issues.
  - **Deploy & Runtime**: Correlates Cloud Run deployment crashes, container startup failures, and memory exhaustion (OOM exit 137).
- **Reverse ServiceNow Incident Creation**: Automatically creates an enterprise ServiceNow ticket (`POST /api/now/table/incident`) with detailed diagnostic work notes and attaches a full interactive HTML report (`POST /api/now/attachment/file`).
- **Asynchronous GitHub Write-Back**: Updates GitHub Check Runs and comments on Pull Requests with actionable root-cause analysis and direct deep-links to the created ServiceNow ticket.
- **Strict Read-Only Guardrails**: 100% of diagnostic tools are marked `@readonly_tool` with AST invariant enforcement preventing any tampering with customer code repositories.

---

## 2. Architecture Summary

```
GitHub Workflow Failure ──► Inbound Webhook (HMAC-SHA256)
                                  │
                                  ▼
                         gdi-bridge (Public Cloud Run)
                                  │
                                  ▼ (Cloud Tasks OIDC)
                         gdi-agent (Private Cloud Run)
                           ├── PipelineLogsSpecialist
                           ├── RuntimeHealthSpecialist
                           ├── RepoCodeSpecialist
                           └── RootCauseSynthesizer (Vertex AI Gemini)
                                  │
                                  ▼ (POST /callback)
                         gdi-bridge
                           ├── ServiceNow Incident Creation (INC0019482)
                           ├── HTML Report Attachment
                           └── GitHub Check Run & PR Write-Back
```

For complete architecture diagrams, contract models, and security boundaries, see [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 3. Quick Start & Local Testing

### Prerequisites
- **Python 3.12+**
- `git clone https://github.com/ssvaidyanathan/autocloud-prototypes`

### Setup Local Environment
```bash
# 1. Clone repository
git clone https://github.com/ssvaidyanathan/autocloud-prototypes
cd autocloud-prototypes/gha-deploy-investigator

# 2. Create virtual environment and install dependencies
uv venv .venv --python 3.12
source .venv/bin/activate
uv pip install -e "./bridge[dev]" -e "./agent[dev]" -e "./demo[dev]"
```

### Run Tests Offline
All unit, component, and scenario tests execute 100% offline without external network or cloud dependencies:
```bash
pytest bridge/tests agent/tests demo/tests -v
```

### Run CI Guardrail Checks
```bash
# Verify byte-identical data contracts between bridge and agent
bash scripts/check-contracts.sh

# Verify AST static check on root-cause overrides
python3 scripts/check-no-rootcause-overrides.py

# Verify zero demo overfit in agent code
bash scripts/check-no-demo-overfit.sh

# Verify markdown links and zero forbidden literal identifiers
bash scripts/check-no-literal-projects.sh
```

---

## 4. Multi-Stage Pipeline Scenarios

The prototype includes a realistic customer-grade demo microservice under `demo/sample-pipeline-app/` and deterministic failure triggers under `demo/scenarios/`:

- **Scenario 1 (Lint)**: `./demo/scenarios/trigger-lint-failure.sh`
- **Scenario 2 (Test)**: `./demo/scenarios/trigger-test-failure.sh`
- **Scenario 3 (Build)**: `./demo/scenarios/trigger-build-failure.sh`
- **Scenario 4 (Deploy)**: `./demo/scenarios/trigger-deploy-failure.sh`
- **Reset**: `./demo/scenarios/reset-scenarios.sh`

For complete runbook instructions and standalone repository seeding details, see [demo/SCENARIOS.md](demo/SCENARIOS.md).

---

## 5. Configuration

Configuration is managed via environment variables and GCP Secret Manager.
- Detailed variable reference: [docs/CONFIGURATION.md](docs/CONFIGURATION.md)
- Personal GitHub App setup and manifest YAML: [docs/CONFIGURATION.md#2-github-app-setup-guide-personal-app-for-ssvaidyanathan](docs/CONFIGURATION.md)

---

## 6. Deployment

Infrastructure is provisioned using dedicated, parameterized Terraform manifests with zero committed secrets:
- **Bridge Stack**: `bridge/infra/terraform/` (Cloud Run, Cloud Tasks queue, Secret containers, IAM)
- **Agent Stack**: `agent/infra/terraform/` (Cloud Run internal, Secret container, IAM)
- **Demo WIF Stack**: `demo/infra/terraform/` (Workload Identity Pool, Provider, Demo Artifact Registry, Deployer SA)
- **Secret Replication Tooling**: `scripts/copy-secrets.sh`

### Key Deployment Invariants:
1. **Deployer IAM Roles**: Newcomers require specific least-privilege roles derived from Terraform (`workloadIdentityPoolAdmin`, `artifactregistry.admin`, `run.admin`, `serviceAccountAdmin`, `serviceAccountUser`, `secretmanager.admin`, `cloudtasks.admin`, `projectIamAdmin`, `cloudbuild.builds.editor`, `serviceUsageAdmin`). Do not rely on `roles/editor`.
2. **Strict 3-Stage Sequencing**:
   - **Stage 1**: Terraform provisions Secret Manager secret containers & IAM bindings.
   - **Stage 2**: Administrator populates secret values (Cloud Run requires at least one enabled version to deploy).
   - **Stage 3**: Container images are published and Cloud Run services are deployed.
3. **Demo WIF & Event Gating**: Demo pipeline authenticates keylessly via Workload Identity Federation scoped strictly to the demo repository on `refs/heads/main`.
4. **ServiceNow Exclusion**: Outbound ServiceNow incidents carry `correlation_display = "gha-deploy-investigator"` for bi-directional exclusion.

For the complete end-to-end deployment runbook, see [docs/DEPLOY.md](docs/DEPLOY.md).

---

## 7. Documentation Index

- [Architecture Design](docs/ARCHITECTURE.md)
- [Configuration Reference](docs/CONFIGURATION.md)
- [Deployment Guide](docs/DEPLOY.md)
- [Pipeline Scenarios Runbook](demo/SCENARIOS.md)
