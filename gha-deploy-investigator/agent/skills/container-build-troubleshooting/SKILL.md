---
name: container-build-troubleshooting
description: Troubleshooting container build failures, Docker multi-stage errors, and packaging conflicts
---

# Container Build Troubleshooting Runbook

## Diagnostic Workflow
1. **Identify Failure Class**:
   - `COPY / ADD` file not found: missing build artifact, typo in path, or file excluded by `.dockerignore`.
   - Dependency resolution conflict: `ResolutionImpossible` or conflicting package specifications.
   - Non-zero command exit during `RUN`: missing system library, compilation error, or script failure.
   - Registry authentication failure: 401/403 pushing to Artifact Registry or Docker Hub.
2. **Inspect Dockerfile & Manifests**:
   - Query repository `Dockerfile` and package manifests (`requirements.txt`, `pyproject.toml`) via `get_file_content`.
3. **Formulate Recommended Remediation**:
   - Verify build context and `.dockerignore` patterns.
   - Run dependency lock updates locally (`uv lock`, `poetry lock`).
   - Validate image tag and registry push permissions.
