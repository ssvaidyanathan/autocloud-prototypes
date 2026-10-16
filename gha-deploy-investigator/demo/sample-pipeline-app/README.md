# Sample Pipeline App

A production-grade FastAPI microservice demonstrating multi-stage GitHub Actions CI/CD pipeline failure investigation by the `gha-deploy-investigator` autonomous agent.

## Architecture & Features

- **Framework**: FastAPI with Pydantic v2 configuration and schema validation.
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

## Testing & Scenarios

Failure injection scenarios are located outside this application directory in `demo/scenarios/` to preserve a clean, realistic production repository without leaked testing artifacts:

- See `demo/SCENARIOS.md` in the parent directory for complete details on triggering and resetting multi-stage pipeline failure scenarios.
