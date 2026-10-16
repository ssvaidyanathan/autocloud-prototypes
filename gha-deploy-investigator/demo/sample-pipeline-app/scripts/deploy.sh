#!/usr/bin/env bash
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-sample-pipeline-app}"
REGION="${REGION:-us-central1}"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_NAME="${IMAGE_NAME:-gcr.io/${PROJECT_ID:-my-project}/${SERVICE_NAME}:${IMAGE_TAG}}"
MEMORY="${MEMORY:-512Mi}"
CPU="${CPU:-1}"
EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"

echo "=================================================="
echo "Deploying ${SERVICE_NAME} to Google Cloud Run"
echo "Region:      ${REGION}"
echo "Image:       ${IMAGE_NAME}"
echo "Memory:      ${MEMORY}"
echo "=================================================="

ENV_FLAGS=""
if [[ -n "${EXTRA_ENV_VARS}" ]]; then
    ENV_FLAGS="--set-env-vars ${EXTRA_ENV_VARS}"
fi

# In dry-run or local mode, simulate deployment
if [[ "${DRY_RUN:-false}" == "true" || "${APP_ENV:-}" == "local" || "${CI_SIMULATION:-false}" == "true" ]]; then
    echo "[SIMULATION] Running deploy verification for ${SERVICE_NAME}..."
    if [[ "${EXTRA_ENV_VARS}" =~ "DEMO_ALLOCATE_OOM=true" ]]; then
        echo "Deploying revision ${SERVICE_NAME}-00004-oom..."
        echo "ERROR: (gcloud.run.deploy) Container failed to start and listen on port 8080." >&2
        echo "Revision '${SERVICE_NAME}-00004-oom' failed to become ready: Container terminated due to memory limit (exit code 137)." >&2
        exit 1
    fi
    echo "Service ${SERVICE_NAME} successfully deployed to https://${SERVICE_NAME}-${REGION}.a.run.app"
    exit 0
fi

# Live gcloud deployment command
gcloud run deploy "${SERVICE_NAME}" \
    --image="${IMAGE_NAME}" \
    --region="${REGION}" \
    --platform=managed \
    --allow-unauthenticated \
    --memory="${MEMORY}" \
    --cpu="${CPU}" \
    ${ENV_FLAGS}
