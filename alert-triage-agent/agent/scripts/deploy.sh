#!/usr/bin/env bash
# ==============================================================================
# Build and deploy <NAME_PREFIX>-agent to Cloud Run with its GCS volume mount.
#
# Usage: agent/scripts/deploy.sh [--yes]
#
# Required: PROJECT_ID (or GCP_PROJECT), REGION (or GCP_REGION), NAME_PREFIX.
# Optional:
#   AGENT_IMAGE               prebuilt image of this stack's repo (skips the build)
#   ENVIRONMENT, OWNER        labels (default demo / sre-prototypes, echoed)
#   BRIDGE_URL, AGENT_URL, TOOLS_URL
#                             default: the service's status.url, else its deterministic URL
#   AUTO_RESOLVE_ON_RECOVERY  true|false; MODEL_NAME; GEMINI_LOCATION; TOOLS_TIMEOUT_SECONDS;
#   TOOLS_RETRY_ATTEMPTS; TOOLS_RETRY_BASE_SECONDS; CHANGE_ONSET_WINDOW_MINUTES;
#   CHANGE_HEALTHY_PERIOD_MINUTES; CHANGE_HEALTHY_MAX_5XX_SHARE; CHANGE_HEALTHY_MAX_LATENCY_MS
#                             passed only when exported (else the service keeps its value,
#                             or the app default applies)
#   IMAGE_TAG                 tag of the build (default: git commit)
# The run.invoker bindings of the agent are in the triage-iam Terraform root.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=SCRIPTDIR/../../scripts/cloud-run-deploy-lib.sh
source "${AGENT_DIR}/../scripts/cloud-run-deploy-lib.sh"

for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=true ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        *) echo "Unknown argument: ${arg} (use --yes)" >&2; exit 1 ;;
    esac
done

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
require_project_and_region || exit 1
require_name_prefix || exit 1
SERVICE_NAME="${NAME_PREFIX}-agent"
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

# URLs: an exported value wins; else the service's status.url; else (not deployed yet) its
# deterministic URL https://<service>-<project number>.<region>.run.app. A failed lookup stops
# the deploy. BRIDGE_URL is the ID-token audience the agent uses for the bridge, which accepts
# its own status.url (bridge BRIDGE_URL) and its deterministic URL (bridge PROJECT_NUMBER).
# TOOLS_URL likewise for <prefix>-tools (EXPECTED_AUDIENCE plus its deterministic URL).
if [ -z "${BRIDGE_URL:-}" ]; then
    BRIDGE_URL="$(service_url_or_deterministic "${NAME_PREFIX}-bridge")"
fi
if [ -z "${AGENT_URL:-}" ]; then
    AGENT_URL="$(service_url_or_deterministic "${SERVICE_NAME}")"
fi
if [ -z "${TOOLS_URL:-}" ]; then
    TOOLS_URL="$(service_url_or_deterministic "${NAME_PREFIX}-tools")"
fi
# Exported URLs from another stack's deploy in the same shell are refused (sec L9).
assert_stack_url BRIDGE_URL bridge || exit 1
assert_stack_url AGENT_URL agent || exit 1
assert_stack_url TOOLS_URL tools || exit 1

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"
echo "  Labels: env=${ENVIRONMENT} owner=${OWNER}"
echo "  BRIDGE_URL=${BRIDGE_URL} TOOLS_URL=${TOOLS_URL}"
echo "==================================================================="
confirm_or_abort || exit 1

# 1. Image: AGENT_IMAGE when given, else build with Cloud Build (build SA, by digest).
IMAGE="$(resolve_service_image "${AGENT_IMAGE:-}" "${AGENT_DIR}" "${SERVICE_NAME}")"

# --update-env-vars changes only the variables listed here and keeps every other variable
# already on the service. --set-env-vars would replace the whole list. The timer queue
# (foundation: ${NAME_PREFIX}-timer-queue in var.region) and the timer tasks' OIDC service
# account are set explicitly: the app defaults name one particular project and region.
# IS_LOCAL=false is always asserted: a stray IS_LOCAL=true would otherwise persist.
ENV_VARS="PROJECT_ID=${PROJECT_ID},NAME_PREFIX=${NAME_PREFIX},REGION=${REGION},INCIDENTS_BUCKET=${INCIDENTS_BUCKET},INCIDENT_STORE_PATH=/knowledge/incident-manager,BRIDGE_URL=${BRIDGE_URL},AGENT_URL=${AGENT_URL},TOOLS_URL=${TOOLS_URL},LOCATION=${REGION},TIMER_QUEUE_NAME=${NAME_PREFIX}-timer-queue,SERVICE_ACCOUNT_EMAIL=${AGENT_SA},IS_LOCAL=false"
if [ -n "${AUTO_RESOLVE_ON_RECOVERY:-}" ]; then
    ENV_VARS="${ENV_VARS},AUTO_RESOLVE_ON_RECOVERY=${AUTO_RESOLVE_ON_RECOVERY}"
fi
# Optional tuning, passed only when exported (else the service keeps its value or the app
# default applies).
for optional in MODEL_NAME GEMINI_LOCATION GEMINI_TIMEOUT_SECONDS TOOLS_TIMEOUT_SECONDS TOOLS_RETRY_ATTEMPTS TOOLS_RETRY_BASE_SECONDS \
        CHANGE_ONSET_WINDOW_MINUTES CHANGE_HEALTHY_PERIOD_MINUTES CHANGE_HEALTHY_MAX_5XX_SHARE CHANGE_HEALTHY_MAX_LATENCY_MS; do
    if [ -n "${!optional:-}" ]; then
        ENV_VARS="${ENV_VARS},${optional}=${!optional}"
    fi
done

# 2. Deploy to Cloud Run with GCS Volume Mount at /knowledge/incident-manager
echo "Deploying Cloud Run service ${SERVICE_NAME} with GCS volume mount..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${AGENT_SA}" \
    --no-allow-unauthenticated \
    --execution-environment=gen2 \
    --add-volume="name=incidents-volume,type=cloud-storage,bucket=${INCIDENTS_BUCKET},mount-options=only-dir=knowledge/incident-manager" \
    --add-volume-mount="volume=incidents-volume,mount-path=/knowledge/incident-manager" \
    --labels="$(stack_labels "${NAME_PREFIX}-agent" triage-system platform)" \
    --update-env-vars="${ENV_VARS}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
echo "run.invoker on ${SERVICE_NAME} (bridge SA, agent SA) comes from the triage-iam Terraform root."
