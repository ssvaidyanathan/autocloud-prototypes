#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-agent to Cloud Run with GCS Volume Mount
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
SERVICE_NAME="${NAME_PREFIX}-agent"
IMAGE_TAG="us-west1-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${SERVICE_NAME}:latest"
AGENT_SA="${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
INCIDENTS_BUCKET="${PROJECT_ID}-${NAME_PREFIX}-incidents"

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# 1. Build and push container image using Cloud Build
echo "Building container image ${IMAGE_TAG} via Cloud Build..."
gcloud builds submit "${AGENT_DIR}" \
    --project="${PROJECT_ID}" \
    --tag="${IMAGE_TAG}"

BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || echo "https://${NAME_PREFIX}-bridge-4klvlqnmpa-uw.a.run.app")"
AGENT_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || echo "https://${SERVICE_NAME}-4klvlqnmpa-uw.a.run.app")"

# 2. Deploy to Cloud Run with GCS Volume Mount at /knowledge/incident-manager
echo "Deploying Cloud Run service ${SERVICE_NAME} with GCS volume mount..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE_TAG}" \
    --service-account="${AGENT_SA}" \
    --no-allow-unauthenticated \
    --execution-environment=gen2 \
    --add-volume="name=incidents-volume,type=cloud-storage,bucket=${INCIDENTS_BUCKET},mount-options=only-dir=knowledge/incident-manager" \
    --add-volume-mount="volume=incidents-volume,mount-path=/knowledge/incident-manager" \
    --set-labels="app=alert-triage-agent,component=ata-agent,role=triage-system,tier=platform,env=demo,managed-by=script,owner=sre-prototypes" \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},INCIDENTS_BUCKET=${INCIDENTS_BUCKET},INCIDENT_STORE_PATH=/knowledge/incident-manager,BRIDGE_URL=${BRIDGE_URL},AGENT_URL=${AGENT_URL}"

# 3. Ensure IAM run.invoker is granted only to bridge-sa and authorized caller SAs
echo "Ensuring IAM run.invoker is restricted to authorized service accounts..."
gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${NAME_PREFIX}-bridge-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role="roles/run.invoker" \
    --quiet

gcloud run services add-iam-policy-binding "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --member="serviceAccount:${AGENT_SA}" \
    --role="roles/run.invoker" \
    --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
