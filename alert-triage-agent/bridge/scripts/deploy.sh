#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-bridge to Cloud Run
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
SERVICE_NAME="${NAME_PREFIX}-bridge"
IMAGE_TAG="us-west1-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${SERVICE_NAME}:latest"
BRIDGE_SA="${NAME_PREFIX}-bridge-sa@${PROJECT_ID}.iam.gserviceaccount.com"

# Resolve SERVICENOW_INSTANCE_URL if not provided in environment
if [ -z "${SERVICENOW_INSTANCE_URL:-}" ]; then
    if [ -f "${BRIDGE_DIR}/../infra/terraform/foundation/terraform.tfvars" ]; then
        SERVICENOW_INSTANCE_URL=$(grep -E '^\s*servicenow_instance_url\s*=' "${BRIDGE_DIR}/../infra/terraform/foundation/terraform.tfvars" | sed -E 's/.*=\s*"([^"]+)".*/\1/' || true)
    fi
    if [ -z "${SERVICENOW_INSTANCE_URL:-}" ]; then
        SERVICENOW_INSTANCE_URL=$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="yaml(spec.template.spec.containers[0].env)" 2>/dev/null | grep -A1 "name: SERVICENOW_INSTANCE_URL" | grep "value:" | awk '{print $2}' || true)
    fi
fi

if [ -z "${SERVICENOW_INSTANCE_URL:-}" ]; then
    echo "SERVICENOW_INSTANCE_URL is required" >&2
    exit 1
fi

# Resolve AGENT_URL if not provided in environment
if [ -z "${AGENT_URL:-}" ]; then
    AGENT_URL="$(gcloud run services describe "${NAME_PREFIX}-agent" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || echo "https://${NAME_PREFIX}-agent-185099261133.${REGION}.run.app")"
fi

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# 1. Build and push container image using Cloud Build
echo "Building container image ${IMAGE_TAG} via Cloud Build..."
gcloud builds submit "${BRIDGE_DIR}" \
    --project="${PROJECT_ID}" \
    --tag="${IMAGE_TAG}"

# 2. Deploy to Cloud Run
# A single bridge instance is sufficient for this prototype; the bridge holds no grouping state (grouping, timers and suppression live in the agent).
echo "Deploying Cloud Run service ${SERVICE_NAME}..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE_TAG}" \
    --service-account="${BRIDGE_SA}" \
    --allow-unauthenticated \
    --max-instances=1 \
    --set-labels="app=alert-triage-agent,component=ata-bridge,role=triage-system,tier=platform,env=demo,managed-by=script,owner=sre-prototypes" \
    --set-env-vars="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},INCIDENTS_BUCKET=${PROJECT_ID}-${NAME_PREFIX}-incidents,TASKS_QUEUE=${NAME_PREFIX}-triage-queue,AGENT_URL=${AGENT_URL},SERVICENOW_INSTANCE_URL=${SERVICENOW_INSTANCE_URL}" \
    --set-secrets="WEBHOOK_TOKEN=${NAME_PREFIX}-webhook-token:latest,SERVICENOW_USERNAME=${NAME_PREFIX}-servicenow-username:latest,SERVICENOW_PASSWORD=${NAME_PREFIX}-servicenow-password:latest"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
