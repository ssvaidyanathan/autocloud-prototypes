#!/usr/bin/env bash
# ==============================================================================
# Common Helper Functions & Environment Setup for Demo Scenarios (S1-S8)
#
# Required, no defaults (a default would point at someone else's stack):
#   PROJECT_ID (or GCP_PROJECT), REGION (or GCP_REGION), NAME_PREFIX ([a-z][a-z0-9]{1,9}).
# Every resource name below is derived from NAME_PREFIX once, here; scripts use the
# derived variables, never literal names.
# Apigee (scripts that call Apigee): APIGEE_HOST, APIGEE_ORG, APIGEE_ENV, read from
#   <alert-triage-agent>/.${NAME_PREFIX}-apigee.env (written by deploy-apigee-proxies.sh)
#   or exported. See load_apigee_env / require_apigee_host / require_apigee_mgmt.
# ==============================================================================

set -euo pipefail

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

# --- Stack identity (required) --------------------------------------------------------
# True if $1 is a valid NAME_PREFIX: [a-z][a-z0-9]{1,9} (same rule as Terraform's
# name_prefix validation). Explicit character lists: [a-z] in a glob depends on the locale.
is_valid_name_prefix() {
    case "$1" in
        ""|*[!abcdefghijklmnopqrstuvwxyz0123456789]*) return 1 ;;
        [abcdefghijklmnopqrstuvwxyz]*) ;;
        *) return 1 ;;
    esac
    [ "${#1}" -ge 2 ] && [ "${#1}" -le 10 ]
}

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-}}"
NAME_PREFIX="${NAME_PREFIX:-}"
_missing=""
[ -n "${PROJECT_ID}" ] || _missing="${_missing} PROJECT_ID"
[ -n "${REGION}" ] || _missing="${_missing} REGION"
[ -n "${NAME_PREFIX}" ] || _missing="${_missing} NAME_PREFIX"
if [ -n "${_missing}" ]; then
    log_error "FATAL: required environment not set:${_missing}. There are no defaults: export PROJECT_ID (or GCP_PROJECT), REGION (or GCP_REGION) and NAME_PREFIX for the stack you mean, e.g. export PROJECT_ID=<project> REGION=<region> NAME_PREFIX=<prefix>." >&2
    exit 1
fi
unset _missing
if ! is_valid_name_prefix "${NAME_PREFIX}"; then
    log_error "FATAL: NAME_PREFIX '${NAME_PREFIX}' is invalid: it must match [a-z][a-z0-9]{1,9}." >&2
    exit 1
fi
case "${PROJECT_ID}" in
    *[!abcdefghijklmnopqrstuvwxyz0123456789.:-]*)
        log_error "FATAL: PROJECT_ID '${PROJECT_ID}' is not a GCP project id." >&2
        exit 1
        ;;
esac
case "${REGION}" in
    *[!abcdefghijklmnopqrstuvwxyz0123456789-]*)
        log_error "FATAL: REGION '${REGION}' is not a GCP region name." >&2
        exit 1
        ;;
esac
export PROJECT_ID REGION NAME_PREFIX
export DRY_RUN="${DRY_RUN:-false}"
export MOCK_MODE="${MOCK_MODE:-false}"

COMMON_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The alert-triage-agent directory of this checkout.
ALERT_TRIAGE_DIR="$(cd "${COMMON_SH_DIR}/../.." && pwd)"
# ATA_STACK_DIR: the directory of the stack-local, gitignored files .${NAME_PREFIX}-api-key,
# .${NAME_PREFIX}-apigee.env and .deploy-state/ (default: ALERT_TRIAGE_DIR). Exported so the
# scripts this one starts read and write the same files. The committed inventory pin is not a
# stack-local file: ATA_INVENTORY_PIN_FILE moves it (below).
export ATA_STACK_DIR="${ATA_STACK_DIR:-${ALERT_TRIAGE_DIR}}"

# --- Derived names (the only place they are spelled out) ------------------------------
# Cloud Run services
export SVC_INVENTORY="${NAME_PREFIX}-inventory-api"
export SVC_ORDERS="${NAME_PREFIX}-orders-api"
export SVC_AGENT="${NAME_PREFIX}-agent"
export SVC_BRIDGE="${NAME_PREFIX}-bridge"
export SVC_TOOLS="${NAME_PREFIX}-tools"
# Cloud SQL
export DB_INSTANCE="${NAME_PREFIX}-inventory-db"
export DB_NAME_INVENTORY="inventory"
export DB_CONNECTION_NAME="${PROJECT_ID}:${REGION}:${DB_INSTANCE}"
# Service accounts
export INVENTORY_SA_EMAIL="${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Cloud SQL IAM database user of the inventory SA (the SA email without .gserviceaccount.com).
export INVENTORY_DB_IAM_USER="${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam"
export APIGEE_SA_EMAIL="${NAME_PREFIX}-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com"
# Artifact Registry repo ${NAME_PREFIX}-docker is created in the stack's region (foundation).
export ARTIFACT_REGISTRY_HOST="${ATA_ARTIFACT_REGISTRY_HOST:-${REGION}-docker.pkg.dev}"
export ARTIFACT_REPO="${ARTIFACT_REGISTRY_HOST}/${PROJECT_ID}/${NAME_PREFIX}-docker"
# Cloud Build (the stack's build SA and bucket; foundation Terraform)
export BUILD_SA_EMAIL="${NAME_PREFIX}-build-sa@${PROJECT_ID}.iam.gserviceaccount.com"
export BUILD_BUCKET="gs://${PROJECT_ID}-${NAME_PREFIX}-build"
# Apigee (deploy-apigee-proxies.sh creates them; these names must match it)
export PROXY_ORDERS="${NAME_PREFIX}-orders-v1"
export PROXY_INVENTORY="${NAME_PREFIX}-inventory-v1"
export BASEPATH_ORDERS="/${NAME_PREFIX}/orders/v1"
export BASEPATH_INVENTORY="/${NAME_PREFIX}/inventory/v1"
export TARGET_ORDERS="${NAME_PREFIX}-orders-target"
export TARGET_INVENTORY="${NAME_PREFIX}-inventory-target"
export APIGEE_PRODUCT="${NAME_PREFIX}-product"
export APIGEE_APP="${NAME_PREFIX}-demo-app"
export APIGEE_DEVELOPER="${NAME_PREFIX}-dev@example.com"
export APIGEE_ACCESS_LOG="${NAME_PREFIX}-apigee-access"
# Alert policy display names (infra/terraform/observability/main.tf)
export POLICY_INVENTORY_5XX="${NAME_PREFIX}-inventory-5xx"
export POLICY_INVENTORY_LATENCY="${NAME_PREFIX}-inventory-latency"
export POLICY_ORDERS_5XX="${NAME_PREFIX}-orders-5xx"
export POLICY_ORDERS_LATENCY="${NAME_PREFIX}-orders-latency"
export POLICY_APIGEE_ORDERS_5XX="${NAME_PREFIX}-apigee-orders-5xx"
export POLICY_APIGEE_ORDERS_429="${NAME_PREFIX}-apigee-orders-429"
export POLICY_CLOUDSQL_CONNECTIONS="${NAME_PREFIX}-cloudsql-connections-critical"
export POLICY_CLOUDSQL_CPU="${NAME_PREFIX}-cloudsql-cpu-high"
# Stack-local files
export API_KEY_FILE="${ATA_STACK_DIR}/.${NAME_PREFIX}-api-key"
export APIGEE_ENV_FILE="${ATA_STACK_DIR}/.${NAME_PREFIX}-apigee.env"

# Incident bucket
export INCIDENTS_BUCKET="${INCIDENTS_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-incidents}"

# --- Apigee host, org and environment ------------------------------------------------
# Sources: APIGEE_ENV_FILE (KEY=VALUE lines APIGEE_HOST=, APIGEE_ORG=, APIGEE_ENV=, written
# by deploy-apigee-proxies.sh) and/or the exported variables. Parsed, not sourced. A value
# exported AND in the file must agree: a mismatch is an error, never a silent choice.
# There are no defaults: a missing value fails in the scripts that need it.
load_apigee_env() {
    local file="${APIGEE_ENV_FILE}" line key value n=0 cur
    [ -f "${file}" ] || return 0
    while IFS= read -r line || [ -n "${line}" ]; do
        n=$((n + 1))
        case "${line}" in
            ""|"#"*) continue ;;
        esac
        key="${line%%=*}"
        value="${line#*=}"
        case "${key}" in
            APIGEE_HOST|APIGEE_ORG|APIGEE_ENV) ;;
            *)
                log_error "${file}:${n}: unexpected line '${line}' (only APIGEE_HOST=, APIGEE_ORG=, APIGEE_ENV=)." >&2
                return 1
                ;;
        esac
        case "${value}" in
            *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.:_@-]*)
                log_error "${file}:${n}: ${key} value '${value}' has unexpected characters." >&2
                return 1
                ;;
        esac
        eval "cur=\"\${${key}:-}\""
        if [ -n "${cur}" ] && [ "${cur}" != "${value}" ]; then
            log_error "${key} is exported as '${cur}' but ${file} says '${value}'. Unset ${key} or fix the file (deploy-apigee-proxies.sh writes it)." >&2
            return 1
        fi
        printf -v "${key}" '%s' "${value}"
    done < "${file}"
}
APIGEE_HOST="${APIGEE_HOST:-}"
APIGEE_ORG="${APIGEE_ORG:-}"
APIGEE_ENV="${APIGEE_ENV:-}"
if ! load_apigee_env; then
    log_error "FATAL: cannot load ${APIGEE_ENV_FILE}." >&2
    exit 1
fi
export APIGEE_HOST APIGEE_ORG APIGEE_ENV

# require_apigee_host: APIGEE_HOST (the envgroup hostname that serves the proxies) must be
# set for live runs. Mock/dry-run send no request: an unset host becomes the logged
# placeholder apigee.mock.invalid.
require_apigee_host() {
    if [ -n "${APIGEE_HOST}" ]; then
        return 0
    fi
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        APIGEE_HOST="apigee.mock.invalid"
        export APIGEE_HOST
        log_warn "[MOCK/DRY-RUN] APIGEE_HOST is not set; using the placeholder ${APIGEE_HOST} (no request is sent)." >&2
        return 0
    fi
    log_error "APIGEE_HOST is not set and ${APIGEE_ENV_FILE} does not exist. Run scripts/deploy-apigee-proxies.sh for this stack (it writes the file), or export APIGEE_HOST=<envgroup hostname>." >&2
    return 1
}

# require_apigee_mgmt: APIGEE_ORG and APIGEE_ENV (Apigee management API) must be set for
# live runs. Mock/dry-run never call the management API.
require_apigee_mgmt() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi
    local missing=""
    [ -n "${APIGEE_ORG}" ] || missing="${missing} APIGEE_ORG"
    [ -n "${APIGEE_ENV}" ] || missing="${missing} APIGEE_ENV"
    if [ -n "${missing}" ]; then
        log_error "Not set:${missing} (Apigee organization / environment). They come from ${APIGEE_ENV_FILE} (written by scripts/deploy-apigee-proxies.sh) or the environment; there are no defaults." >&2
        return 1
    fi
}

# --- ${SVC_INVENTORY} baseline pin -----------------------------------------
# The pinned revision and image digest of each stack live in ONE file per prefix,
# inventory-pin.${NAME_PREFIX}.env next to this script (stack data, committed;
# ATA_INVENTORY_PIN_FILE overrides the path). Re-pinning is one documented step:
# scripts/repin-inventory.sh (DEPLOY.md, "Re-pinning the inventory baseline"); a new stack
# gets its file from `repin-inventory.sh write-pin --initial`. The env vars
# PINNED_INVENTORY_DIGEST / PINNED_INVENTORY_REVISION still override the file for one run.
export INVENTORY_PIN_FILE="${ATA_INVENTORY_PIN_FILE:-${COMMON_SH_DIR}/inventory-pin.${NAME_PREFIX}.env}"

# True if $1 is sha256:<64 lowercase hex>.
is_valid_image_digest() {
    local hex="${1#sha256:}"
    [ "${hex}" != "$1" ] && [ "${#hex}" -eq 64 ] || return 1
    case "${hex}" in
        *[!0-9a-f]*) return 1 ;;
    esac
}

# True if $1 is a revision name of ${SVC_INVENTORY} (lowercase, digits, '-').
is_valid_inventory_revision() {
    case "$1" in
        "${SVC_INVENTORY}-"?*) ;;
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
        log_error "${file}: PIN_INVENTORY_REVISION '${PIN_INVENTORY_REVISION}' is not a ${SVC_INVENTORY} revision name." >&2
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

# Load the pin. Scripts that work without one (repin-inventory.sh --initial, the inventory
# deploy.sh on a fresh stack) set INVENTORY_PIN_OPTIONAL=true before sourcing; then a
# MISSING file leaves the pin empty (INVENTORY_PIN_PRESENT=false). An invalid file is
# always fatal.
INVENTORY_PIN_PRESENT=false
PIN_INVENTORY_REVISION="" PIN_INVENTORY_DIGEST=""
PIN_INVENTORY_PREVIOUS_REVISION="" PIN_INVENTORY_PREVIOUS_DIGEST=""
if [ ! -e "${INVENTORY_PIN_FILE}" ] && [ "${INVENTORY_PIN_OPTIONAL:-false}" = "true" ]; then
    :
elif [ ! -e "${INVENTORY_PIN_FILE}" ]; then
    log_error "FATAL: no inventory pin for stack '${NAME_PREFIX}': ${INVENTORY_PIN_FILE} does not exist." >&2
    log_error "A new stack needs its first pin: run scripts/repin-inventory.sh prepare --initial (then write-pin --initial and switch; DEPLOY.md, 'Re-pinning the inventory baseline')." >&2
    exit 1
elif ! read_inventory_pin_file "${INVENTORY_PIN_FILE}"; then
    log_error "FATAL: cannot load the ${SVC_INVENTORY} pin; fix ${INVENTORY_PIN_FILE} (see DEPLOY.md, 'Re-pinning the inventory baseline')." >&2
    exit 1
else
    INVENTORY_PIN_PRESENT=true
fi
# Read by the scripts that source this file (repin-inventory.sh, the inventory deploy.sh).
export INVENTORY_PIN_PRESENT

# Deterministic pinned demo image and revision (empty without a pin, see above).
export PINNED_INVENTORY_DIGEST="${PINNED_INVENTORY_DIGEST:-${PIN_INVENTORY_DIGEST}}"
export PINNED_INVENTORY_IMAGE_BASE="${ARTIFACT_REPO}/${SVC_INVENTORY}"
if [ -n "${PINNED_INVENTORY_DIGEST}" ]; then
    export PINNED_INVENTORY_IMAGE="${PINNED_INVENTORY_IMAGE:-${PINNED_INVENTORY_IMAGE_BASE}@${PINNED_INVENTORY_DIGEST}}"
else
    export PINNED_INVENTORY_IMAGE="${PINNED_INVENTORY_IMAGE:-}"
fi
# PINNED_INVENTORY_REVISION must be a revision whose image is PINNED_INVENTORY_DIGEST
# and whose config is the healthy baseline (correct DB user, no DEPLOY_TAG).
# Older revisions run code where GET on /admin/* performs the fault action.
export PINNED_INVENTORY_REVISION="${PINNED_INVENTORY_REVISION:-${PIN_INVENTORY_REVISION}}"
# The rollback target (prune-revisions.sh keeps it); empty if none.
export PREVIOUS_PINNED_INVENTORY_REVISION="${PIN_INVENTORY_PREVIOUS_REVISION}"
if [ -n "${PINNED_INVENTORY_DIGEST}" ] && ! is_valid_image_digest "${PINNED_INVENTORY_DIGEST}"; then
    log_error "FATAL: PINNED_INVENTORY_DIGEST '${PINNED_INVENTORY_DIGEST}' is not sha256:<64 hex>." >&2
    exit 1
fi

# --- Inventory image build (Cloud Build as the stack's build SA) ----------------------
# Same flags as the stack's other image builds (scripts/build-image.sh): build SA
# BUILD_SA_EMAIL, sources and logs in BUILD_BUCKET.
INVENTORY_DEMO_DIR="${ALERT_TRIAGE_DIR}/demo/ata-inventory-api"  # prefix-ok: repo directory name

# build_inventory_image <tag>: build INVENTORY_DEMO_DIR into
# ${PINNED_INVENTORY_IMAGE_BASE}:<tag>. Prints the pushed sha256 digest on stdout (progress on
# stderr); returns non-zero, saying why, on any failure.
build_inventory_image() {
    local tag="$1" ref digest
    case "${tag}" in
        ""|*[!A-Za-z0-9._-]*|[.-]*) log_error "Image tag '${tag}' is not a valid Docker tag." >&2; return 1 ;;
    esac
    ref="${PINNED_INVENTORY_IMAGE_BASE}:${tag}"
    log_info "Building ${ref} from ${INVENTORY_DEMO_DIR} with Cloud Build (service account ${BUILD_SA_EMAIL}, bucket ${BUILD_BUCKET})..." >&2
    # The build log goes to stderr: gcloud builds submit streams it on stdout, so capturing
    # stdout (e.g. with --format=value(results.images[0].digest)) returns the whole log plus
    # the digest (live ata rollout 2026-10-27). The digest is read back from Artifact
    # Registry for the pushed tag instead, like build_image in scripts/cloud-run-deploy-lib.sh.
    if ! gcloud builds submit "${INVENTORY_DEMO_DIR}" --project="${PROJECT_ID}" --tag="${ref}" \
        --service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA_EMAIL}" \
        --gcs-source-staging-dir="${BUILD_BUCKET}/source" \
        --gcs-log-dir="${BUILD_BUCKET}/logs" >&2; then
        log_error "Cloud Build failed for ${ref} (see the build log above)." >&2
        return 1
    fi
    if ! digest="$(gcloud artifacts docker images describe "${ref}" --project="${PROJECT_ID}" \
        --format="value(image_summary.digest)")"; then
        log_error "Cloud Build pushed ${ref}, but its digest could not be read from Artifact Registry (see gcloud's error above)." >&2
        return 1
    fi
    if ! is_valid_image_digest "${digest}"; then
        log_error "Cloud Build returned no image digest for ${ref} ('${digest}')." >&2
        return 1
    fi
    log_success "Pushed ${PINNED_INVENTORY_IMAGE_BASE}@${digest}" >&2
    echo "${digest}"
}

# inventory_image_exists <digest>: 0 if ${PINNED_INVENTORY_IMAGE_BASE}@<digest> is in the
# stack's Artifact Registry, 1 if it is not there (NOT_FOUND), 2 (gcloud's error on stderr)
# if that cannot be told.
inventory_image_exists() {
    local digest="$1" err_file rc=0
    err_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-ar.XXXXXX")"
    gcloud artifacts docker images describe "${PINNED_INVENTORY_IMAGE_BASE}@${digest}" \
        --project="${PROJECT_ID}" --format="value(image_summary.digest)" >/dev/null 2>"${err_file}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        rm -f "${err_file}"
        return 0
    fi
    if grep -qiE "NOT_FOUND|not found" "${err_file}"; then
        rm -f "${err_file}"
        return 1
    fi
    cat "${err_file}" >&2
    rm -f "${err_file}"
    return 2
}

# pinned_inventory_revision_exists: whether the pin file's revision (PIN_INVENTORY_REVISION) is a
# Cloud Run revision in ${PROJECT_ID}/${REGION}. 0 if it exists, 1 if gcloud says it is not
# found, 2 (gcloud's error on stderr) if that cannot be told. Needs INVENTORY_PIN_PRESENT=true.
# The pin file is per prefix and committed, and a teardown keeps it: a fresh project, or a
# redeploy after teardown, has the file but not the revision. Shared by repin-inventory.sh
# (--initial) and deploy-stage1.sh (applications), so the two cannot disagree.
pinned_inventory_revision_exists() {
    local err_file rc=0
    err_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-rev.XXXXXX")"
    gcloud run revisions describe "${PIN_INVENTORY_REVISION}" --project="${PROJECT_ID}" --region="${REGION}" \
        --format="value(metadata.name)" >/dev/null 2>"${err_file}" || rc=$?
    if [ "${rc}" -eq 0 ]; then
        rm -f "${err_file}"
        return 0
    fi
    if grep -qiE "not found|could not be found|NOT_FOUND" "${err_file}"; then
        rm -f "${err_file}"
        return 1
    fi
    cat "${err_file}" >&2
    rm -f "${err_file}"
    return 2
}

# Max Cloud Run instances of the demo workloads (SVC_INVENTORY, SVC_ORDERS), at
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
    if [ -z "${PINNED_INVENTORY_REVISION}" ] || [ -z "${PINNED_INVENTORY_DIGEST}" ]; then
        log_error "No inventory pin for stack '${NAME_PREFIX}' (${INVENTORY_PIN_FILE}); run scripts/repin-inventory.sh prepare --initial." >&2
        return 1
    fi
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

# Print ${SVC_INVENTORY}'s live traffic as "revision,percent" (empty if describe fails).
inventory_traffic() {
    gcloud run services describe "${SVC_INVENTORY}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format="csv[no-heading](status.traffic.revisionName,status.traffic.percent)" 2>/dev/null || true
}

# Fail loudly unless ${SVC_INVENTORY} serves 100% from the given revision.
verify_inventory_serving_revision() {
    local revision="$1"
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        return 0
    fi
    local traffic
    traffic="$(inventory_traffic)"
    if [ "${traffic}" != "${revision},100" ]; then
        log_error "${SVC_INVENTORY} traffic is '${traffic:-<unknown>}', expected ${revision}=100." >&2
        return 1
    fi
}

# Fail loudly unless ${SVC_INVENTORY} serves 100% from PINNED_INVENTORY_REVISION.
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
# ATA_INVENTORY_* names: the app reads nothing else (demo/<inventory app>/app/config.py).
INVENTORY_BASELINE_ENV_VARS="ATA_INVENTORY_INSTANCE_CONNECTION_NAME=${DB_CONNECTION_NAME},ATA_INVENTORY_DB_USER=${INVENTORY_DB_IAM_USER},ATA_INVENTORY_DB_NAME=${DB_NAME_INVENTORY},ATA_INVENTORY_DB_IAM_AUTH=true,ATA_INVENTORY_MOCK_DB=false"
# Legacy aliases the app no longer reads; the baseline template and pinned revision must not
# carry them (a stale DB_USER would look like configuration that matters).
INVENTORY_LEGACY_ENV_NAMES="DB_USER,MOCK_DB"

# Exit 0 if the inventory service template already has PINNED_INVENTORY_IMAGE,
# INVENTORY_BASELINE_ENV_VARS, none of INVENTORY_LEGACY_ENV_NAMES, maxScale
# DEMO_MAX_INSTANCES and minScale 0; 1 if it differs, 2 (with an error) if it cannot be read.
inventory_template_matches_baseline() {
    local svc_json
    if ! svc_json="$(gcloud run services describe "${SVC_INVENTORY}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format=json)"; then
        log_error "Could not read the ${SVC_INVENTORY} service template." >&2
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

# Restore ${SVC_INVENTORY} to the pinned baseline: 100% traffic on
# PINNED_INVENTORY_REVISION and a baseline service template. Each write is skipped
# when the live state already matches, because every `services update` creates a
# new revision (which gets 0% while traffic is pinned by name). Fails loudly.
restore_inventory_pinned_baseline() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        log_info "[MOCK/DRY-RUN] Restored ${SVC_INVENTORY} to pinned revision ${PINNED_INVENTORY_REVISION}."
        return 0
    fi
    verify_pinned_inventory_revision || return 1

    # 1. Traffic back to the pinned revision.
    if [ "$(inventory_traffic)" = "${PINNED_INVENTORY_REVISION},100" ]; then
        log_info "    Traffic already 100% on ${PINNED_INVENTORY_REVISION}; not changing it."
    elif ! gcloud run services update-traffic "${SVC_INVENTORY}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${PINNED_INVENTORY_REVISION}=100" \
        --quiet; then
        log_error "Failed to route ${SVC_INVENTORY} traffic to ${PINNED_INVENTORY_REVISION}." >&2
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
            if ! gcloud run services update "${SVC_INVENTORY}" \
                --project="${PROJECT_ID}" \
                --region="${REGION}" \
                --image="${PINNED_INVENTORY_IMAGE}" \
                --remove-env-vars="${INVENTORY_LEGACY_ENV_NAMES}" \
                --update-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
                --max-instances="${DEMO_MAX_INSTANCES}" \
                --min-instances=0 \
                --quiet; then
                log_error "Failed to restore the ${SVC_INVENTORY} service template." >&2
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
    err_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-gcs.XXXXXX")"
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
# prints an empty key. Sources, in order: $ATA_API_KEY, then API_KEY_FILE
# (<alert-triage-agent>/.${NAME_PREFIX}-api-key, written by deploy-apigee-proxies.sh).
# An empty key file is an error. There is no other location: a key file of another
# stack or checkout is never picked up.
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

    local key
    if [ -f "${API_KEY_FILE}" ]; then
        key="$(tr -d '[:space:]' < "${API_KEY_FILE}")"
        if [ -n "${key}" ]; then
            echo "${key}"
            return 0
        fi
        log_error "Apigee consumer API key file ${API_KEY_FILE} is empty." >&2
        api_key_help >&2
        return 1
    fi

    log_error "Apigee consumer API key not found for stack '${NAME_PREFIX}'." >&2
    api_key_help >&2
    return 1
}

# Explain where resolve_api_key looks for the key and how to get it (see DEMO.md step 4).
api_key_help() {
    log_error "The key is looked up, in order, from:"
    log_error "  1. the ATA_API_KEY environment variable"
    log_error "  2. ${API_KEY_FILE}"
    log_error "scripts/deploy-apigee-proxies.sh writes that file. To fetch the consumer key of ${APIGEE_APP} with your own gcloud credentials (DEMO.md step 4), run:"
    log_error "  curl -s -H \"Authorization: Bearer \$(gcloud auth application-default print-access-token)\" \\"
    log_error "    \"https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG:-<APIGEE_ORG>}/developers/${APIGEE_DEVELOPER}/apps/${APIGEE_APP}\" \\"
    log_error "    | python3 -c \"import sys, json; print(json.load(sys.stdin)['credentials'][0]['consumerKey'])\" > ${API_KEY_FILE}.tmp \\"
    log_error "    && test -s ${API_KEY_FILE}.tmp && mv ${API_KEY_FILE}.tmp ${API_KEY_FILE}"
    log_error "  chmod 600 ${API_KEY_FILE}"
    log_error "Or: export ATA_API_KEY=\"<consumer key>\""
}

# require_service_url <service>: the live URL of a Cloud Run service, with no fallback: a
# live run fails loudly (return 1) when it cannot be read (a fixed fallback URL would point
# at another stack's service). Mock/dry-run print https://<service>.mock.invalid and say so
# on stderr.
require_service_url() {
    local service_name="$1" url=""
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        url="https://${service_name}.mock.invalid"
        log_warn "[MOCK/DRY-RUN] Using the placeholder URL ${url} for Cloud Run service ${service_name} (no request is sent to it)." >&2
        echo "${url}"
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

    local token="" err_file="" err=""
    err_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-id-token.XXXXXX" 2>/dev/null || mktemp "/tmp/${NAME_PREFIX}-id-token.XXXXXX")"
    if [ -n "${aud}" ]; then
        token="$(gcloud auth print-identity-token --audiences="${aud}" 2>"${err_file}" || true)"
    fi
    if [ -z "${token}" ]; then
        token="$(gcloud auth print-identity-token 2>"${err_file}" || true)"
    fi
    if [ -f "${err_file}" ]; then
        err="$(cat "${err_file}" 2>/dev/null || true)"
        rm -f "${err_file}"
    fi

    if [ -z "${token}" ]; then
        # stderr: stdout is the token, and callers send it as a Bearer header.
        log_warn "Could not resolve identity token via 'gcloud auth print-identity-token'." >&2
        if [ -n "${CLOUDSDK_AUTH_ACCESS_TOKEN:-}" ]; then
            log_warn "CLOUDSDK_AUTH_ACCESS_TOKEN is set in the environment, which breaks 'gcloud auth print-identity-token'. Unset it: unset CLOUDSDK_AUTH_ACCESS_TOKEN" >&2
        fi
        if [ -n "${err}" ]; then
            log_warn "gcloud error: ${err}" >&2
        fi
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
        "https://${APIGEE_HOST}${BASEPATH_INVENTORY}/items" 2>/dev/null)" || body=""
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
        log_error "Could not find an in-stock item via GET https://${APIGEE_HOST}${BASEPATH_INVENTORY}/items; every POST /orders would return HTTP 400 and never exercise the inventory path." >&2
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
    log_error "FATAL: POST ${INV_URL}/admin/fault/reset returned HTTP ${code:-<none>}; ${fault} is STILL INJECTED on ${SVC_INVENTORY}." >&2
    return 1
}

# Token source for Google API access tokens (ATA_TOKEN_SOURCE, default: adc):
#   adc     gcloud auth application-default print-access-token (default)
#   gcloud  gcloud auth print-access-token (the active gcloud account)
# ADC is the default because federated/workforce gcloud user tokens are rejected by
# Google APIs (Apigee management API, Cloud Monitoring Alerts API) with 401 ACCESS_TOKEN_TYPE_UNSUPPORTED.
# ATA_MONITORING_TOKEN_SOURCE is supported for backward compatibility and defaults to ATA_TOKEN_SOURCE.
# Only the configured source is tried; there is no fallback to the other one.
ATA_TOKEN_SOURCE="${ATA_TOKEN_SOURCE:-adc}"
ATA_MONITORING_TOKEN_SOURCE="${ATA_MONITORING_TOKEN_SOURCE:-${ATA_TOKEN_SOURCE}}"

# Print the gcloud command that mints a token for the given source, or fail if unknown.
token_command() {
    local source="$1"
    local var_name="${2:-ATA_TOKEN_SOURCE}"
    case "${source}" in
        gcloud) echo "gcloud auth print-access-token" ;;
        adc) echo "gcloud auth application-default print-access-token" ;;
        *)
            log_error "Invalid ${var_name} '${source}' (expected 'gcloud' or 'adc')." >&2
            return 1
            ;;
    esac
}

# Print the login command that fixes a failed token fetch for the given source.
token_login_hint() {
    case "$1" in
        adc) echo "gcloud auth application-default login" ;;
        *) echo "gcloud auth login" ;;
    esac
}

monitoring_token_command() {
    token_command "$1" "ATA_MONITORING_TOKEN_SOURCE"
}

monitoring_token_login_hint() {
    token_login_hint "$1"
}

# Resolve Google API Access Token from ATA_TOKEN_SOURCE.
# On failure, prints nothing on stdout, logs a clear error naming the login command, and returns non-zero.
resolve_access_token() {
    local source="${1:-${ATA_TOKEN_SOURCE}}"
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-access-token"
        return 0
    fi
    local cmd
    cmd="$(token_command "${source}")" || return 1
    local token rc=0
    token="$(${cmd} 2>&1)" || rc=$?
    if [ "${rc}" -ne 0 ] || [ -z "${token}" ]; then
        log_error "Could not get an access token from token source '${source}' ('${cmd}' failed). Run: $(token_login_hint "${source}")" >&2
        if [ -n "${token}" ]; then
            log_error "${token}" >&2
        fi
        return 1
    fi
    echo "${token}"
}

# Resolve Apigee Management Token
resolve_apigee_token() {
    if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
        echo "mock-apigee-token"
        return 0
    fi
    resolve_access_token "${ATA_TOKEN_SOURCE}"
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
