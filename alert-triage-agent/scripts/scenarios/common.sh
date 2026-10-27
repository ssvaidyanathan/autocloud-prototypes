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

# Logging helpers. printf, not echo -e: portable to any POSIX sh/BSD echo, and a message's
# own backslashes and % signs are printed as they are.
log_info() {
    printf '\033[1;34m[INFO]\033[0m %s\n' "$*"
}

log_success() {
    printf '\033[1;32m[SUCCESS]\033[0m %s\n' "$*"
}

log_warn() {
    printf '\033[1;33m[WARN]\033[0m %s\n' "$*"
}

log_error() {
    printf '\033[1;31m[ERROR]\033[0m %s\n' "$*"
}

# --- ata-inventory-api baseline pin ----------------------------------------------------
# The pinned revision and image digest live in ONE file, inventory-pin.env (next to this
# script; ATA_INVENTORY_PIN_FILE overrides the path). Re-pinning is one documented step:
# scripts/repin-inventory.sh (DEPLOY.md, "Re-pinning the inventory baseline"). The env vars
# PINNED_INVENTORY_DIGEST / PINNED_INVENTORY_REVISION still override the file for one run.
COMMON_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export INVENTORY_PIN_FILE="${ATA_INVENTORY_PIN_FILE:-${COMMON_SH_DIR}/inventory-pin.env}"

# True if $1 is sha256:<64 lowercase hex>.
is_valid_image_digest() {
    local hex="${1#sha256:}"
    [ "${hex}" != "$1" ] && [ "${#hex}" -eq 64 ] || return 1
    case "${hex}" in
        *[!0-9a-f]*) return 1 ;;
    esac
}

# True if $1 is a revision name of ${NAME_PREFIX}-inventory-api (lowercase, digits, '-').
is_valid_inventory_revision() {
    case "$1" in
        "${NAME_PREFIX}-inventory-api-"?*) ;;
        *) return 1 ;;
    esac
    case "$1" in
        *[!a-z0-9-]*) return 1 ;;
    esac
    [ "${#1}" -le 63 ]
}

# Read pin file $1 into PIN_INVENTORY_REVISION, PIN_INVENTORY_DIGEST,
# PIN_INVENTORY_PREVIOUS_REVISION and PIN_INVENTORY_PREVIOUS_DIGEST. Strict: only those
# KEY=VALUE lines, comments and blank lines; the current pin is required and valid; the
# previous pin is both empty or both valid. Not sourced, so the file cannot run code.
read_inventory_pin_file() {
    local file="$1" line key value n=0
    PIN_INVENTORY_REVISION="" PIN_INVENTORY_DIGEST=""
    PIN_INVENTORY_PREVIOUS_REVISION="" PIN_INVENTORY_PREVIOUS_DIGEST=""
    if [ ! -r "${file}" ]; then
        log_error "Inventory pin file ${file} is missing or unreadable." >&2
        return 1
    fi
    while IFS= read -r line || [ -n "${line}" ]; do
        n=$((n + 1))
        case "${line}" in
            ""|"#"*) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        case "${key}" in
            PIN_INVENTORY_REVISION|PIN_INVENTORY_DIGEST|PIN_INVENTORY_PREVIOUS_REVISION|PIN_INVENTORY_PREVIOUS_DIGEST)
                printf -v "${key}" '%s' "${value}"
                ;;
            *)
                log_error "${file}:${n}: unexpected line '${line}' (only PIN_INVENTORY_* KEY=VALUE lines)." >&2
                return 1
                ;;
        esac
    done < "${file}"
    if ! is_valid_inventory_revision "${PIN_INVENTORY_REVISION}"; then
        log_error "${file}: PIN_INVENTORY_REVISION '${PIN_INVENTORY_REVISION}' is not a ${NAME_PREFIX}-inventory-api revision name." >&2
        return 1
    fi
    if ! is_valid_image_digest "${PIN_INVENTORY_DIGEST}"; then
        log_error "${file}: PIN_INVENTORY_DIGEST '${PIN_INVENTORY_DIGEST}' is not sha256:<64 hex>." >&2
        return 1
    fi
    if [ -n "${PIN_INVENTORY_PREVIOUS_REVISION}${PIN_INVENTORY_PREVIOUS_DIGEST}" ]; then
        if ! is_valid_inventory_revision "${PIN_INVENTORY_PREVIOUS_REVISION}" \
            || ! is_valid_image_digest "${PIN_INVENTORY_PREVIOUS_DIGEST}"; then
            log_error "${file}: PIN_INVENTORY_PREVIOUS_REVISION/DIGEST must both be empty or both valid (got '${PIN_INVENTORY_PREVIOUS_REVISION}' / '${PIN_INVENTORY_PREVIOUS_DIGEST}')." >&2
            return 1
        fi
    fi
}

if ! read_inventory_pin_file "${INVENTORY_PIN_FILE}"; then
    log_error "FATAL: cannot load the ${NAME_PREFIX}-inventory-api pin; fix ${INVENTORY_PIN_FILE} (see DEPLOY.md, 'Re-pinning the inventory baseline')." >&2
    exit 1
fi

# Deterministic pinned demo image and revision.
export PINNED_INVENTORY_DIGEST="${PINNED_INVENTORY_DIGEST:-${PIN_INVENTORY_DIGEST}}"
# Artifact Registry repo ${NAME_PREFIX}-docker is created in the stack's region (foundation).
export ARTIFACT_REGISTRY_HOST="${ATA_ARTIFACT_REGISTRY_HOST:-${REGION}-docker.pkg.dev}"
export PINNED_INVENTORY_IMAGE_BASE="${ARTIFACT_REGISTRY_HOST}/${PROJECT_ID}/${NAME_PREFIX}-docker/${NAME_PREFIX}-inventory-api"
export PINNED_INVENTORY_IMAGE="${PINNED_INVENTORY_IMAGE:-${PINNED_INVENTORY_IMAGE_BASE}@${PINNED_INVENTORY_DIGEST}}"
# PINNED_INVENTORY_REVISION must be a revision whose image is PINNED_INVENTORY_DIGEST
# and whose config is the healthy baseline (correct DB user, no DEPLOY_TAG).
# Older revisions run code where GET on /admin/* performs the fault action.
export PINNED_INVENTORY_REVISION="${PINNED_INVENTORY_REVISION:-${PIN_INVENTORY_REVISION}}"
# The rollback target (prune-revisions.sh keeps it); empty if none.
export PREVIOUS_PINNED_INVENTORY_REVISION="${PIN_INVENTORY_PREVIOUS_REVISION}"
if ! is_valid_image_digest "${PINNED_INVENTORY_DIGEST}"; then
    log_error "FATAL: PINNED_INVENTORY_DIGEST '${PINNED_INVENTORY_DIGEST}' is not sha256:<64 hex>." >&2
    exit 1
fi

# Max Cloud Run instances of the demo workloads (ata-inventory-api, ata-orders-api), at
# service level and per revision. 1: fault state and leaked DB connections live in one
# instance's memory, and /admin/fault/reset clears only the instance it reaches. Keep it
# equal to Terraform's demo_max_instance_count (infra/terraform/applications). Min
# instances stay 0 (scale to zero; min-instances=1 is not approved).
export DEMO_MAX_INSTANCES="${ATA_DEMO_MAX_INSTANCES:-1}"
case "${DEMO_MAX_INSTANCES}" in
    ""|*[!0-9]*|0*)
        log_error "FATAL: ATA_DEMO_MAX_INSTANCES '${DEMO_MAX_INSTANCES}' must be a whole number >= 1 (no leading zeros)." >&2
        exit 1
        ;;
esac

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
        log_error "Fix the pin in ${INVENTORY_PIN_FILE} (scripts/repin-inventory.sh; DEPLOY.md, 'Re-pinning the inventory baseline') or unset PINNED_INVENTORY_REVISION/PINNED_INVENTORY_DIGEST overrides before running scenarios." >&2
        return 1
    fi
}

# Print ata-inventory-api's live traffic as "revision,percent" (empty if describe fails).
inventory_traffic() {
    gcloud run services describe "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format="csv[no-heading](status.traffic.revisionName,status.traffic.percent)" 2>/dev/null || true
}

# Fail loudly unless ata-inventory-api serves 100% from the given revision.
verify_inventory_serving_revision() {
    local revision="$1"
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi
    local traffic
    traffic="$(inventory_traffic)"
    if [ "${traffic}" != "${revision},100" ]; then
        log_error "${NAME_PREFIX}-inventory-api traffic is '${traffic:-<unknown>}', expected ${revision}=100." >&2
        return 1
    fi
}

# Fail loudly unless ata-inventory-api serves 100% from PINNED_INVENTORY_REVISION.
verify_inventory_serving_pinned() {
    if ! verify_inventory_serving_revision "${PINNED_INVENTORY_REVISION}"; then
        log_error "If S5 was run, restore the baseline with: scripts/scenarios/s5_bad_deploy.sh --revert (or scripts/reset-scenarios.sh)." >&2
        return 1
    fi
}

# Demo stock that reset-scenarios.sh restores before every scenario (POST /admin/restock:
# absolute values, idempotent). QA r3: stock ran out across runs (item-101..103 at 0), so
# POST /orders returned HTTP 400. 1000 per item covers one scenario run with margin: a 7 min
# hold sends at most ~525 orders to one item (S1: one order per 0.8 s loop), and reset runs
# between scenarios. Format: <item id>=<stock>,... (every id must exist; stock >= 1).
export INVENTORY_RESTOCK_BASELINE="${ATA_INVENTORY_RESTOCK_BASELINE:-item-101=1000,item-102=1000,item-103=1000,item-104=1000,item-105=1000}"

# Fail loudly (return 1) unless INVENTORY_RESTOCK_BASELINE is a valid, non-empty list of
# <id>=<stock> with unique ids ([A-Za-z0-9._-]) and whole-number stock >= 1.
validate_restock_baseline() {
    local entry id qty seen=" " old_ifs="${IFS}"
    if [ -z "${INVENTORY_RESTOCK_BASELINE}" ]; then
        log_error "ATA_INVENTORY_RESTOCK_BASELINE is empty (expected e.g. item-101=1000,item-102=1000)." >&2
        return 1
    fi
    IFS=","
    set -f
    for entry in ${INVENTORY_RESTOCK_BASELINE}; do
        id="${entry%%=*}"
        qty="${entry#*=}"
        case "${entry}" in
            *=*) ;;
            *) qty="" ;;
        esac
        case "${id}" in
            ""|*[!A-Za-z0-9._-]*) qty="invalid-id" ;;
        esac
        case "${qty}" in
            ""|*[!0-9]*|0*)
                IFS="${old_ifs}"; set +f
                log_error "Invalid ATA_INVENTORY_RESTOCK_BASELINE entry '${entry}' (expected <item id>=<whole number >= 1>)." >&2
                return 1
                ;;
        esac
        case "${seen}" in
            *" ${id} "*)
                IFS="${old_ifs}"; set +f
                log_error "ATA_INVENTORY_RESTOCK_BASELINE lists ${id} twice." >&2
                return 1
                ;;
        esac
        seen="${seen}${id} "
    done
    IFS="${old_ifs}"
    set +f
}

# Baseline env of the inventory revision template (S5 overwrites the DB user). Only the
# ATA_INVENTORY_* names: the app reads nothing else (demo/ata-inventory-api/app/config.py).
INVENTORY_BASELINE_ENV_VARS="ATA_INVENTORY_INSTANCE_CONNECTION_NAME=${PROJECT_ID}:${REGION}:${NAME_PREFIX}-inventory-db,ATA_INVENTORY_DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,ATA_INVENTORY_DB_NAME=inventory,ATA_INVENTORY_DB_IAM_AUTH=true,ATA_INVENTORY_MOCK_DB=false"
# Legacy aliases the app no longer reads; the baseline template and pinned revision must not
# carry them (a stale DB_USER would look like configuration that matters).
INVENTORY_LEGACY_ENV_NAMES="DB_USER,MOCK_DB"

# Exit 0 if the inventory service template already has PINNED_INVENTORY_IMAGE,
# INVENTORY_BASELINE_ENV_VARS, none of INVENTORY_LEGACY_ENV_NAMES, maxScale
# DEMO_MAX_INSTANCES and minScale 0; 1 if it differs, 2 (with an error) if it cannot be read.
inventory_template_matches_baseline() {
    local svc_json
    if ! svc_json="$(gcloud run services describe "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format=json)"; then
        log_error "Could not read the ${NAME_PREFIX}-inventory-api service template." >&2
        return 2
    fi
    python3 "${COMMON_SH_DIR}/inventory_baseline.py" template \
        --image="${PINNED_INVENTORY_IMAGE}" \
        --env="${INVENTORY_BASELINE_ENV_VARS}" \
        --absent="${INVENTORY_LEGACY_ENV_NAMES}" \
        --max="${DEMO_MAX_INSTANCES}" <<<"${svc_json}"
}

# Exit 0 if revision $1 is a valid baseline: runs digest $2, has the baseline env, none of
# the legacy aliases, maxScale DEMO_MAX_INSTANCES, minScale 0 and is Ready. 1 (differences
# on stderr) if not, 2 if it cannot be read. Used by preflight and repin-inventory.sh.
inventory_revision_matches_baseline() {
    local revision="$1" digest="$2" rev_json
    if ! rev_json="$(gcloud run revisions describe "${revision}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format=json)"; then
        log_error "Could not read revision ${revision}." >&2
        return 2
    fi
    python3 "${COMMON_SH_DIR}/inventory_baseline.py" revision \
        --digest="${digest}" \
        --env="${INVENTORY_BASELINE_ENV_VARS}" \
        --absent="${INVENTORY_LEGACY_ENV_NAMES}" \
        --max="${DEMO_MAX_INSTANCES}" <<<"${rev_json}"
}

# Exit 0 if service $1 has the service-level instance limit DEMO_MAX_INSTANCES (gcloud run
# services update --max), 1 if not, 2 if it cannot be read.
service_max_matches_baseline() {
    local svc="$1" svc_json
    if ! svc_json="$(gcloud run services describe "${svc}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format=json)"; then
        log_error "Could not read service ${svc}." >&2
        return 2
    fi
    python3 "${COMMON_SH_DIR}/inventory_baseline.py" service-max --max="${DEMO_MAX_INSTANCES}" <<<"${svc_json}"
}

# Restore ata-inventory-api to the pinned baseline: 100% traffic on
# PINNED_INVENTORY_REVISION and a baseline service template. Each write is skipped
# when the live state already matches, because every `services update` creates a
# new revision (which gets 0% while traffic is pinned by name). Fails loudly.
restore_inventory_pinned_baseline() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        log_info "[MOCK/DRY-RUN] Restored ${NAME_PREFIX}-inventory-api to pinned revision ${PINNED_INVENTORY_REVISION}."
        return 0
    fi
    verify_pinned_inventory_revision || return 1

    # 1. Traffic back to the pinned revision.
    if [ "$(inventory_traffic)" = "${PINNED_INVENTORY_REVISION},100" ]; then
        log_info "    Traffic already 100% on ${PINNED_INVENTORY_REVISION}; not changing it."
    elif ! gcloud run services update-traffic "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${PINNED_INVENTORY_REVISION}=100" \
        --quiet; then
        log_error "Failed to route ${NAME_PREFIX}-inventory-api traffic to ${PINNED_INVENTORY_REVISION}." >&2
        return 1
    fi

    # 2. Service template back to the baseline image, ATA_INVENTORY_* env (no legacy
    # aliases) and instance limits. This does not serve traffic (traffic stays pinned), but
    # it must not keep S5's invalid DB user: `update-traffic --to-latest` or a later deploy
    # would put it live.
    local rc=0
    inventory_template_matches_baseline || rc=$?
    case "${rc}" in
        0)
            log_info "    Service template already on the baseline image, env and instance limits; skipping services update."
            ;;
        1)
            if ! gcloud run services update "${NAME_PREFIX}-inventory-api" \
                --project="${PROJECT_ID}" \
                --region="${REGION}" \
                --image="${PINNED_INVENTORY_IMAGE}" \
                --remove-env-vars="${INVENTORY_LEGACY_ENV_NAMES}" \
                --update-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
                --max-instances="${DEMO_MAX_INSTANCES}" \
                --min-instances=0 \
                --quiet; then
                log_error "Failed to restore the ${NAME_PREFIX}-inventory-api service template." >&2
                return 1
            fi
            ;;
        *)
            return 1
            ;;
    esac

    verify_inventory_serving_pinned
}

# Case conversion via tr: macOS ships bash 3.2, which has no ${x,,} or ${x^^}.
to_lower() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]'; }
to_upper() { printf '%s' "$1" | tr '[:lower:]' '[:upper:]'; }

# Run `gcloud storage <args>` (gcloud CLI credentials; no gsutil). A URL that matches no
# objects is not an error: gcloud storage reports it as "matched no objects" with a
# non-zero exit, and gcs_run returns 0 with empty output for it. Any other failure
# (auth, permission, missing bucket, gcloud not installed) prints gcloud's error on
# stderr and returns gcloud's exit status. stdout is gcloud's stdout.
gcs_run() {
    local err_file rc=0
    err_file="$(mktemp "${TMPDIR:-/tmp}/ata-gcs.XXXXXX")"
    gcloud storage "$@" 2>"${err_file}" || rc=$?
    if [ "${rc}" -ne 0 ]; then
        if grep -q "matched no objects" "${err_file}"; then
            rc=0
        else
            cat "${err_file}" >&2
        fi
    fi
    rm -f "${err_file}"
    return "${rc}"
}

# Next step after a failed gcloud storage call on the incidents bucket.
gcs_error_hint() {
    log_error "gcloud storage uses your gcloud CLI login. Check the active account with 'gcloud auth list'," >&2
    log_error "that it can read and write gs://${INCIDENTS_BUCKET} (roles/storage.objectAdmin), and that" >&2
    log_error "INCIDENTS_BUCKET/PROJECT_ID are right; then rerun this script." >&2
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

# Print the Apigee consumer API key on stdout, or fail loudly. In live mode it never
# prints an empty key. Lookup order: $ATA_API_KEY, ./.ata-api-key,
# alert-triage-agent/.ata-api-key, repo root .ata-api-key. The first key file that exists
# is used; an empty one is an error, not a reason to try the next location.
# Mock/dry-run never call Apigee, so they need no key: it prints nothing and succeeds
# without looking anywhere (callers send no x-api-key header for an empty key).
resolve_api_key() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi

    if [ -n "${ATA_API_KEY:-}" ]; then
        echo "${ATA_API_KEY}"
        return 0
    fi

    local key_file key
    for key_file in "${PWD}/.ata-api-key" "${SCRIPT_DIR}/../../.ata-api-key" "${SCRIPT_DIR}/../../../.ata-api-key"; do
        if [ -f "${key_file}" ]; then
            key="$(tr -d '[:space:]' < "${key_file}")"
            if [ -n "${key}" ]; then
                echo "${key}"
                return 0
            fi
            log_error "Apigee consumer API key file ${key_file} is empty." >&2
            api_key_help >&2
            return 1
        fi
    done

    log_error "Apigee consumer API key not found." >&2
    api_key_help >&2
    return 1
}

# Explain where resolve_api_key looks for the key and how to fetch it (see DEMO.md step 4).
api_key_help() {
    log_error "The key is looked up, in order, from:"
    log_error "  1. the ATA_API_KEY environment variable"
    log_error "  2. ${PWD}/.ata-api-key (current directory)"
    log_error "  3. ${SCRIPT_DIR}/../../.ata-api-key (alert-triage-agent/)"
    log_error "  4. ${SCRIPT_DIR}/../../../.ata-api-key (repository root)"
    log_error "To retrieve your consumer key using your own gcloud credentials (DEMO.md step 4), run:"
    log_error "  curl -s -H \"Authorization: Bearer \$(gcloud auth print-access-token)\" \\"
    log_error "    \"https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}/developers/ata-dev@example.com/apps/ata-demo-app\" \\"
    log_error "    | python3 -c \"import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])\" > .ata-api-key"
    log_error "  chmod 600 .ata-api-key"
    log_error "Or: export ATA_API_KEY=\"<consumer key>\""
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

# require_service_url <service>: the live URL of a Cloud Run service, with no fallback: a
# live run fails loudly (return 1) when it cannot be read (a fixed fallback URL would point
# at another project's service). Mock/dry-run print https://<service>.mock.invalid.
require_service_url() {
    local service_name="$1" url=""
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "https://${service_name}.mock.invalid"
        return 0
    fi
    url="$(gcloud run services describe "${service_name}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)" 2>/dev/null || true)"
    if [ -z "${url}" ]; then
        log_error "Could not read the URL of Cloud Run service ${service_name} in ${PROJECT_ID}/${REGION} (gcloud run services describe failed or returned nothing). Is it deployed, and are you logged in?" >&2
        return 1
    fi
    echo "${url}"
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
        # stderr: stdout is the token, and callers send it as a Bearer header.
        log_warn "Could not resolve identity token via 'gcloud auth print-identity-token'." >&2
        log_warn "Ensure you are authenticated ('gcloud auth login') and have 'roles/run.invoker' on Cloud Run demo services." >&2
    fi

    echo "${token}"
}

# Order item for scenario traffic: the in-stock item with the most stock (GET /items
# through Apigee), so POST /orders reaches the inventory reservation instead of the
# inventory's "Insufficient stock" HTTP 400 for an item earlier runs used up (QA S4: item-101
# returned 400 on every order). Usage: select_order_item <api_key> <expected_orders>.
# Prints the item id. Warns on stderr, naming the consequence, when stock is short; exits
# non-zero when no in-stock item can be read.
select_order_item() {
    local api_key="$1" expected="${2:-0}" body picked item stock
    local key_header=()
    if [ -n "${api_key}" ]; then
        key_header=(-H "x-api-key: ${api_key}")
    fi
    body="$(curl -k -s --connect-timeout 5 --max-time 10 ${key_header[@]+"${key_header[@]}"} \
        "https://${APIGEE_HOST}/ata/inventory/v1/items" 2>/dev/null)" || body=""
    if ! picked="$(printf '%s' "${body}" | python3 -c '
import json, sys
try:
    items = json.load(sys.stdin)
except Exception:
    sys.exit(1)
def stock(i):
    # The API may send 12, 12.0 or "12"; anything else is not a known stock.
    try:
        return int(float(i.get("stock")))
    except (TypeError, ValueError, OverflowError):
        return None
stocked = [i for i in items if isinstance(i, dict) and i.get("id") and (stock(i) or 0) > 0]
if not stocked:
    sys.exit(2)
best = max(stocked, key=stock)
print(best["id"], stock(best))
' 2>/dev/null)"; then
        log_error "Could not find an in-stock item via GET https://${APIGEE_HOST}/ata/inventory/v1/items; every POST /orders would return HTTP 400 and never exercise the inventory path." >&2
        return 1
    fi
    item="${picked%% *}"
    stock="${picked##* }"
    case "${stock}" in
        ""|*[!0-9]*)
            log_warn "Order item ${item} has a non-integer stock '${stock}'; cannot check that it covers the ~${expected} orders this run sends (orders return HTTP 400 once it runs out)." >&2
            ;;
        *)
            if [ "${expected}" -gt "${stock}" ]; then
                log_warn "Order item ${item} has stock ${stock}, below the ~${expected} orders this run sends; orders return HTTP 400 once it runs out." >&2
            fi
            ;;
    esac
    echo "${item}"
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

# Scenario traffic loops: true when a Ctrl-C asked the loop to stop. Usage:
# traffic_interrupted [<exit code of the child just run>].
# A terminal Ctrl-C sends SIGINT to the whole foreground process group, so the loop's
# children (curl, sleep, date) die with it, exit 130. A command substitution's subshell does
# not inherit the script's INT trap: it dies of SIGINT before an inner `|| echo` runs, and
# under set -e the failed assignment aborted the run, so the EXIT trap undid the fault (QA r3).
# So each child in a loop is followed by a check: the INT/TERM trap set STOP_TRAFFIC, or the
# child exited 130 (the trap may not have run yet). The loop then ends and the scenario
# finishes normally (DEMO.md: "Press Ctrl-C ... to safely stop traffic early and view next steps").
traffic_interrupted() {
    [ "${STOP_TRAFFIC:-false}" = "true" ] || [ "${1:-0}" -eq 130 ]
}

# After a scenario aborted with an inventory app fault injected, reset the fault as
# reset-scenarios.sh does: POST ${INV_URL}/admin/fault/reset, with AUTH_HEADER when set.
# Usage: reset_inventory_fault_after_abort <fault description>. Uses INV_URL and AUTH_HEADER.
# Returns 0 when the fault was reset (dry-run injected nothing: the call is only printed), and
# 1 after saying the fault is STILL INJECTED when the reset failed. All output is on stderr.
reset_inventory_fault_after_abort() {
    local fault="$1" code
    if [ "${DRY_RUN}" = "true" ]; then
        log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/reset" >&2
        return 0
    fi
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 30 -X POST \
        ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} "${INV_URL}/admin/fault/reset" 2>/dev/null) || code="000"
    case "${code}" in
        2??)
            log_warn "Reset ${fault} (POST ${INV_URL}/admin/fault/reset -> HTTP ${code})." >&2
            return 0
            ;;
    esac
    log_error "FATAL: POST ${INV_URL}/admin/fault/reset returned HTTP ${code:-<none>}; ${fault} is STILL INJECTED on ${NAME_PREFIX}-inventory-api." >&2
    return 1
}

# Resolve Apigee Management Token
resolve_apigee_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-apigee-token"
        return 0
    fi
    gcloud auth print-access-token 2>/dev/null || echo ""
}

# Cloud Monitoring token source for the reset alert check (ATA_MONITORING_TOKEN_SOURCE):
#   adc     gcloud auth application-default print-access-token (default)
#   gcloud  gcloud auth print-access-token (the active gcloud account)
# ADC is the default because federated/workforce gcloud user tokens are rejected by the
# Monitoring Alerts API with 401 ACCESS_TOKEN_TYPE_UNSUPPORTED, while ADC tokens work.
# Only the configured source is tried; there is no fallback to the other one.
ATA_MONITORING_TOKEN_SOURCE="${ATA_MONITORING_TOKEN_SOURCE:-adc}"

# Print the gcloud command that mints a token for the given source, or fail if unknown.
monitoring_token_command() {
    case "$1" in
        gcloud) echo "gcloud auth print-access-token" ;;
        adc) echo "gcloud auth application-default print-access-token" ;;
        *)
            log_error "Invalid ATA_MONITORING_TOKEN_SOURCE '$1' (expected 'gcloud' or 'adc')." >&2
            return 1
            ;;
    esac
}

# Print the login command that fixes a failed token fetch for the given source.
monitoring_token_login_hint() {
    case "$1" in
        adc) echo "gcloud auth application-default login" ;;
        *) echo "gcloud auth login" ;;
    esac
}

# Resolve Cloud Monitoring Access Token from ATA_MONITORING_TOKEN_SOURCE. On failure,
# prints nothing on stdout, leaves gcloud's error on stderr, and returns non-zero.
resolve_monitoring_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-monitoring-token"
        return 0
    fi
    local cmd
    cmd="$(monitoring_token_command "${ATA_MONITORING_TOKEN_SOURCE}")" || return 1
    # shellcheck disable=SC2086  # cmd is a fixed gcloud command line from monitoring_token_command.
    ${cmd}
}
