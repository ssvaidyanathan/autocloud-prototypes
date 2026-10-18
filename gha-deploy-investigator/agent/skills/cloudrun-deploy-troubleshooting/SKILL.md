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
3. **Correlate with Commit Diff**:
   - Query `get_commit_diff` to correlate deployment failures with triggering commit changes (e.g. `scripts/deploy.sh` resource limits like `--memory`, environment variables, configuration regressions).
4. **Query Container Stderr Logs**:
   - Query Cloud Logging via `query_cloud_logging` using structured parameters (`service_name`, `revision_name`) or valid Cloud Logging filter syntax (`resource.type="cloud_run_revision" resource.labels.service_name="<service>" resource.labels.revision_name="<revision>"`).
   - Capture stack traces, uncaught exceptions, memory limit exhaustion warnings, and container termination signals.
5. **Formulate Recommended Remediation**:
   - If OOM (exit 137): increase memory limit via `gcloud run services update <service> --memory 1Gi`.
   - If startup crash: fix missing environment variables, secrets, or module imports.
   - If probe timeout: verify app binds to `0.0.0.0:$PORT`.
