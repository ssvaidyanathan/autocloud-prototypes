#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-tools to Cloud Run
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=SCRIPTDIR/../../scripts/cloud-run-deploy-lib.sh
source "${TOOLS_DIR}/../scripts/cloud-run-deploy-lib.sh"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
require_project_and_region || exit 1
NAME_PREFIX="${NAME_PREFIX:-ata}"
SERVICE_NAME="${NAME_PREFIX}-tools"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${SERVICE_NAME}:latest"
TOOLS_SA="${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"
# Apigee organization the tools read; the demo org is named after the project.
APIGEE_ORG="${APIGEE_ORG:-${PROJECT_ID}}"

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# 1. Build and push container image using Cloud Build
echo "Syncing single topology package into tools build context..."
rm -rf "${TOOLS_DIR}/topology"
cp -r "${TOOLS_DIR}/../topology" "${TOOLS_DIR}/topology"
trap 'rm -rf "${TOOLS_DIR}/topology"' EXIT

echo "Building container image ${IMAGE_TAG} via Cloud Build..."
gcloud builds submit "${TOOLS_DIR}" \
    --project="${PROJECT_ID}" \
    --tag="${IMAGE_TAG}"

# 2. Deploy to Cloud Run
# --update-env-vars changes only the variables listed here and keeps every other variable
# already on the service. --set-env-vars would replace the whole list.
echo "Deploying Cloud Run service ${SERVICE_NAME}..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE_TAG}" \
    --service-account="${TOOLS_SA}" \
    --no-allow-unauthenticated \
    --labels="app=alert-triage-agent,component=${NAME_PREFIX}-tools,role=triage-system,tier=platform,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}" \
    --update-env-vars="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},APIGEE_ORG=${APIGEE_ORG}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
