#!/usr/bin/env bash
# ==============================================================================
# Common Helper Functions & Environment Setup for Demo Scenarios (S1-S8)
# ==============================================================================

set -euo pipefail

# Project & Regional Defaults
export PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
export REGION="${REGION:-${GCP_REGION:-us-west1}}"
export NAME_PREFIX="${NAME_PREFIX:-ata}"
export ENV_NAME="${ENV_NAME:-prod}"
export APIGEE_HOST="${APIGEE_HOST:-8.233.247.52.nip.io}"
export DRY_RUN="${DRY_RUN:-false}"
export MOCK_MODE="${MOCK_MODE:-false}"

# Incident bucket
export INCIDENTS_BUCKET="${INCIDENTS_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-incidents}"

# Logging helpers
log_info() {
    echo -e "\033[1;34m[INFO]\033[0m $*"
}

log_success() {
    echo -e "\033[1;32m[SUCCESS]\033[0m $*"
}

log_warn() {
    echo -e "\033[1;33m[WARN]\033[0m $*"
}

log_error() {
    echo -e "\033[1;31m[ERROR]\033[0m $*"
}

# Deterministic Pinned Demo Images and Revisions
export PINNED_INVENTORY_DIGEST="${PINNED_INVENTORY_DIGEST:-sha256:24b07eaa50db6d226889607116f0dd43693df5e1f597867a79e0f86e3d7767ce}"
export PINNED_INVENTORY_IMAGE="${PINNED_INVENTORY_IMAGE:-us-west1-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${NAME_PREFIX}-inventory-api@${PINNED_INVENTORY_DIGEST}}"
export PINNED_INVENTORY_REVISION="${PINNED_INVENTORY_REVISION:-ata-inventory-api-00070-6nq}"

# Resolve API Key
resolve_api_key() {
    local key_file="${SCRIPT_DIR}/../../.ata-api-key"
    local alt_key_file="/workspace/alert-triage-agent/.ata-api-key"
    if [ -n "${ATA_API_KEY:-}" ]; then
        echo "${ATA_API_KEY}"
    elif [ -f "${key_file}" ]; then
        cat "${key_file}"
    elif [ -f "${alt_key_file}" ]; then
        cat "${alt_key_file}"
    else
        echo ""
    fi
}

# Resolve Cloud Run Service URLs
resolve_service_url() {
    local service_name="$1"
    local default_url="$2"

    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "${default_url}"
        return 0
    fi

    local discovered_url=""
    discovered_url="$(gcloud run services describe "${service_name}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || true)"
    if [ -n "${discovered_url}" ]; then
        echo "${discovered_url}"
    else
        echo "${default_url}"
    fi
}

# Resolve Bearer Token for Cloud Run IAM Invocation
resolve_id_token() {
    local aud="${1:-}"
    if [ -n "${aud}" ]; then
        gcloud auth print-identity-token --audiences="${aud}" 2>/dev/null || echo ""
        return 0
    fi
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-id-token"
        return 0
    fi
    gcloud auth print-identity-token 2>/dev/null || echo ""
}

# Resolve Apigee Management Token
resolve_apigee_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-apigee-token"
        return 0
    fi
    gcloud auth print-access-token 2>/dev/null || echo ""
}
