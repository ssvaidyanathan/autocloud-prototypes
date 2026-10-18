# Demo Pipeline & Multi-Stage CI/CD Failure Scenarios

This directory provides a customer-representative sample application, automated scenario injection tooling, and infrastructure automation for validating the autonomous `gha-deploy-investigator` system.

## Directory Structure

- `sample-pipeline-app/`: A realistic microservice application featuring a complete multi-stage GitHub Actions CI/CD workflow (`lint`, `test`, `build`, `deploy`), tests, and deployment scripts.
- `scenarios/`: Deterministic defect injection and reset scripts for testing the investigator across all pipeline stages.
- `infra/terraform/`: Terraform manifests for Workload Identity Federation (WIF) and Artifact Registry repository supporting keyless GitHub Actions authentication.
- `seed-demo-repo.sh`: Automation script to seed a standalone git repository with the clean sample pipeline application.
- `SCENARIOS.md`: Detailed catalog of all failure scenarios, runbooks, and evaluation guidelines.

## Declarative Deployment Invariant (`--clear-env-vars`)

A critical architectural invariant implemented in `sample-pipeline-app/scripts/deploy.sh` is declarative environment variable management:

```bash
ENV_ARGS=()
if [[ -n "${EXTRA_ENV_VARS}" ]]; then
    ENV_ARGS=("--set-env-vars" "${EXTRA_ENV_VARS}")
else
    ENV_ARGS=("--clear-env-vars")
fi
```

### Why `--clear-env-vars` is Essential
Google Cloud Run persists previously set environment variables across service revisions by default when updating or redeploying a service. If an injected defect or manual deployment introduced runtime environment variables (e.g. `CACHE_PREWARM_BUFFER_MB=2048`), subsequent deployments without explicit flags would continue to carry those variables forward into new revisions. 

By passing `--clear-env-vars` whenever `EXTRA_ENV_VARS` is empty, `scripts/deploy.sh` guarantees strictly declarative deployments: the runtime configuration in Cloud Run is deterministic and strictly matches the repository commit.

For detailed scenario runbooks and execution commands, see [SCENARIOS.md](SCENARIOS.md).
