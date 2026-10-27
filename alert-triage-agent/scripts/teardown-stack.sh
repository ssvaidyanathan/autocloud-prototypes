#!/usr/bin/env bash
# ==============================================================================
# Tear down one alert-triage-agent stack (the inverse of scripts/deploy-stage1.sh).
#
# Usage:
#   scripts/teardown-stack.sh                         dry run (default): prints every step, changes nothing
#   scripts/teardown-stack.sh --apply                 asks you to type NAME_PREFIX, then deletes
#   scripts/teardown-stack.sh --apply --confirm-prefix <NAME_PREFIX>   non-interactive
#   Options: --delete-state (also delete the Terraform state bucket, last)
#            --skip-apigee  (no Apigee org for this stack)
#
# Required: PROJECT_ID, REGION, NAME_PREFIX ([a-z][a-z0-9]{1,9}); APIGEE_ENV unless
# --skip-apigee; APIGEE_ORG (default: PROJECT_ID, echoed). Optional: TFSTATE_BUCKET.
#
# Order:
#   1. scripts/teardown-apigee.sh --apply --yes (proxies, product, app, developer, TargetServers)
#   2. terraform destroy observability, then triage-iam
#   3. delete Cloud Run ${NAME_PREFIX}-agent, -bridge, -tools (script-made, not in Terraform)
#   4. terraform destroy applications, then database
#   5. empty gs://${PROJECT_ID}-${NAME_PREFIX}-incidents (all versions) and -build
#   6. delete every package in Artifact Registry ${NAME_PREFIX}-docker
#   7. terraform destroy foundation
#   8. with --delete-state only: delete gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate
#   9. "nothing left" check: lists Cloud Run services, Cloud SQL instances, Cloud Tasks
#      queues, secrets, buckets, service accounts, AR repositories, alert policies and
#      notification channels named ${NAME_PREFIX}-* (buckets ${PROJECT_ID}-${NAME_PREFIX}-*);
#      exits 1 if anything remains (the state bucket is expected without --delete-state).
# Never touches a resource whose name does not start with "${NAME_PREFIX}-" (buckets:
# "${PROJECT_ID}-${NAME_PREFIX}-"). APIs stay enabled. Local files (.deploy-state,
# .${NAME_PREFIX}-api-key, .${NAME_PREFIX}-apigee.env in ATA_STACK_DIR) are left alone.
# Note: Cloud SQL keeps a deleted instance name reserved for about a week; a redeploy with the
# same NAME_PREFIX in that window fails at the database phase.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=scripts/cloud-run-deploy-lib.sh
source "${SCRIPT_DIR}/cloud-run-deploy-lib.sh"

APPLY=false
DELETE_STATE=false
SKIP_APIGEE=false
CONFIRM_PREFIX=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        --dry-run) APPLY=false; shift ;;
        --delete-state) DELETE_STATE=true; shift ;;
        --skip-apigee) SKIP_APIGEE=true; shift ;;
        --confirm-prefix)
            [ "$#" -ge 2 ] || { echo "--confirm-prefix needs a value" >&2; exit 1; }
            CONFIRM_PREFIX="$2"; shift 2 ;;
        -h|--help) sed -n '2,35p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
done

die() {
    echo "ERROR: $*" >&2
    exit 1
}

# Guard first: an empty or odd prefix would widen every name match below.
require_vars PROJECT_ID REGION NAME_PREFIX || exit 1
require_name_prefix || exit 1
if [ "${SKIP_APIGEE}" != "true" ]; then
    require_vars APIGEE_ENV || exit 1
    if [ -z "${APIGEE_ORG:-}" ]; then
        APIGEE_ORG="${PROJECT_ID}"
        echo "NOTE: APIGEE_ORG not set; using PROJECT_ID as the Apigee organization: ${APIGEE_ORG}"
    fi
    export APIGEE_ORG APIGEE_ENV
fi
export PROJECT_ID REGION NAME_PREFIX

P="${NAME_PREFIX}"
NAME_GUARD="${P}-"
BUCKET_GUARD="${PROJECT_ID}-${P}-"
STATE_BUCKET="${TFSTATE_BUCKET:-${PROJECT_ID}-${P}-tfstate}"

# assert_owned <name>: refuse anything outside this stack.
assert_owned() {
    case "$1" in
        "${NAME_GUARD}"?*|"${BUCKET_GUARD}"?*) return 0 ;;
    esac
    if [ -n "${TFSTATE_BUCKET:-}" ] && [ "$1" = "${TFSTATE_BUCKET}" ]; then
        return 0
    fi
    die "refusing to touch '$1': it does not start with '${NAME_GUARD}' or '${BUCKET_GUARD}'."
}

# mut <owned name> <command...>: run (apply) or print (dry run) a mutating command.
mut() {
    local name="$1"
    shift
    assert_owned "${name}"
    if [ "${APPLY}" = "true" ]; then
        echo "    + $*"
        "$@"
    else
        echo "    [dry-run] $*"
    fi
}

if [ "${APPLY}" = "true" ]; then MODE="APPLY"; else MODE="DRY RUN"; fi
echo "==================================================================="
echo "  Teardown of stack '${P}' (${MODE})"
echo "  Project: ${PROJECT_ID} | Region: ${REGION}"
echo "  Apigee: $([ "${SKIP_APIGEE}" = "true" ] && echo skipped || echo "org=${APIGEE_ORG} env=${APIGEE_ENV}")"
echo "  State bucket gs://${STATE_BUCKET}: $([ "${DELETE_STATE}" = "true" ] && echo DELETED last || echo kept)"
echo "==================================================================="
if [ "${APPLY}" = "true" ]; then
    if [ -n "${CONFIRM_PREFIX}" ]; then
        [ "${CONFIRM_PREFIX}" = "${P}" ] || die "--confirm-prefix '${CONFIRM_PREFIX}' does not match NAME_PREFIX '${P}'."
    else
        [ -t 0 ] || die "stdin is not a terminal: pass --confirm-prefix ${P} to confirm non-interactively."
        printf 'Type the NAME_PREFIX (%s) to delete this stack: ' "${P}" >&2
        typed=""
        read -r typed || typed=""
        [ "${typed}" = "${P}" ] || die "confirmation '${typed}' does not match '${P}': nothing was changed."
    fi
fi

# --- Terraform ------------------------------------------------------------------
TF_VARS=(-var "project_id=${PROJECT_ID}" -var "region=${REGION}" -var "name_prefix=${P}")

tf_destroy() {
    local root="$1" dir="${REPO_ROOT}/infra/terraform/$1" hcl
    shift
    hcl="${dir}/backend.hcl"
    echo "--> terraform destroy ${root}"
    if [ ! -f "${hcl}" ]; then
        local bucket_args=()
        if [ -n "${TFSTATE_BUCKET:-}" ]; then bucket_args=(--bucket "${TFSTATE_BUCKET}"); fi
        if [ "${APPLY}" = "true" ]; then
            bash "${SCRIPT_DIR}/bootstrap-tfstate.sh" --project-id "${PROJECT_ID}" --region "${REGION}" \
                --name-prefix "${P}" --write-backend-only ${bucket_args[@]+"${bucket_args[@]}"} >/dev/null
        else
            echo "    [dry-run] scripts/bootstrap-tfstate.sh --write-backend-only (no ${hcl})"
        fi
    fi
    if [ -f "${hcl}" ]; then
        grep -Fq "prefix = \"${P}/${root}\"" "${hcl}" || die "${hcl} is not for NAME_PREFIX=${P}."
        grep -Fq "bucket = \"${STATE_BUCKET}\"" "${hcl}" || die "${hcl} does not use bucket ${STATE_BUCKET}."
    fi
    if [ "${APPLY}" = "true" ]; then
        (cd "${dir}" && terraform init -input=false -reconfigure -backend-config=backend.hcl \
            && terraform destroy -input=false -auto-approve ${TF_VARS[@]+"${TF_VARS[@]}"} "$@")
    else
        echo "    [dry-run] (cd infra/terraform/${root} && terraform init -reconfigure -backend-config=backend.hcl && terraform destroy -auto-approve ${TF_VARS[*]} $*)"
    fi
}

# --- Steps ----------------------------------------------------------------------
echo ""
echo "=== 1. Apigee ==="
if [ "${SKIP_APIGEE}" = "true" ]; then
    echo "    skipped (--skip-apigee)"
else
    [ -f "${SCRIPT_DIR}/teardown-apigee.sh" ] || die "scripts/teardown-apigee.sh not found (use --skip-apigee if this stack has no Apigee resources)."
    if [ "${APPLY}" = "true" ]; then
        bash "${SCRIPT_DIR}/teardown-apigee.sh" --apply --yes
    else
        bash "${SCRIPT_DIR}/teardown-apigee.sh"
    fi
fi

echo ""
echo "=== 2. Terraform observability, triage-iam ==="
tf_destroy observability
tf_destroy triage-iam

echo ""
echo "=== 3. Triage Cloud Run services (script-managed) ==="
EXISTING_SERVICES="$(gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" --format="value(metadata.name)")" \
    || die "cannot list Cloud Run services."
for svc in "${P}-agent" "${P}-bridge" "${P}-tools"; do
    if printf '%s\n' "${EXISTING_SERVICES}" | grep -Fxq "${svc}"; then
        mut "${svc}" gcloud run services delete "${svc}" --project="${PROJECT_ID}" --region="${REGION}" --quiet
    else
        echo "    ${svc}: not deployed"
    fi
done

echo ""
echo "=== 4. Terraform applications, database ==="
# The image variables are required by the root but play no part in a destroy.
tf_destroy applications -var "environment=${ENVIRONMENT:-demo}" -var "inventory_image=unused-on-destroy" -var "orders_image=unused-on-destroy"
tf_destroy database

echo ""
echo "=== 5. Empty the incidents and build buckets ==="
EXISTING_BUCKETS="$(gcloud storage buckets list --project="${PROJECT_ID}" --format="value(name)")" || die "cannot list buckets."
for bucket in "${PROJECT_ID}-${P}-incidents" "${PROJECT_ID}-${P}-build"; do
    if printf '%s\n' "${EXISTING_BUCKETS}" | grep -Fxq "${bucket}"; then
        # Empty only when there are objects; "matched no objects" is empty, any other error stops.
        if objects="$(gcloud storage ls --all-versions "gs://${bucket}/**" --project="${PROJECT_ID}" 2>&1)"; then
            mut "${bucket}" gcloud storage rm --recursive --all-versions "gs://${bucket}/**" --project="${PROJECT_ID}"
        else
            case "${objects}" in
                *"matched no objects"*) echo "    gs://${bucket}: already empty" ;;
                *) echo "${objects}" >&2; die "cannot list gs://${bucket}." ;;
            esac
        fi
    else
        echo "    gs://${bucket}: does not exist"
    fi
done

echo ""
echo "=== 6. Artifact Registry images (${P}-docker) ==="
REPO_NAME="${P}-docker"
if gcloud artifacts repositories list --project="${PROJECT_ID}" --location="${REGION}" --format="value(name)" \
        | awk -F/ '{ print $NF }' | grep -Fxq "${REPO_NAME}"; then
    PACKAGES="$(gcloud artifacts packages list --project="${PROJECT_ID}" --location="${REGION}" --repository="${REPO_NAME}" --format="value(name)")" \
        || die "cannot list packages of ${REPO_NAME}."
    for pkg in ${PACKAGES}; do
        pkg="${pkg##*/}"
        mut "${REPO_NAME}" gcloud artifacts packages delete "${pkg}" --project="${PROJECT_ID}" --location="${REGION}" --repository="${REPO_NAME}" --quiet
    done
    [ -n "${PACKAGES}" ] || echo "    ${REPO_NAME}: no images"
else
    echo "    ${REPO_NAME}: does not exist"
fi

echo ""
echo "=== 7. Terraform foundation ==="
tf_destroy foundation -var "environment=${ENVIRONMENT:-demo}"

echo ""
echo "=== 8. Terraform state bucket ==="
if [ "${DELETE_STATE}" = "true" ]; then
    # rm --recursive on the bucket URL deletes every object version and then the bucket
    # (it also works when the bucket is already empty).
    mut "${STATE_BUCKET}" gcloud storage rm --recursive --all-versions "gs://${STATE_BUCKET}" --project="${PROJECT_ID}"
else
    echo "    kept gs://${STATE_BUCKET} (pass --delete-state to delete it)"
fi

# --- Nothing left ---------------------------------------------------------------
echo ""
echo "=== 9. Nothing-left check (names ${NAME_GUARD}*, buckets ${BUCKET_GUARD}*) ==="
REMAINING=0
# report <kind> <names, one per line>: print and count the names that belong to this stack.
report() {
    local kind="$1" names="$2" name
    for name in ${names}; do
        name="${name##*/}"
        case "${name}" in
            "${NAME_GUARD}"*|"${BUCKET_GUARD}"*) ;;
            *) continue ;;
        esac
        if [ "${kind}" = "bucket" ] && [ "${name}" = "${STATE_BUCKET}" ] && [ "${DELETE_STATE}" != "true" ]; then
            echo "    kept      bucket ${name} (state; expected without --delete-state)"
            continue
        fi
        echo "    REMAINING ${kind} ${name}"
        REMAINING=$((REMAINING + 1))
    done
}
# check <kind> <list command...>: run the listing (a failure stops the check) and report it.
check() {
    local kind="$1" out
    shift
    out="$("$@")" || die "listing failed: $*"
    report "${kind}" "${out}"
}
# Monitoring has no GA gcloud list for policies: use the REST API (display names).
monitoring_names() {
    local kind="$1" token
    token="$(gcloud auth print-access-token)" || die "gcloud auth print-access-token failed."
    # Token in a curl config from a process substitution, not on curl's argv (`ps`).
    curl -sf --config <(printf 'header = "Authorization: Bearer %s"\n' "${token}") \
        "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/${kind}?pageSize=1000" \
        | python3 -c '
import json, sys
for item in json.load(sys.stdin).get(sys.argv[1], []):
    print(item.get("displayName", ""))
' "${kind}" || die "cannot list monitoring ${kind}."
}

if [ "${APPLY}" = "true" ]; then
    check "run-service" gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" --format="value(metadata.name)"
    check "sql-instance" gcloud sql instances list --project="${PROJECT_ID}" --format="value(name)"
    check "tasks-queue" gcloud tasks queues list --project="${PROJECT_ID}" --location="${REGION}" --format="value(name)"
    check "secret" gcloud secrets list --project="${PROJECT_ID}" --format="value(name)"
    check "bucket" gcloud storage buckets list --project="${PROJECT_ID}" --format="value(name)"
    check "service-account" gcloud iam service-accounts list --project="${PROJECT_ID}" --format="value(email)"
    check "ar-repository" gcloud artifacts repositories list --project="${PROJECT_ID}" --location="${REGION}" --format="value(name)"
    POLICIES="$(monitoring_names alertPolicies)" || exit 1
    report "alert-policy" "$(printf '%s\n' "${POLICIES}" | tr ' ' '_')"
    CHANNELS="$(monitoring_names notificationChannels)" || exit 1
    report "notification-channel" "$(printf '%s\n' "${CHANNELS}" | tr ' ' '_')"
    echo ""
    if [ "${REMAINING}" -gt 0 ]; then
        echo "=== Teardown INCOMPLETE: ${REMAINING} resource(s) named ${NAME_GUARD}* remain (listed above) ===" >&2
        exit 1
    fi
    echo "=== Teardown complete: nothing named ${NAME_GUARD}* is left ==="
else
    echo "    [dry-run] the check lists run services, sql instances, tasks queues, secrets, buckets,"
    echo "    service accounts, AR repositories, alert policies and notification channels after --apply."
    echo ""
    echo "=== Dry run only: nothing was changed. Re-run with --apply to delete. ==="
fi
