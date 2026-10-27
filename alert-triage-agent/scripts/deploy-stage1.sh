#!/usr/bin/env bash
# ==============================================================================
# From-scratch deploy of one alert-triage-agent stack (any project, region and prefix).
# This is THE deploy entrypoint; scripts/deploy-demo-infra.sh only forwards to it.
#
# Usage:
#   scripts/deploy-stage1.sh [--yes] [--from <phase>] [--to <phase>] [--only <phase>]
#   scripts/deploy-stage1.sh --list-phases
#
# Phases, in order (each can be run alone to resume after a failure):
#   preflight      tools on PATH, gcloud auth and ADC, project readable (read-only)
#   bootstrap      scripts/bootstrap-tfstate.sh: bootstrap APIs, state bucket, backend.hcl x5
#   foundation     Terraform foundation (APIs, SAs, IAM, AR repo, buckets, queues, secrets)
#   secrets        scripts/set-secrets.sh --skip-if-set (ServiceNow), BEFORE the bridge
#   images         scripts/build-image.sh for inventory, orders, agent, bridge, tools
#   database       Terraform database
#   schema         demo/db/schema.sql with the GRANT to the IAM DB user, via cloud-sql-proxy
#   applications   Terraform applications (inventory and orders with the built images)
#   inventory-pin  scripts/repin-inventory.sh prepare --initial / write-pin --initial / switch
#   triage         tools, bridge, agent deploy.sh with the built images
#   triage-iam     Terraform triage-iam (run.invoker of agent, bridge, tools)
#   apigee         scripts/deploy-apigee-proxies.sh
#   observability  Terraform observability (alert policies), after everything exists
#   verify         scripts/verify-stage1.sh, then scripts/verify-demo-infra.sh
#
# Required environment (no defaults that point at any existing stack):
#   PROJECT_ID REGION NAME_PREFIX ENVIRONMENT OWNER SERVICENOW_INSTANCE_URL
#   APIGEE_ENV APIGEE_HOST
#   APIGEE_ORG (default: PROJECT_ID, echoed)
# Optional: SERVICENOW_USERNAME / SERVICENOW_PASSWORD (else prompted), VERIFIER_SA,
#   AUTO_RESOLVE_ON_RECOVERY, MODEL_NAME, GEMINI_LOCATION, IMAGE_TAG, TFSTATE_BUCKET,
#   PGPASSWORD (postgres password for the schema phase; else a new random one is set),
#   SCHEMA_PROXY_PORT (default 54329), DEPLOY_STATE_DIR (default <alert-triage-agent>/.deploy-state).
# Image refs built by the images phase are kept in
#   ${DEPLOY_STATE_DIR}/${PROJECT_ID}-${NAME_PREFIX}-images.env
# for later phases; exported INVENTORY_IMAGE/ORDERS_IMAGE/AGENT_IMAGE/BRIDGE_IMAGE/TOOLS_IMAGE win.
# Prerequisites: gcloud, terraform >= 1.9, curl, python3; psql and cloud-sql-proxy (v2) for
# the schema phase; `gcloud auth login` and `gcloud auth application-default login`.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/cloud-run-deploy-lib.sh
source "${SCRIPT_DIR}/cloud-run-deploy-lib.sh"

PHASES="preflight bootstrap foundation secrets images database schema applications inventory-pin triage triage-iam apigee observability verify"

usage() { sed -n '2,44p' "$0"; }

die() {
    echo "ERROR: $*" >&2
    exit 1
}

phase_index() {
    local want="$1" i=0 p
    for p in ${PHASES}; do
        i=$((i + 1))
        if [ "${p}" = "${want}" ]; then
            printf '%s\n' "${i}"
            return 0
        fi
    done
    return 1
}

FROM_PHASE=""
TO_PHASE=""
ONLY_PHASE=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --yes|-y) ASSUME_YES=true; shift ;;
        --from|--to|--only)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                die "$1 needs a phase name (see --list-phases)."
            fi
            phase_index "$2" >/dev/null || die "unknown phase '$2'. Phases: ${PHASES}"
            case "$1" in
                --from) FROM_PHASE="$2" ;;
                --to) TO_PHASE="$2" ;;
                --only) ONLY_PHASE="$2" ;;
            esac
            shift 2
            ;;
        --list-phases) for p in ${PHASES}; do printf '%s\n' "${p}"; done; exit 0 ;;
        -h|--help) usage; exit 0 ;;
        *) usage >&2; die "unknown argument '$1'." ;;
    esac
done
if [ -n "${ONLY_PHASE}" ] && { [ -n "${FROM_PHASE}" ] || [ -n "${TO_PHASE}" ]; }; then
    die "--only cannot be combined with --from/--to."
fi
if [ -n "${ONLY_PHASE}" ]; then
    FROM_PHASE="${ONLY_PHASE}"
    TO_PHASE="${ONLY_PHASE}"
fi
FIRST_INDEX="$(phase_index "${FROM_PHASE:-preflight}")"
LAST_INDEX="$(phase_index "${TO_PHASE:-verify}")"
[ "${FIRST_INDEX}" -le "${LAST_INDEX}" ] || die "--from ${FROM_PHASE} comes after --to ${TO_PHASE}."

selected() {
    local i
    i="$(phase_index "$1")"
    [ "${i}" -ge "${FIRST_INDEX}" ] && [ "${i}" -le "${LAST_INDEX}" ]
}

SELECTED=""
for p in ${PHASES}; do
    if selected "${p}"; then
        SELECTED="${SELECTED} ${p}"
    fi
done

# --- Inputs --------------------------------------------------------------------
require_vars PROJECT_ID REGION NAME_PREFIX ENVIRONMENT OWNER SERVICENOW_INSTANCE_URL APIGEE_ENV APIGEE_HOST || exit 1
require_name_prefix || exit 1
case "${SERVICENOW_INSTANCE_URL}" in
    https://*) ;;
    *) die "SERVICENOW_INSTANCE_URL must be an https:// URL, got '${SERVICENOW_INSTANCE_URL}'." ;;
esac
APIGEE_ORG_NOTE=""
if [ -z "${APIGEE_ORG:-}" ]; then
    APIGEE_ORG="${PROJECT_ID}"
    APIGEE_ORG_NOTE=" (defaulted to PROJECT_ID)"
fi
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-${REPO_ROOT}/.deploy-state}"
IMAGES_FILE="${DEPLOY_STATE_DIR}/${PROJECT_ID}-${NAME_PREFIX}-images.env"
export PROJECT_ID REGION NAME_PREFIX ENVIRONMENT OWNER SERVICENOW_INSTANCE_URL APIGEE_ORG APIGEE_ENV APIGEE_HOST

echo "==================================================================="
echo "  alert-triage-agent from-scratch deploy"
echo "  PROJECT_ID=${PROJECT_ID}  REGION=${REGION}  NAME_PREFIX=${NAME_PREFIX}"
echo "  ENVIRONMENT=${ENVIRONMENT}  OWNER=${OWNER}"
echo "  SERVICENOW_INSTANCE_URL=${SERVICENOW_INSTANCE_URL}"
echo "  APIGEE_ORG=${APIGEE_ORG}${APIGEE_ORG_NOTE}  APIGEE_ENV=${APIGEE_ENV}  APIGEE_HOST=${APIGEE_HOST}"
echo "  Phases:${SELECTED}"
echo "==================================================================="
confirm_or_abort || exit 1
# The operator confirmed the whole run: the child scripts must not ask again.
ASSUME_YES=true
export ASSUME_YES

# --- Helpers --------------------------------------------------------------------
TF_COMMON_VARS=(-var "project_id=${PROJECT_ID}" -var "region=${REGION}" -var "name_prefix=${NAME_PREFIX}")
TF_LABEL_VARS=(-var "environment=${ENVIRONMENT}" -var "owner=${OWNER}")

# Refuse a backend.hcl of another project or prefix, so a run never touches another stack's state.
check_backend() {
    local root="$1" hcl="${REPO_ROOT}/infra/terraform/$1/backend.hcl"
    [ -f "${hcl}" ] || die "${hcl} not found: run the bootstrap phase (scripts/bootstrap-tfstate.sh) first."
    grep -Fq "prefix = \"${NAME_PREFIX}/${root}\"" "${hcl}" \
        || die "${hcl} is not for NAME_PREFIX=${NAME_PREFIX} (expected prefix = \"${NAME_PREFIX}/${root}\"). Re-run the bootstrap phase for this stack."
    if [ -z "${TFSTATE_BUCKET:-}" ]; then
        grep -Fq "bucket = \"${PROJECT_ID}-${NAME_PREFIX}-tfstate\"" "${hcl}" \
            || die "${hcl} does not use bucket ${PROJECT_ID}-${NAME_PREFIX}-tfstate. Re-run the bootstrap phase, or export TFSTATE_BUCKET for a custom bucket."
    else
        grep -Fq "bucket = \"${TFSTATE_BUCKET}\"" "${hcl}" \
            || die "${hcl} does not use TFSTATE_BUCKET=${TFSTATE_BUCKET}."
    fi
}

# tf_apply <root> [extra -var args...]: init -reconfigure with the root's backend.hcl, then apply.
tf_apply() {
    local root="$1"
    shift
    check_backend "${root}"
    echo "--> terraform ${root}: init -reconfigure -backend-config=backend.hcl, apply"
    (cd "${REPO_ROOT}/infra/terraform/${root}" && \
        terraform init -input=false -reconfigure -backend-config=backend.hcl && \
        terraform apply -input=false -auto-approve ${TF_COMMON_VARS[@]+"${TF_COMMON_VARS[@]}"} "$@")
}

# Load the image refs: exported values win, else the images file of this project/prefix.
load_images() {
    local name value
    for name in INVENTORY_IMAGE ORDERS_IMAGE AGENT_IMAGE BRIDGE_IMAGE TOOLS_IMAGE; do
        if [ -z "${!name:-}" ] && [ -f "${IMAGES_FILE}" ]; then
            value="$(grep -E "^${name}=" "${IMAGES_FILE}" | tail -n 1 | cut -d= -f2-)" || value=""
            if [ -n "${value}" ]; then
                eval "${name}=\"\${value}\""
            fi
        fi
    done
}

# need_images NAME...: fail unless every listed image ref is known.
need_images() {
    local name missing=""
    load_images
    for name in "$@"; do
        [ -n "${!name:-}" ] || missing="${missing} ${name}"
    done
    [ -z "${missing}" ] || die "image ref(s) unknown:${missing}. Run the images phase (--only images) or export them (${IMAGES_FILE} has none)."
    for name in "$@"; do
        export "${name?}"
    done
}

# build_one <VAR> <service dir> <image name>: build, record VAR=<repo>@sha256:... in the images file.
build_one() {
    local var="$1" dir="$2" name="$3" out ref
    out="$("${SCRIPT_DIR}/build-image.sh" --yes "${dir}" "${name}")" || die "build of ${name} failed."
    ref="$(printf '%s\n' "${out}" | grep -E '^IMAGE=' | tail -n 1 | cut -d= -f2-)"
    printf '%s\n' "${ref}" | grep -Eq "^$(artifact_repo)/${name}@sha256:[0-9a-f]{64}$" \
        || die "build-image.sh printed no IMAGE=<repo>/${name}@sha256:<hex> line (got '${out}')."
    printf '%s=%s\n' "${var}" "${ref}" >> "${IMAGES_FILE}"
    eval "${var}=\"\${ref}\""
    export "${var?}"
    echo "    ${var}=${ref}"
}

# --- Phases ---------------------------------------------------------------------
phase_preflight() {
    local tool missing="" tf_version
    for tool in gcloud terraform curl python3; do
        command -v "${tool}" >/dev/null 2>&1 || missing="${missing} ${tool}"
    done
    if selected schema; then
        for tool in psql cloud-sql-proxy; do
            command -v "${tool}" >/dev/null 2>&1 || missing="${missing} ${tool}"
        done
    fi
    [ -z "${missing}" ] || die "required tool(s) not on PATH:${missing}. psql: a PostgreSQL client (e.g. brew install libpq / apt install postgresql-client); cloud-sql-proxy: v2 from https://cloud.google.com/sql/docs/postgres/sql-proxy (or gcloud components install cloud-sql-proxy)."
    tf_version="$(terraform version | head -n 1 | sed -E 's/^Terraform v([0-9]+)\.([0-9]+).*/\1 \2/')"
    # shellcheck disable=SC2086 # split "<major> <minor>" into $1 $2
    set -- ${tf_version}
    if [ "$#" -ne 2 ] || [ "$1" -lt 1 ] || { [ "$1" -eq 1 ] && [ "$2" -lt 9 ]; }; then
        die "Terraform >= 1.9 is required (terraform version: '$(terraform version | head -n 1)')."
    fi
    gcloud auth list --filter=status:ACTIVE --format="value(account)" | grep -q . \
        || die "no active gcloud account: run gcloud auth login."
    gcloud auth application-default print-access-token >/dev/null \
        || die "no Application Default Credentials: run gcloud auth application-default login."
    PROJECT_NUMBER="$(resolve_project_number)" || die "cannot read project ${PROJECT_ID}."
    export PROJECT_NUMBER
    echo "    project ${PROJECT_ID} (number ${PROJECT_NUMBER}) is readable; tools and credentials are in place."
}

phase_bootstrap() {
    local bucket_args=()
    if [ -n "${TFSTATE_BUCKET:-}" ]; then
        bucket_args=(--bucket "${TFSTATE_BUCKET}")
    fi
    "${SCRIPT_DIR}/bootstrap-tfstate.sh" --project-id "${PROJECT_ID}" --region "${REGION}" \
        --name-prefix "${NAME_PREFIX}" --env "${ENVIRONMENT}" --owner "${OWNER}" \
        ${bucket_args[@]+"${bucket_args[@]}"}
}

phase_foundation() {
    tf_apply foundation ${TF_LABEL_VARS[@]+"${TF_LABEL_VARS[@]}"}
}

phase_secrets() {
    "${SCRIPT_DIR}/set-secrets.sh" --yes --skip-if-set
}

phase_images() {
    mkdir -p "${DEPLOY_STATE_DIR}"
    # Local state, never committed.
    [ -f "${DEPLOY_STATE_DIR}/.gitignore" ] || printf '*\n' > "${DEPLOY_STATE_DIR}/.gitignore"
    : > "${IMAGES_FILE}"
    build_one INVENTORY_IMAGE "${REPO_ROOT}/demo/ata-inventory-api" "${NAME_PREFIX}-inventory-api"
    build_one ORDERS_IMAGE "${REPO_ROOT}/demo/ata-orders-api" "${NAME_PREFIX}-orders-api"
    build_one AGENT_IMAGE "${REPO_ROOT}/agent" "${NAME_PREFIX}-agent"
    build_one BRIDGE_IMAGE "${REPO_ROOT}/bridge" "${NAME_PREFIX}-bridge"
    build_one TOOLS_IMAGE "${REPO_ROOT}/tools" "${NAME_PREFIX}-tools"
    echo "    image refs written to ${IMAGES_FILE}"
}

phase_database() {
    tf_apply database ${TF_LABEL_VARS[@]+"${TF_LABEL_VARS[@]}"}
}

# Schema + GRANT. Path: the Cloud SQL Auth Proxy v2 on 127.0.0.1 (ADC credentials; works with
# the instance's public IP, no authorized networks), psql as the built-in postgres user.
# The postgres password is PGPASSWORD when exported, else a new random one set with
# `gcloud sql users set-password` (never printed). Any failure stops the deploy.
SCHEMA_PROXY_PID=""
stop_schema_proxy() {
    if [ -n "${SCHEMA_PROXY_PID}" ]; then
        kill "${SCHEMA_PROXY_PID}" 2>/dev/null || true
        wait "${SCHEMA_PROXY_PID}" 2>/dev/null || true
        SCHEMA_PROXY_PID=""
    fi
}
phase_schema() {
    local instance="${NAME_PREFIX}-inventory-db"
    local connection="${PROJECT_ID}:${REGION}:${instance}"
    local iam_user="${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam"
    local port="${SCHEMA_PROXY_PORT:-54329}" proxy_log waited=0 password
    command -v psql >/dev/null 2>&1 || die "psql not on PATH (PostgreSQL client)."
    command -v cloud-sql-proxy >/dev/null 2>&1 || die "cloud-sql-proxy (v2) not on PATH."
    case "${port}" in ""|*[!0-9]*) die "SCHEMA_PROXY_PORT '${port}' is not a port number." ;; esac
    if [ -n "${PGPASSWORD:-}" ]; then
        password="${PGPASSWORD}"
        echo "    using the postgres password from PGPASSWORD."
    else
        # python3 secrets: a tr </dev/urandom | head pipeline dies of SIGPIPE under pipefail.
        password="$(python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(32)))')"
        [ "${#password}" -eq 32 ] || die "could not generate a postgres password."
        echo "    setting a new random password for the built-in postgres user of ${instance} (not printed)..."
        gcloud sql users set-password postgres --instance="${instance}" --project="${PROJECT_ID}" \
            --password="${password}" >/dev/null || die "gcloud sql users set-password postgres failed."
    fi
    proxy_log="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-sql-proxy.XXXXXX")"
    echo "    starting cloud-sql-proxy for ${connection} on 127.0.0.1:${port}..."
    cloud-sql-proxy --port "${port}" "${connection}" > "${proxy_log}" 2>&1 &
    SCHEMA_PROXY_PID=$!
    trap 'stop_schema_proxy' EXIT
    while ! grep -q "ready for new connections" "${proxy_log}"; do
        if ! kill -0 "${SCHEMA_PROXY_PID}" 2>/dev/null; then
            cat "${proxy_log}" >&2
            SCHEMA_PROXY_PID=""
            die "cloud-sql-proxy exited before it was ready (log above)."
        fi
        if [ "${waited}" -ge 60 ]; then
            cat "${proxy_log}" >&2
            stop_schema_proxy
            die "cloud-sql-proxy was not ready after 60 s (log above)."
        fi
        sleep 1
        waited=$((waited + 1))
    done
    echo "    applying demo/db/schema.sql (GRANT to ${iam_user})..."
    if ! PGPASSWORD="${password}" psql "host=127.0.0.1 port=${port} dbname=inventory user=postgres sslmode=disable" \
            -v ON_ERROR_STOP=1 -v iam_user="${iam_user}" -f "${REPO_ROOT}/demo/db/schema.sql"; then
        stop_schema_proxy
        die "schema.sql failed (psql output above)."
    fi
    stop_schema_proxy
    trap - EXIT
    rm -f "${proxy_log}"
    password=""
    echo "    schema applied; ${iam_user} has SELECT, UPDATE on inventory_items."
}

phase_applications() {
    need_images INVENTORY_IMAGE ORDERS_IMAGE
    tf_apply applications ${TF_LABEL_VARS[@]+"${TF_LABEL_VARS[@]}"} \
        -var "inventory_image=${INVENTORY_IMAGE}" -var "orders_image=${ORDERS_IMAGE}"
}

# First pin of the inventory baseline (ata-s3's repin-inventory.sh): candidate with no traffic,
# verified; pin file written; 100% traffic to it.
phase_inventory_pin() {
    local repin="${SCRIPT_DIR}/repin-inventory.sh" out_file line revision digest
    [ -x "${repin}" ] || [ -f "${repin}" ] || die "${repin} not found: the first inventory pin needs scripts/repin-inventory.sh (prepare --initial)."
    need_images INVENTORY_IMAGE
    digest="${INVENTORY_IMAGE##*@}"
    out_file="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-repin.XXXXXX")"
    bash "${repin}" prepare --initial --image-digest "${digest}" 2>&1 | tee "${out_file}"
    [ "${PIPESTATUS[0]}" -eq 0 ] || die "repin-inventory.sh prepare --initial failed (output above)."
    line="$(grep -E 'write-pin --revision [^ ]+ --digest sha256:[0-9a-f]{64}' "${out_file}" | tail -n 1)" || line=""
    rm -f "${out_file}"
    revision="$(printf '%s\n' "${line}" | sed -E 's/.*write-pin --revision ([^ ]+) --digest .*/\1/')"
    case "${revision}" in
        "${NAME_PREFIX}-inventory-api-"*) ;;
        *) die "could not read the candidate revision from repin-inventory.sh prepare (expected a 'write-pin --revision ${NAME_PREFIX}-inventory-api-... --digest ${digest}' line)." ;;
    esac
    bash "${repin}" write-pin --revision "${revision}" --digest "${digest}" --initial \
        || die "repin-inventory.sh write-pin --initial failed."
    bash "${repin}" switch || die "repin-inventory.sh switch failed."
    echo "    pinned ${revision} (${digest}). Commit scripts/scenarios/inventory-pin.env in your clone."
}

phase_triage() {
    need_images TOOLS_IMAGE BRIDGE_IMAGE AGENT_IMAGE
    bash "${REPO_ROOT}/tools/scripts/deploy.sh" --yes
    bash "${REPO_ROOT}/bridge/scripts/deploy.sh" --yes
    bash "${REPO_ROOT}/agent/scripts/deploy.sh" --yes
}

phase_triage_iam() {
    tf_apply triage-iam
}

phase_apigee() {
    "${SCRIPT_DIR}/deploy-apigee-proxies.sh"
}

phase_observability() {
    tf_apply observability
}

phase_verify() {
    "${SCRIPT_DIR}/verify-stage1.sh"
    "${SCRIPT_DIR}/verify-demo-infra.sh"
}

# Verification hints printed at the end of each phase (read-only commands for the operator).
phase_hints() {
    local P="${PROJECT_ID}" R="${REGION}" N="${NAME_PREFIX}"
    echo "--- Check (${1}) ---"
    case "$1" in
        preflight) echo "    gcloud config list; terraform version" ;;
        bootstrap)
            echo "    gcloud storage buckets describe gs://${TFSTATE_BUCKET:-${P}-${N}-tfstate} --project=${P}"
            echo "    cat infra/terraform/*/backend.hcl   # prefix = \"${N}/<root>\"" ;;
        foundation)
            echo "    (cd infra/terraform/foundation && terraform output)"
            echo "    gcloud iam service-accounts list --project=${P} --filter=\"email:${N}-*\""
            echo "    gcloud secrets list --project=${P} --filter=\"name~${N}-\"" ;;
        secrets) echo "    gcloud secrets versions list ${N}-servicenow-username --project=${P}   # newest version enabled (values are never printed)" ;;
        images)
            echo "    cat ${IMAGES_FILE}"
            echo "    gcloud artifacts docker images list ${R}-docker.pkg.dev/${P}/${N}-docker --include-tags" ;;
        database) echo "    gcloud sql instances describe ${N}-inventory-db --project=${P} --format=\"value(state)\"   # RUNNABLE" ;;
        schema) echo "    gcloud sql users list --instance=${N}-inventory-db --project=${P}   # ${N}-inventory-sa@${P}.iam (CLOUD_IAM_SERVICE_ACCOUNT)" ;;
        applications) echo "    gcloud run services list --project=${P} --region=${R} --filter=\"metadata.name~^${N}-\"" ;;
        inventory-pin)
            echo "    scripts/repin-inventory.sh status"
            echo "    git diff scripts/scenarios/inventory-pin.env   # commit it" ;;
        triage)
            echo "    gcloud run services describe ${N}-agent --project=${P} --region=${R} --format=\"yaml(spec.template.spec.containers[0].env)\""
            echo "    gcloud run services describe ${N}-bridge --project=${P} --region=${R} --format=\"value(status.url)\"" ;;
        triage-iam) echo "    gcloud run services get-iam-policy ${N}-agent --project=${P} --region=${R}   # bridge SA and agent SA: roles/run.invoker" ;;
        apigee) echo "    cat .${N}-apigee.env; curl -sk https://${APIGEE_HOST}/${N}/inventory/v1/health" ;;
        observability) echo "    (cd infra/terraform/observability && terraform state list)" ;;
        verify) echo "    scripts/scenarios/preflight.sh; scripts/run-all-scenarios.sh --scenario s1" ;;
    esac
}

for p in ${SELECTED}; do
    echo ""
    echo "=== Phase ${p} ==="
    case "${p}" in
        inventory-pin) phase_inventory_pin ;;
        triage-iam) phase_triage_iam ;;
        *) "phase_${p}" ;;
    esac
    phase_hints "${p}"
    echo "=== Phase ${p}: done ==="
done

echo ""
echo "==================================================================="
echo "  Deploy finished:${SELECTED}"
echo "  Next: scripts/scenarios/preflight.sh, then one scenario (run-all-scenarios.sh --scenario s1)."
echo "  Resume after a failure with: $0 --from <phase>"
echo "==================================================================="
