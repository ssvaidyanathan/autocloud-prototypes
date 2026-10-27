#!/usr/bin/env bash
# ==============================================================================
# Build and deploy <NAME_PREFIX>-bridge to Cloud Run.
#
# Usage: bridge/scripts/deploy.sh [--yes]
#
# Required: PROJECT_ID (or GCP_PROJECT), REGION (or GCP_REGION), NAME_PREFIX,
#   SERVICENOW_INSTANCE_URL (or a deployed bridge whose value is kept, with a log line).
# Optional:
#   BRIDGE_IMAGE          prebuilt image of this stack's repo (skips the build)
#   ENVIRONMENT, OWNER    labels (default demo / sre-prototypes, echoed)
#   AGENT_URL, BRIDGE_URL default: status.url, else the deterministic URL; an exported
#                         BRIDGE_URL is never replaced after the deploy
#   VERIFIER_SA           extra /instructions/* callers (comma-separated emails); unset keeps
#                         the current value; VERIFIER_SA=none removes it
#   PROJECT_NUMBER        default: gcloud projects describe
# Run scripts/set-secrets.sh BEFORE the first deploy: secrets resolve at instance start.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=SCRIPTDIR/../../scripts/cloud-run-deploy-lib.sh
source "${BRIDGE_DIR}/../scripts/cloud-run-deploy-lib.sh"

for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=true ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) echo "Unknown argument: ${arg} (use --yes)" >&2; exit 1 ;;
    esac
done

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
require_project_and_region || exit 1
require_name_prefix || exit 1
SERVICE_NAME="${NAME_PREFIX}-bridge"
BRIDGE_SA="${NAME_PREFIX}-bridge-sa@${PROJECT_ID}.iam.gserviceaccount.com"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"

# ServiceNow instance URL: SERVICENOW_INSTANCE_URL when exported; else the value on the
# deployed service (said in a log line); else stop. No file is parsed and a failed gcloud call
# stops the deploy instead of being read as "no value".
if [ -n "${SERVICENOW_INSTANCE_URL:-}" ]; then
    echo "SERVICENOW_INSTANCE_URL from the environment: ${SERVICENOW_INSTANCE_URL}"
else
    EXISTING_BRIDGE_URL="$(existing_service_url "${SERVICE_NAME}")"
    if [ -n "${EXISTING_BRIDGE_URL}" ]; then
        SERVICE_JSON="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format=json)"
        SERVICENOW_INSTANCE_URL="$(printf '%s' "${SERVICE_JSON}" | python3 -c '
import json, sys
svc = json.load(sys.stdin)
for c in svc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
    for e in c.get("env", []) or []:
        if e.get("name") == "SERVICENOW_INSTANCE_URL" and e.get("value"):
            print(e["value"])
            sys.exit(0)
')"
        if [ -n "${SERVICENOW_INSTANCE_URL}" ]; then
            echo "SERVICENOW_INSTANCE_URL not exported: keeping the value on the deployed ${SERVICE_NAME}: ${SERVICENOW_INSTANCE_URL}"
        fi
    fi
fi
if [ -z "${SERVICENOW_INSTANCE_URL:-}" ]; then
    echo "ERROR: SERVICENOW_INSTANCE_URL is required (e.g. https://dev12345.service-now.com): export it; ${SERVICE_NAME} is not deployed with one to keep." >&2
    exit 1
fi
case "${SERVICENOW_INSTANCE_URL}" in
    https://*) ;;
    *) echo "ERROR: SERVICENOW_INSTANCE_URL must be an https:// URL, got '${SERVICENOW_INSTANCE_URL}'." >&2; exit 1 ;;
esac

# Numeric GCP project number (env PROJECT_NUMBER, else `gcloud projects describe`): the bridge
# accepts https://<prefix>-bridge-<number>.<region>.run.app as an ID-token audience and refuses
# to start without it. Exits with "PROJECT_NUMBER is required" when it cannot be resolved.
PROJECT_NUMBER="$(resolve_project_number)"

# URLs: an exported value wins; else the service's status.url; else (not deployed yet) its
# deterministic URL. A failed lookup stops the deploy; no URL is hardcoded.
if [ -z "${AGENT_URL:-}" ]; then
    AGENT_URL="$(service_url_or_deterministic "${NAME_PREFIX}-agent")"
fi
# The bridge's own status.url (the agent mints its ID tokens for it). Required by the bridge.
# On a first deploy the service has no status.url yet: start with the deterministic URL and
# set the real one after the deploy (below).
# The post-deploy update below runs only for a derived value: an exported BRIDGE_URL (e.g. a
# custom domain the agent calls) is never replaced.
BRIDGE_URL_DERIVED=false
if [ -z "${BRIDGE_URL:-}" ]; then
    BRIDGE_URL="$(service_url_or_deterministic "${SERVICE_NAME}")"
    BRIDGE_URL_DERIVED=true
fi

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"
echo "  Labels: env=${ENVIRONMENT} owner=${OWNER}"
echo "  SERVICENOW_INSTANCE_URL=${SERVICENOW_INSTANCE_URL} AGENT_URL=${AGENT_URL} BRIDGE_URL=${BRIDGE_URL}"
echo "==================================================================="
confirm_or_abort || exit 1

# 1. Image: BRIDGE_IMAGE when given, else build with Cloud Build (build SA, by digest).
IMAGE="$(resolve_service_image "${BRIDGE_IMAGE:-}" "${BRIDGE_DIR}" "${SERVICE_NAME}")"

# 2. Deploy to Cloud Run
# A single bridge instance is sufficient for this prototype; the bridge holds no grouping state (grouping, timers and suppression live in the agent).
echo "Deploying Cloud Run service ${SERVICE_NAME}..."
# --update-env-vars changes only the variables listed here and keeps every other variable
# already on the service (e.g. VERIFIER_SA when it is not exported). --set-env-vars would
# replace the whole list. The list uses "|" as the delimiter (gcloud's ^|^ prefix), because
# VERIFIER_SA is a comma-separated list. IS_LOCAL=false is always asserted: IS_LOCAL=true
# makes the bridge accept static test tokens, and --update-env-vars would keep a stray one.
# VERIFIER_SA=none removes the variable from the service (the only way to revoke it, since
# an unset VERIFIER_SA keeps the current value).
ENV_VARS="^|^PROJECT_ID=${PROJECT_ID}|PROJECT_NUMBER=${PROJECT_NUMBER}|NAME_PREFIX=${NAME_PREFIX}|REGION=${REGION}|INCIDENTS_BUCKET=${PROJECT_ID}-${NAME_PREFIX}-incidents|TASKS_QUEUE=${NAME_PREFIX}-triage-queue|AGENT_URL=${AGENT_URL}|BRIDGE_URL=${BRIDGE_URL}|SERVICENOW_INSTANCE_URL=${SERVICENOW_INSTANCE_URL}|IS_LOCAL=false"
REMOVE_ENV_ARGS=()
case "${VERIFIER_SA:-}" in
    "") ;;
    none)
        echo "VERIFIER_SA=none: removing VERIFIER_SA from ${SERVICE_NAME}."
        REMOVE_ENV_ARGS=(--remove-env-vars=VERIFIER_SA)
        ;;
    *) ENV_VARS="${ENV_VARS}|VERIFIER_SA=${VERIFIER_SA}" ;;
esac

gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${IMAGE}" \
    --service-account="${BRIDGE_SA}" \
    --allow-unauthenticated \
    --max-instances=1 \
    --labels="$(stack_labels "${NAME_PREFIX}-bridge" triage-system platform)" \
    --update-env-vars="${ENV_VARS}" \
    ${REMOVE_ENV_ARGS[@]+"${REMOVE_ENV_ARGS[@]}"} \
    --update-secrets="WEBHOOK_TOKEN=${NAME_PREFIX}-webhook-token:latest,SERVICENOW_USERNAME=${NAME_PREFIX}-servicenow-username:latest,SERVICENOW_PASSWORD=${NAME_PREFIX}-servicenow-password:latest"

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
DETERMINISTIC_URL="https://${SERVICE_NAME}-${PROJECT_NUMBER}.${REGION}.run.app"
if [ "${BRIDGE_URL_DERIVED}" = "true" ] && [ "${SERVICE_URL}" != "${BRIDGE_URL}" ] && [ "${SERVICE_URL}" != "${DETERMINISTIC_URL}" ]; then
    echo "Setting BRIDGE_URL=${SERVICE_URL} (the service's status.url; it was ${BRIDGE_URL})..."
    gcloud run services update "${SERVICE_NAME}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --update-env-vars="BRIDGE_URL=${SERVICE_URL}"
fi
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL}"
