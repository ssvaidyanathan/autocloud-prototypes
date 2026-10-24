#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-inventory-api to Cloud Run
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
SERVICE_NAME="${NAME_PREFIX}-inventory-api"
PINNED_DIGEST="${PINNED_INVENTORY_DIGEST:-sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce}"
PINNED_TAG="${PINNED_INVENTORY_TAG:-v1.1.0}"
IMAGE_BASE="us-west1-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${SERVICE_NAME}"
IMAGE_TAG="${IMAGE_BASE}:${PINNED_TAG}"
PINNED_IMAGE="${IMAGE_BASE}@${PINNED_DIGEST}"

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "  Pinned Image: ${PINNED_IMAGE}"
echo "==================================================================="

# 1. Optionally build and push container image using Cloud Build if requested
if [ "${BUILD_IMAGE:-false}" = "true" ]; then
    echo "Building container image ${IMAGE_TAG} via Cloud Build..."
    BUILD_OUT="$(gcloud builds submit "${DEMO_DIR}" \
        --project="${PROJECT_ID}" \
        --tag="${IMAGE_TAG}" \
        --format="value(results.images[0].digest)")"
    if [ -n "${BUILD_OUT}" ]; then
        PINNED_DIGEST="${BUILD_OUT}"
        PINNED_IMAGE="${IMAGE_BASE}@${PINNED_DIGEST}"
        # Tag latest as well
        gcloud artifacts docker tags add "${IMAGE_TAG}" "${IMAGE_BASE}:latest" --project="${PROJECT_ID}" --quiet || true
    fi
fi

# 2. Deploy to Cloud Run using pinned image
echo "Deploying Cloud Run service ${SERVICE_NAME} using pinned image..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${PINNED_IMAGE}" \
    --service-account="${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --set-labels="app=alert-triage-agent,component=ata-inventory-api,role=demo-workload,tier=service,env=demo,managed-by=script,owner=sre-prototypes" \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},ATA_INVENTORY_INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:${NAME_PREFIX}-inventory-db,ATA_INVENTORY_DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,ATA_INVENTORY_DB_NAME=inventory,ATA_INVENTORY_DB_IAM_AUTH=true,ATA_INVENTORY_MOCK_DB=false,DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,MOCK_DB=false" \
    --no-allow-unauthenticated \
    --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
