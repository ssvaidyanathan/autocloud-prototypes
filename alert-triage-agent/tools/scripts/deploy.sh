#!/usr/bin/env bash
# ==============================================================================
# Build and deploy <NAME_PREFIX>-tools to Cloud Run.
#
# Usage: tools/scripts/deploy.sh [--yes]
#
# Required: PROJECT_ID (or GCP_PROJECT), REGION (or GCP_REGION), NAME_PREFIX, APIGEE_ENV.
# Optional:
#   APIGEE_ORG            Apigee organization (default: PROJECT_ID, echoed)
#   TOOLS_IMAGE           prebuilt image of this stack's repo (skips the build)
#   ENVIRONMENT, OWNER    labels (default demo / sre-prototypes, echoed)
#   PROJECT_NUMBER        default: gcloud projects describe
#   ALLOWED_SERVICE_ACCOUNTS  callers (comma-separated); passed only when exported (the app
#                         default is ${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com)
# Auth env always set: ENFORCE_AUTH=true, VERIFY_OIDC=true, IS_LOCAL=false, PROJECT_NUMBER
# (the app also accepts the deterministic URL), EXPECTED_AUDIENCE = the service's own URL,
# the same URL the agent deploy resolves as TOOLS_URL (status.url, else the deterministic
# URL until the first deploy gives it one).
# The agent SA's run.invoker on the service is in the triage-iam Terraform root.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TOOLS_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=SCRIPTDIR/../../scripts/cloud-run-deploy-lib.sh
source "${TOOLS_DIR}/../scripts/cloud-run-deploy-lib.sh"

for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=true ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "Unknown argument: ${arg} (use --yes)" >&2; exit 1 ;;
    esac
done

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
require_project_and_region || exit 1
require_name_prefix || exit 1
require_vars APIGEE_ENV || exit 1
SERVICE_NAME="${NAME_PREFIX}-tools"
# The tools service runs as the agent SA (no separate tools SA in foundation).
TOOLS_SA="${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"
if [ -z "${APIGEE_ORG:-}" ]; then
    APIGEE_ORG="${PROJECT_ID}"
    echo "NOTE: APIGEE_ORG not set; using PROJECT_ID as the Apigee organization: ${APIGEE_ORG}"
fi
PROJECT_NUMBER="$(resolve_project_number)"
# The service's own URL is the ID-token audience the agent mints for (agent TOOLS_URL).
EXPECTED_AUDIENCE="$(service_url_or_deterministic "${SERVICE_NAME}")"

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"
echo "  Apigee: org=${APIGEE_ORG} env=${APIGEE_ENV} | Labels: env=${ENVIRONMENT} owner=${OWNER}"
echo "==================================================================="
confirm_or_abort || exit 1

# 1. Image: TOOLS_IMAGE when given, else build with Cloud Build (build SA, by digest).
IMAGE="$(resolve_service_image "${TOOLS_IMAGE:-}" "${TOOLS_DIR}" "${SERVICE_NAME}")"

ENV_VARS="^|^PROJECT_ID=${PROJECT_ID}|PROJECT_NUMBER=${PROJECT_NUMBER}|NAME_PREFIX=${NAME_PREFIX}|REGION=${REGION}|APIGEE_ORG=${APIGEE_ORG}|APIGEE_ENV=${APIGEE_ENV}|EXPECTED_AUDIENCE=${EXPECTED_AUDIENCE}|ENFORCE_AUTH=true|VERIFY_OIDC=true|IS_LOCAL=false"
if [ -n "${ALLOWED_SERVICE_ACCOUNTS:-}" ]; then
    ENV_VARS="${ENV_VARS}|ALLOWED_SERVICE_ACCOUNTS=${ALLOWED_SERVICE_ACCOUNTS}"
fi

# 2. Deploy to Cloud Run
# --update-env-vars changes only the variables listed here and keeps every other variable
# already on the service. --set-env-vars would replace the whole list. IS_LOCAL=false is
# always asserted so a stray IS_LOCAL=true cannot persist across redeploys.
echo "Deploying Cloud Run service ${SERVICE_NAME}..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${TOOLS_SA}" \
    --no-allow-unauthenticated \
    --labels="$(stack_labels "${NAME_PREFIX}-tools" triage-system platform)" \
    --update-env-vars="${ENV_VARS}"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
if [ -n "${SERVICE_URL}" ] && [ "${SERVICE_URL}" != "${EXPECTED_AUDIENCE}" ]; then
    echo "Setting EXPECTED_AUDIENCE=${SERVICE_URL} (the service's status.url; it was ${EXPECTED_AUDIENCE})..."
    gcloud run services update "${SERVICE_NAME}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --update-env-vars="EXPECTED_AUDIENCE=${SERVICE_URL}"
fi
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
