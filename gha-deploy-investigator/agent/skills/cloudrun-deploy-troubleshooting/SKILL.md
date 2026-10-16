---
name: cloudrun-deploy-troubleshooting
description: Troubleshooting Cloud Run deployment rollouts, startup probe failures, and container crashes
---

# Cloud Run Deploy Troubleshooting Runbook

## Diagnostic Workflow
1. **Extract Deployment Coordinates**:
   - Cloud Run service name
   - Target revision name
   - Target GCP region
2. **Inspect Revision Status Conditions**:
   - Query `describe_cloud_run_revision` to check `Ready` condition and container exit codes.
   - Exit code `137`: Container terminated due to Out Of Memory (OOM).
   - Exit code `1` or non-zero: Unhandled application exception during startup.
   - Startup probe timeout: Container failed to listen on `$PORT` within timeout limit.
3. **Query Container Stderr Logs**:
   - Query Cloud Logging via `query_cloud_logging` for `resource.type="cloud_run_revision"` and the failing revision name.
   - Capture stack traces, uncaught exceptions, and database connection timeouts.
4. **Formulate Recommended Remediation**:
   - If OOM (exit 137): increase memory limit via `gcloud run services update <service> --memory 1Gi`.
   - If startup crash: fix missing environment variables, secrets, or module imports.
   - If probe timeout: verify app binds to `0.0.0.0:$PORT`.
