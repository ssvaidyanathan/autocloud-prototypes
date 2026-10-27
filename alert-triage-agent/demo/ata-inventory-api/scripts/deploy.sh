#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-inventory-api to Cloud Run (initial / stage-1 deploy)
#
# Deploys the pinned image (scripts/scenarios/inventory-pin.env) and routes 100% of
# traffic to the new revision. To move the demo baseline to new code, do NOT use this
# script: use scripts/repin-inventory.sh (no-traffic candidate, verify, then switch;
# DEPLOY.md, "Re-pinning ata-inventory-api").
#
# Env and limits come from scripts/scenarios/common.sh, so this script, Terraform and the
# scenario restore agree: ATA_INVENTORY_* only (no DB_USER/MOCK_DB aliases), at most
# DEMO_MAX_INSTANCES instances (service level and revision level), min instances 0.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEMO_DIR}/../.." && pwd)"

# PROJECT_ID, REGION, NAME_PREFIX, the pin (PINNED_INVENTORY_DIGEST), DEMO_MAX_INSTANCES,
# INVENTORY_BASELINE_ENV_VARS. Fails loudly on an invalid pin file or cap.
# shellcheck source=scripts/scenarios/common.sh
source "${REPO_ROOT}/scripts/scenarios/common.sh"

SERVICE_NAME="${NAME_PREFIX}-inventory-api"
PINNED_DIGEST="${PINNED_INVENTORY_DIGEST}"
PINNED_TAG="${PINNED_INVENTORY_TAG:-v1.1.0}"
IMAGE_BASE="${PINNED_INVENTORY_IMAGE_BASE}"
IMAGE_TAG="${IMAGE_BASE}:${PINNED_TAG}"
PINNED_IMAGE="${IMAGE_BASE}@${PINNED_DIGEST}"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "  Pinned Image: ${PINNED_IMAGE}"
echo "  Max instances: ${DEMO_MAX_INSTANCES} (service and revision) | Min instances: 0"
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
    --labels="app=alert-triage-agent,component=${NAME_PREFIX}-inventory-api,role=demo-workload,tier=service,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}" \
    --set-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
    --max="${DEMO_MAX_INSTANCES}" \
    --max-instances="${DEMO_MAX_INSTANCES}" \
    --min-instances=0 \
    --no-allow-unauthenticated \
    --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
