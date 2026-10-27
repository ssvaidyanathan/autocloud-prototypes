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

# Baseline env that S5 overwrites on the inventory service template.
INVENTORY_BASELINE_ENV_VARS="ATA_INVENTORY_DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,ATA_INVENTORY_MOCK_DB=false,MOCK_DB=false"

# Exit 0 if the inventory service template already has PINNED_INVENTORY_IMAGE and
# INVENTORY_BASELINE_ENV_VARS, 1 if it differs, 2 (with an error) if it cannot be read.
inventory_template_matches_baseline() {
    local svc_json
    if ! svc_json="$(gcloud run services describe "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format=json)"; then
        log_error "Could not read the ${NAME_PREFIX}-inventory-api service template." >&2
        return 2
    fi
    python3 -c '
import json, sys
image, wanted = sys.argv[1], dict(kv.split("=", 1) for kv in sys.argv[2].split(","))
try:
    container = json.loads(sys.stdin.read())["spec"]["template"]["spec"]["containers"][0]
except (ValueError, KeyError, IndexError, TypeError) as exc:
    print(f"unreadable service template: {exc!r}", file=sys.stderr)
    sys.exit(2)
env = {e.get("name"): e.get("value") for e in container.get("env", [])}
live_image = container.get("image")
diffs = [f"image={live_image}"] if live_image != image else []
diffs += [f"{k}={env.get(k)}" for k, v in wanted.items() if env.get(k) != v]
if diffs:
    print("template differs from baseline: " + ", ".join(diffs), file=sys.stderr)
    sys.exit(1)
' "${PINNED_INVENTORY_IMAGE}" "${INVENTORY_BASELINE_ENV_VARS}" <<<"${svc_json}"
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

    # 2. Service template back to the baseline image and DB credentials. This does
    # not serve traffic (traffic stays pinned), but it must not keep S5's invalid
    # DB user: `update-traffic --to-latest` or a later deploy would put it live.
    local rc=0
    inventory_template_matches_baseline || rc=$?
    case "${rc}" in
        0)
            log_info "    Service template already on the baseline image and DB env; skipping services update."
            ;;
        1)
            if ! gcloud run services update "${NAME_PREFIX}-inventory-api" \
                --project="${PROJECT_ID}" \
                --region="${REGION}" \
                --image="${PINNED_INVENTORY_IMAGE}" \
                --update-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
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
stocked = [i for i in items if isinstance(i, dict) and i.get("id") and (i.get("stock") or 0) > 0]
if not stocked:
    sys.exit(2)
best = max(stocked, key=lambda i: i.get("stock") or 0)
print(best["id"], best.get("stock"))
' 2>/dev/null)"; then
        log_error "Could not find an in-stock item via GET https://${APIGEE_HOST}/ata/inventory/v1/items; every POST /orders would return HTTP 400 and never exercise the inventory path." >&2
        return 1
    fi
    item="${picked%% *}"
    stock="${picked##* }"
    if [ "${expected}" -gt "${stock}" ] 2>/dev/null; then
        log_warn "Order item ${item} has stock ${stock}, below the ~${expected} orders this run sends; orders return HTTP 400 once it runs out." >&2
    fi
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
