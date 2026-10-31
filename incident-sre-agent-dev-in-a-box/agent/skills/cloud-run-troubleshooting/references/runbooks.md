# Cloud Run Troubleshooting Runbooks

## Runbook 1: Container Failed to Start and Listen on the Specified Port
- **Symptoms**: Deploy fails or revisions trigger 503 errors with `The user-provided container failed to start and listen on the port defined by the PORT environment variable`.
- **Diagnostic Steps**:
  1. Inspect container logs during startup: `severity >= DEFAULT` at revision creation timestamp.
  2. Check if the process crashed on missing environment variables or fatal import errors.
  3. Verify application binds to `0.0.0.0`, not `127.0.0.1`.

## Runbook 2: Memory Limit Exceeded (OOM)
- **Symptoms**: 500 errors with log message: `Memory limit of 512 MiB exceeded with 514 MiB used. Consider increasing the memory limit`.
- **Mitigation**:
  1. Increase memory limit in revision template (e.g. 512Mi -> 1Gi or 2Gi).
  2. Investigate memory leaks or large payload buffering in application code.

## Runbook 3: Concurrency & Max Instances Saturation
- **Symptoms**: `POST /...` requests return 429 or 504 gateway timeout while instance count is at `maxScale`.
- **Mitigation**:
  1. Increase max instances limit or adjust container request concurrency.
