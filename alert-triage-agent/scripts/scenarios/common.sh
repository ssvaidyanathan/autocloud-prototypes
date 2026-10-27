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
# PINNED_INVENTORY_REVISION must be a revision whose image is PINNED_INVENTORY_DIGEST
# and whose config is the healthy baseline (correct DB user, no DEPLOY_TAG).
# Older revisions run code where GET on /admin/* performs the fault action.
export PINNED_INVENTORY_REVISION="${PINNED_INVENTORY_REVISION:-ata-inventory-api-00073-9vb}"

# Fail loudly unless PINNED_INVENTORY_REVISION runs PINNED_INVENTORY_DIGEST.
verify_pinned_inventory_revision() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi
    local actual
    actual="$(gcloud run revisions describe "${PINNED_INVENTORY_REVISION}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format="value(status.imageDigest)" 2>/dev/null || true)"
    actual="${actual##*@}"
    if [ "${actual}" != "${PINNED_INVENTORY_DIGEST}" ]; then
        log_error "Pinned revision ${PINNED_INVENTORY_REVISION} runs '${actual:-<not found>}', expected ${PINNED_INVENTORY_DIGEST}." >&2
        log_error "Fix PINNED_INVENTORY_REVISION/PINNED_INVENTORY_DIGEST in scripts/scenarios/common.sh before running scenarios." >&2
        return 1
    fi
}

# Fail loudly unless ata-inventory-api serves 100% from PINNED_INVENTORY_REVISION.
verify_inventory_serving_pinned() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi
    local traffic
    traffic="$(gcloud run services describe "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format="csv[no-heading](status.traffic.revisionName,status.traffic.percent)" 2>/dev/null || true)"
    if [ "${traffic}" != "${PINNED_INVENTORY_REVISION},100" ]; then
        log_error "${NAME_PREFIX}-inventory-api traffic is '${traffic:-<unknown>}', expected ${PINNED_INVENTORY_REVISION}=100." >&2
        return 1
    fi
}

# Duration Parser (supports Xm, Xs, or raw seconds)
parse_duration() {
    local val="$1"
    if [[ "${val}" =~ ^([0-9]+)[mM]$ ]]; then
        echo $((${BASH_REMATCH[1]} * 60))
    elif [[ "${val}" =~ ^([0-9]+)[sS]?$ ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "${val}"
    fi
}

# Resolve API Key
resolve_api_key() {
    if [ -n "${ATA_API_KEY:-}" ]; then
        echo "${ATA_API_KEY}"
        return 0
    fi

    local key_file="${SCRIPT_DIR}/../../.ata-api-key"
    local repo_key_file="${SCRIPT_DIR}/../../../.ata-api-key"
    local pwd_key_file="${PWD}/.ata-api-key"

    if [ -f "${pwd_key_file}" ]; then
        cat "${pwd_key_file}"
        return 0
    elif [ -f "${key_file}" ]; then
        cat "${key_file}"
        return 0
    elif [ -f "${repo_key_file}" ]; then
        cat "${repo_key_file}"
        return 0
    fi

    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-apigee-api-key"
        return 0
    fi

    log_error "Apigee consumer API key not found." >&2
    log_error "Please set the ATA_API_KEY environment variable or write the key to .ata-api-key." >&2
    log_error "To retrieve your consumer key using your own gcloud credentials, run:" >&2
    log_error "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\" >&2
    log_error "    \"https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}/developers/ata-dev@example.com/apps/ata-demo-app\" \\" >&2
    log_error "    | python3 -c \"import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])\" > .ata-api-key" >&2
    return 1
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
# Note: For user credentials (gcloud auth login), custom audiences are unsupported;
# falls back to gcloud auth print-identity-token without audiences. Never mints tokens for service accounts.
resolve_id_token() {
    local aud="${1:-}"
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-id-token"
        return 0
    fi

    local token=""
    if [ -n "${aud}" ]; then
        token="$(gcloud auth print-identity-token --audiences="${aud}" 2>/dev/null || true)"
    fi
    if [ -z "${token}" ]; then
        token="$(gcloud auth print-identity-token 2>/dev/null || true)"
    fi

    if [ -z "${token}" ]; then
        log_warn "Could not resolve identity token via 'gcloud auth print-identity-token'."
        log_warn "Ensure you are authenticated ('gcloud auth login') and have 'roles/run.invoker' on Cloud Run demo services."
    fi

    echo "${token}"
}

# Verify authorization for Cloud Run response codes
check_cloud_run_auth() {
    local status="$1"
    local target="$2"
    if [ "${status}" = "401" ] || [ "${status}" = "403" ]; then
        log_error "Unauthorized (HTTP ${status}) accessing ${target}."
        log_error "Ensure your user account ('gcloud auth login') has the 'roles/run.invoker' (Cloud Run Invoker) role on the demo services in project ${PROJECT_ID}."
        return 1
    fi
    return 0
}

# Resolve Apigee Management Token
resolve_apigee_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-apigee-token"
        return 0
    fi
    gcloud auth print-access-token 2>/dev/null || echo ""
}

# Resolve Cloud Monitoring Access Token
resolve_monitoring_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-monitoring-token"
        return 0
    fi
    gcloud auth print-access-token 2>/dev/null || echo ""
}
