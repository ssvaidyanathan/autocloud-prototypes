#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy ata-agent to Cloud Run with GCS Volume Mount
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=SCRIPTDIR/../../scripts/cloud-run-deploy-lib.sh
source "${AGENT_DIR}/../scripts/cloud-run-deploy-lib.sh"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
require_project_and_region || exit 1
NAME_PREFIX="${NAME_PREFIX:-ata}"
SERVICE_NAME="${NAME_PREFIX}-agent"
IMAGE_TAG="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${SERVICE_NAME}:latest"
AGENT_SA="${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
INCIDENTS_BUCKET="${PROJECT_ID}-${NAME_PREFIX}-incidents"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"
# Resolve the ServiceNow ticket when monitoring recovers and the cool-down passes (true),
# or leave it In Progress with a verify-and-resolve work note (false). See CONFIGURATION.md §1.2.
# Passed only when set: a redeploy without it keeps the service's current value (the app
# default is false when it was never set).
case "${AUTO_RESOLVE_ON_RECOVERY:-}" in
    ""|true|false) ;;
    *)
        echo "AUTO_RESOLVE_ON_RECOVERY must be 'true' or 'false', got '${AUTO_RESOLVE_ON_RECOVERY}'" >&2
        exit 1
        ;;
esac

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "==================================================================="

# 1. Build and push container image using Cloud Build
echo "Syncing single topology package into agent build context..."
rm -rf "${AGENT_DIR}/topology"
cp -r "${AGENT_DIR}/../topology" "${AGENT_DIR}/topology"
trap 'rm -rf "${AGENT_DIR}/topology"' EXIT

echo "Building container image ${IMAGE_TAG} via Cloud Build..."
gcloud builds submit "${AGENT_DIR}" \
    --project="${PROJECT_ID}" \
    --tag="${IMAGE_TAG}"

# URLs: an exported value wins; else the service's status.url; else (not deployed yet) its
# deterministic URL https://<service>-<project number>.<region>.run.app. A failed lookup stops
# the deploy. BRIDGE_URL is the ID-token audience the agent uses for the bridge, which accepts
# its own status.url (bridge BRIDGE_URL) and its deterministic URL (bridge PROJECT_NUMBER).
if [ -z "${BRIDGE_URL:-}" ]; then
    BRIDGE_URL="$(service_url_or_deterministic "${NAME_PREFIX}-bridge")"
fi
if [ -z "${AGENT_URL:-}" ]; then
    AGENT_URL="$(service_url_or_deterministic "${SERVICE_NAME}")"
fi

# --update-env-vars changes only the variables listed here and keeps every other variable
# already on the service. --set-env-vars would replace the whole list. The timer queue
# (foundation: ${NAME_PREFIX}-timer-queue in var.region) and the timer tasks' OIDC service
# account are set explicitly: the app defaults name one particular project and region.
ENV_VARS="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},INCIDENTS_BUCKET=${INCIDENTS_BUCKET},INCIDENT_STORE_PATH=/knowledge/incident-manager,BRIDGE_URL=${BRIDGE_URL},AGENT_URL=${AGENT_URL},LOCATION=${REGION},TIMER_QUEUE_NAME=${NAME_PREFIX}-timer-queue,SERVICE_ACCOUNT_EMAIL=${AGENT_SA}"
if [ -n "${AUTO_RESOLVE_ON_RECOVERY:-}" ]; then
    ENV_VARS="${ENV_VARS},AUTO_RESOLVE_ON_RECOVERY=${AUTO_RESOLVE_ON_RECOVERY}"
fi

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
    --labels="app=alert-triage-agent,component=${NAME_PREFIX}-agent,role=triage-system,tier=platform,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}" \
    --update-env-vars="${ENV_VARS}"

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
