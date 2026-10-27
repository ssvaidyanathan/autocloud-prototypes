#!/usr/bin/env bash
# ==============================================================================
# Cap the demo workloads at DEMO_MAX_INSTANCES Cloud Run instances (default 1).
#
# Why: fault state (/admin/fault/inject) and leaked DB connections (/admin/leak-connections)
# live in ONE instance's memory, and /admin/fault/reset clears only the instance it reaches.
# With 2 instances (live 2026-10-27 13:37-13:39Z) the S4 reset missed the leak, and a fault
# applies to only part of the traffic.
#
# What (only where the live value differs):
#   - Service level, ata-inventory-api and ata-orders-api: `gcloud run services update
#     --max=N`. Covers every revision, including a pinned revision created before the cap,
#     and creates NO revision. (Not in google provider 5.x, so Terraform cannot set it.)
#   - Revision level, ata-orders-api: `--max-instances=N --min-instances=0`. Orders serves
#     its latest revision, so the new revision serves at once. Terraform declares the same
#     values (demo_max_instance_count). ata-inventory-api's revision level comes with the
#     re-pin (scripts/repin-inventory.sh): its traffic is pinned by name.
# Min instances stay 0 (scale to zero; min-instances=1 is not approved).
#
# Default: dry run (read-only describes; prints what --apply would run). --apply writes,
# then verifies and fails loudly. bash 3.2 and BSD userland compatible.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

BASELINE_PY="${SCRIPT_DIR}/scenarios/inventory_baseline.py"
APPLY=false

usage() {
    cat <<EOF
Usage: $0 [--apply]
  (no option)  Dry run: show the live instance limits and the commands --apply would run
  --apply      Set them, then verify
Limit: DEMO_MAX_INSTANCES=${DEMO_MAX_INSTANCES} (env ATA_DEMO_MAX_INSTANCES; keep it equal to
Terraform's demo_max_instance_count). Services: ${NAME_PREFIX}-inventory-api, ${NAME_PREFIX}-orders-api.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --apply) APPLY=true; shift ;;
        -h|--help) usage; exit 0 ;;
        *) log_error "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_error "FATAL: $0 has no mock mode; without --apply it is already read-only. Unset MOCK_MODE/DRY_RUN." >&2
    exit 1
fi

describe() {
    gcloud run services describe "$1" --project="${PROJECT_ID}" --region="${REGION}" --format=json
}

# run_or_print <description> <gcloud args...>
PENDING=0
run_or_print() {
    local what="$1"
    shift
    if [ "${APPLY}" = "true" ]; then
        log_info "    ${what}: gcloud $*"
        gcloud "$@" || { log_error "FATAL: ${what} failed." >&2; exit 1; }
    else
        log_warn "    WOULD RUN (${what}): gcloud $*"
        PENDING=$((PENDING + 1))
    fi
}

ORDERS="${NAME_PREFIX}-orders-api"
log_info "=== Demo instance caps: max ${DEMO_MAX_INSTANCES}, min 0 ($( [ "${APPLY}" = "true" ] && echo apply || echo dry run)) ==="

for svc in "${NAME_PREFIX}-inventory-api" "${ORDERS}"; do
    log_info "--> ${svc}: service-level max instances"
    svc_json="$(describe "${svc}")" || { log_error "FATAL: cannot describe ${svc}." >&2; exit 1; }
    if python3 "${BASELINE_PY}" service-max --max="${DEMO_MAX_INSTANCES}" <<<"${svc_json}"; then
        log_success "    already ${DEMO_MAX_INSTANCES}"
    else
        run_or_print "${svc} service-level max" run services update "${svc}" \
            --project="${PROJECT_ID}" --region="${REGION}" --max="${DEMO_MAX_INSTANCES}" --quiet
    fi
done

log_info "--> ${ORDERS}: revision-level max/min instances (template; orders serves its latest revision)"
svc_json="$(describe "${ORDERS}")" || { log_error "FATAL: cannot describe ${ORDERS}." >&2; exit 1; }
if python3 "${BASELINE_PY}" scaling --kind=template --max="${DEMO_MAX_INSTANCES}" <<<"${svc_json}"; then
    log_success "    template already max ${DEMO_MAX_INSTANCES}, min 0"
else
    run_or_print "${ORDERS} revision-level limits (creates a revision)" run services update "${ORDERS}" \
        --project="${PROJECT_ID}" --region="${REGION}" \
        --max-instances="${DEMO_MAX_INSTANCES}" --min-instances=0 --quiet
fi

if [ "${APPLY}" != "true" ]; then
    if [ "${PENDING}" -gt 0 ]; then
        log_warn "Dry run: ${PENDING} change(s) pending. Run: $0 --apply" >&2
    else
        log_success "Dry run: nothing to change."
    fi
    exit 0
fi

# Verify what was applied.
failed=0
for svc in "${NAME_PREFIX}-inventory-api" "${ORDERS}"; do
    service_max_matches_baseline "${svc}" || failed=$((failed + 1))
done
svc_json="$(describe "${ORDERS}")" || { log_error "FATAL: cannot describe ${ORDERS}." >&2; exit 1; }
python3 "${BASELINE_PY}" scaling --kind=template --max="${DEMO_MAX_INSTANCES}" <<<"${svc_json}" || failed=$((failed + 1))
# The orders revision that serves must carry the limits too.
for rev in $(python3 "${BASELINE_PY}" serving <<<"${svc_json}" | awk '{print $1}'); do
    rev_json="$(gcloud run revisions describe "${rev}" --project="${PROJECT_ID}" --region="${REGION}" --format=json)" \
        || { log_error "FATAL: cannot describe ${rev}." >&2; exit 1; }
    if python3 "${BASELINE_PY}" scaling --kind=revision --max="${DEMO_MAX_INSTANCES}" <<<"${rev_json}"; then
        log_success "    serving ${ORDERS} revision ${rev}: max ${DEMO_MAX_INSTANCES}, min 0"
    else
        log_error "    serving ${ORDERS} revision ${rev} lacks the limits (above)." >&2
        failed=$((failed + 1))
    fi
done
if [ "${failed}" -gt 0 ]; then
    log_error "FATAL: ${failed} instance-limit check(s) failed after --apply (above)." >&2
    exit 1
fi
log_success "=== Demo instance caps verified: max ${DEMO_MAX_INSTANCES}, min 0 ==="
log_info "ata-inventory-api's pinned revision gets its own revision-level cap with the re-pin (scripts/repin-inventory.sh)."
