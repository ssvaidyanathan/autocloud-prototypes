# Sample Pipeline App (Demo Microservice)

A production-grade FastAPI microservice demonstrating multi-stage GitHub Actions CI/CD pipeline failure investigation by the `gha-deploy-investigator` autonomous agent.

## Architecture & Features

- **Framework**: FastAPI with Pydantic v2 validation.
- **Functionality**: Customer order processing, health checks, and metadata endpoints.
- **Pipeline Workflow**: `.github/workflows/pipeline.yml` featuring 4 sequenced stages:
  1. `lint`: Code style, unused imports, and syntax verification with `ruff`.
  2. `unit-test`: Isolated test suite verification with `pytest`.
  3. `build-container`: Multi-stage Docker image packaging.
  4. `deploy-cloudrun`: Deployment to Google Cloud Run with memory and environment configuration.

---

## Local Development

### Prerequisites
- Python 3.11+
- Virtual environment with project dependencies:
  ```bash
  pip install -e .[test]
  ```

### Running the Microservice Locally
```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

### Running Unit Tests
```bash
pytest tests/ -v
```

### Running Linter
```bash
ruff check app/
```

---

## Fault Injection Scenarios

Deterministic fault injection scripts under `scenarios/` simulate realistic CI/CD pipeline failures across all four stages:

| Scenario | Stage | Script | Failure Mechanism |
|---|---|---|---|
| **1. Lint Failure** | `lint` | `scenarios/trigger-lint-failure.sh` | Injects unused imports (`F401`) and multiple statements on a single line (`E701`) into `app/main.py`. |
| **2. Test Failure** | `unit-test` | `scenarios/trigger-test-failure.sh` | Injects broken assertion `test_order_discount_calculation_failure` into `tests/test_main.py`. |
| **3. Build Failure** | `build-container` | `scenarios/trigger-build-failure.sh` | Adds an invalid `COPY missing_asset_configuration.json` instruction to `Dockerfile`. |
| **4. Deploy Failure** | `deploy-cloudrun` | `scenarios/trigger-deploy-failure.sh` | Sets `DEMO_ALLOCATE_OOM=true` and limits memory to 128Mi in `scripts/deploy.sh` causing exit code 137. |

### Restoring Clean State
To revert all injected faults and return the repository to a clean state:
```bash
./scenarios/reset-scenarios.sh
```

---

## Standalone Repository Seeding

To seed a separate standalone GitHub repository for live demo runs (e.g. `ssvaidyanathan/gdi-sample-pipeline-app`):
```bash
./seed-demo-repo.sh /path/to/target-repo
```
