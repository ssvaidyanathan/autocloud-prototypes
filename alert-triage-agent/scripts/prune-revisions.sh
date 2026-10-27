#!/usr/bin/env bash
# ==============================================================================
# Prune old Cloud Run revisions of the demo workloads (<prefix>-inventory-api and
# <prefix>-orders-api). Every S5 run and baseline restore creates an inventory revision
# (146 on 2026-10-27).
#
# Lists, oldest first, the revisions that would be deleted: 0% traffic, no traffic tag, not
# the latest created/ready revision, and NOT protected. Protected, never deleted:
#   - the pinned inventory revision (scripts/scenarios/inventory-pin.env, and a
#     PINNED_INVENTORY_REVISION override),
#   - the previous pin (PIN_INVENTORY_PREVIOUS_REVISION: the rollback target),
#   - every revision given with --keep.
# Default: dry run (read-only). --apply deletes, and only when ata-inventory-api serves
# 100% from the pinned revision (no scenario in progress). Before deleting, the protected
# set is read again; if it changed, nothing is deleted. Each delete re-checks the guard.
# Fails loudly; a failed delete is reported and makes the exit status non-zero.
# bash 3.2 and BSD userland compatible.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

PRUNE_PY="${SCRIPT_DIR}/scenarios/revision_prune.py"
APPLY=false
KEEP=""
SERVICES=""

usage() {
    cat <<EOF
Usage: $0 [--apply] [--keep <revision>]... [--service inventory|orders]...
  (no option)        Dry run: list what would be deleted and what is kept (and why)
  --apply            Delete the listed revisions (needs inventory on the pinned revision)
  --keep <revision>  Also protect this revision (repeatable)
  --service <name>   Only this service (inventory or orders; repeatable; default both)
Always protected: pinned ${PINNED_INVENTORY_REVISION}, previous pin ${PREVIOUS_PINNED_INVENTORY_REVISION:-<none>},
any revision with traffic or a tag, and each service's latest created/ready revision.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        --keep)
            [ $# -ge 2 ] || { log_error "--keep needs a revision name" >&2; exit 2; }
            KEEP="${KEEP} $2"; shift 2 ;;
        --keep=*) KEEP="${KEEP} ${1#*=}"; shift ;;
        --service)
            [ $# -ge 2 ] || { log_error "--service needs inventory or orders" >&2; exit 2; }
            SERVICES="${SERVICES} $2"; shift 2 ;;
        --service=*) SERVICES="${SERVICES} ${1#*=}"; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_error "FATAL: $0 has no mock mode; without --apply it is already read-only. Unset MOCK_MODE/DRY_RUN." >&2
    exit 1
fi

for k in ${KEEP}; do
    case "${k}" in
        ""|*[!a-z0-9-]*) log_error "FATAL: --keep '${k}' is not a revision name." >&2; exit 2 ;;
    esac
done
[ -n "${SERVICES}" ] || SERVICES="inventory orders"
for s in ${SERVICES}; do
    case "${s}" in
        inventory|orders) ;;
        *) log_error "FATAL: --service '${s}' (expected inventory or orders)." >&2; exit 2 ;;
    esac
done

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/${NAME_PREFIX}-prune.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT

# protect_args: --protect for the pin, the previous pin, the pin file's own value and --keep.
protect_args() {
    local r
    for r in "${PINNED_INVENTORY_REVISION}" "${PIN_INVENTORY_REVISION}" "${PREVIOUS_PINNED_INVENTORY_REVISION}" ${KEEP}; do
        [ -n "${r}" ] && printf -- '--protect=%s\n' "${r}"
    done
}

# plan <service>: write <service>.plan (revision_prune.py lines); fails loudly.
plan() {
    local svc="$1" out="${WORK_DIR}/$1"
    gcloud run services describe "${svc}" --project="${PROJECT_ID}" --region="${REGION}" --format=json \
        > "${out}.service.json" || { log_error "FATAL: cannot describe ${svc}." >&2; return 1; }
    gcloud run revisions list --service="${svc}" --project="${PROJECT_ID}" --region="${REGION}" --format=json \
        > "${out}.revisions.json" || { log_error "FATAL: cannot list revisions of ${svc}." >&2; return 1; }
    # shellcheck disable=SC2046  # one --protect=<rev> per line, revision names have no spaces
    python3 "${PRUNE_PY}" --service-json="${out}.service.json" --revisions-json="${out}.revisions.json" \
        $(protect_args) > "${out}.plan" || { log_error "FATAL: cannot select revisions of ${svc}." >&2; return 1; }
}

# is_protected <service> <revision>: true if the revision is KEEP or MISSING in the fresh plan.
is_protected() {
    awk -v r="$2" '($1 == "KEEP" || $1 == "MISSING") && $2 == r { found = 1 } END { exit found ? 0 : 1 }' \
        "${WORK_DIR}/$1.plan"
}

TOTAL_DELETE=0
for s in ${SERVICES}; do
    svc="${NAME_PREFIX}-${s}-api"
    plan "${svc}" || exit 1
    n_keep="$(awk '$1 == "KEEP"' "${WORK_DIR}/${svc}.plan" | wc -l | tr -d ' ')"
    n_del="$(awk '$1 == "DELETE"' "${WORK_DIR}/${svc}.plan" | wc -l | tr -d ' ')"
    log_info "=== ${svc}: ${n_del} to delete, ${n_keep} kept ==="
    awk '$1 == "KEEP" { $1 = ""; print "    KEEP  " substr($0, 2) }' "${WORK_DIR}/${svc}.plan"
    awk '$1 == "MISSING" { print "    NOTE  protected revision " $2 " does not exist in this service" }' "${WORK_DIR}/${svc}.plan"
    awk '$1 == "DELETE" { print "    DELETE " $2 " (created " $3 ")" }' "${WORK_DIR}/${svc}.plan"
    cp "${WORK_DIR}/${svc}.plan" "${WORK_DIR}/${svc}.plan.first"
    TOTAL_DELETE=$((TOTAL_DELETE + n_del))
done

if [ "${APPLY}" != "true" ]; then
    log_warn "Dry run: ${TOTAL_DELETE} revision(s) would be deleted. Delete them with: $0 --apply$(for k in ${KEEP}; do printf ' --keep %s' "${k}"; done)" >&2
    exit 0
fi
if [ "${TOTAL_DELETE}" -eq 0 ]; then
    log_success "Nothing to delete."
    exit 0
fi

# --apply: only between scenarios, with inventory on the pinned revision.
verify_pinned_inventory_revision || exit 1
if ! verify_inventory_serving_pinned; then
    log_error "FATAL: not pruning while ${NAME_PREFIX}-inventory-api is off the pinned revision (scenario in progress?). Run ./scripts/reset-scenarios.sh first." >&2
    exit 1
fi

FAILED=0
DELETED=0
for s in ${SERVICES}; do
    svc="${NAME_PREFIX}-${s}-api"
    # Read the service again: refuse if what is protected changed since the listing.
    plan "${svc}" || exit 1
    if ! diff <(awk '$1 != "DELETE" { print $1, $2 }' "${WORK_DIR}/${svc}.plan.first") \
              <(awk '$1 != "DELETE" { print $1, $2 }' "${WORK_DIR}/${svc}.plan") >/dev/null; then
        log_error "FATAL: the protected revisions of ${svc} changed since the listing (traffic, tags or latest revision moved); nothing deleted for it. Re-run $0." >&2
        FAILED=$((FAILED + 1))
        continue
    fi
    awk '$1 == "DELETE" { print $2 }' "${WORK_DIR}/${svc}.plan.first" > "${WORK_DIR}/${svc}.delete"
    # fd 3: gcloud must not read the list from stdin.
    while IFS= read -r rev <&3; do
        # Guard on every delete: never the pin, the previous pin, --keep, serving, tagged or latest.
        if is_protected "${svc}" "${rev}" || [ "${rev}" = "${PINNED_INVENTORY_REVISION}" ] \
            || [ "${rev}" = "${PIN_INVENTORY_REVISION}" ] || [ "${rev}" = "${PREVIOUS_PINNED_INVENTORY_REVISION}" ]; then
            log_error "FATAL: refusing to delete protected revision ${rev}." >&2
            exit 1
        fi
        if gcloud run revisions delete "${rev}" --project="${PROJECT_ID}" --region="${REGION}" --quiet; then
            DELETED=$((DELETED + 1))
            log_success "    deleted ${rev}"
        else
            log_error "    FAILED to delete ${rev} (above)." >&2
            FAILED=$((FAILED + 1))
        fi
    done 3< "${WORK_DIR}/${svc}.delete"
done

if [ "${FAILED}" -gt 0 ]; then
    log_error "Pruned ${DELETED} revision(s); ${FAILED} failure(s) (above)." >&2
    exit 1
fi
verify_inventory_serving_pinned || exit 1
log_success "=== Pruned ${DELETED} revision(s); ${NAME_PREFIX}-inventory-api still serves ${PINNED_INVENTORY_REVISION} ==="
