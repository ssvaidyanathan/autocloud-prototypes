#!/usr/bin/env bash
# ==============================================================================
# Scenario S5: Bad Deploy / Config Failure
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: Deploy new Cloud Run revision configured with invalid DB user/auth
# Alerts: Cloud Run 500 error alert on startup/health, zero Cloud SQL CPU/connection alerts
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api explicitly named revision (e.g. ata-inventory-api-00062-6pj)
#   - Blast Radius: Inventory & dependent APIs
#   - Root Cause: Change-induced database authentication failure
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

HOLD_DURATION="${ATA_HOLD_DURATION:-7m}"

# Revert Helper Function: Restores the exact pinned revision and baseline config
revert_s5() {
    log_info "--> Reverting S5: Restoring exact pinned revision ${PINNED_INVENTORY_REVISION}..."
    restore_inventory_pinned_baseline || {
        log_error "FATAL: S5 revert failed; ${NAME_PREFIX}-inventory-api is NOT on the pinned baseline." >&2
        return 1
    }
    log_success "    Restored exact pinned revision ${PINNED_INVENTORY_REVISION}."
}

if [ "${1:-}" = "--revert" ]; then
    revert_s5 || exit 1
    exit 0
fi

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hold)
            HOLD_DURATION="$2"
            shift 2
            ;;
        --hold=*)
            HOLD_DURATION="${1#*=}"
            shift
            ;;
        --dry-run)
            export DRY_RUN="true"
            shift
            ;;
        --mock)
            export MOCK_MODE="true"
            shift
            ;;
        --revert)
            revert_s5 || exit 1
            exit 0
            ;;
        --help|-h)
            echo "Usage: $0 [--hold <duration>] [--dry-run] [--mock] [--revert]"
            echo "  --hold <duration>   Sustained traffic duration (default: 7m, e.g. 7m, 420s, 0s)"
            echo "  --dry-run           Simulate fault injection and traffic without network calls"
            echo "  --mock              Run in mock mode without GCP cloud dependencies"
            echo "  --revert            Revert service back to healthy pinned revision"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

HOLD_SECS="$(parse_duration "${HOLD_DURATION}")"
SCENARIO_START_TIME="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

log_info "=== Starting Scenario S5: Bad Deploy / Config Failure ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

# 1. Deploy the faulty revision (invalid DB user on the pinned image) and route all
# traffic to it. Inventory traffic is pinned to PINNED_INVENTORY_REVISION by name,
# and Cloud Run keeps a named-revision split for new revisions, so the revision
# created by `services update` gets 0% until traffic is moved to it explicitly.
log_info "--> Updating ata-inventory-api with invalid DB credentials on pinned image..."
if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
    log_info "[MOCK/DRY-RUN] Simulating Cloud Run deployment with DB_USER=invalid_user on ${PINNED_INVENTORY_IMAGE}..."
else
    # Name the revision ourselves: reading latestCreatedRevisionName races with any
    # concurrent `services update`, which would make S5 route to someone else's revision.
    S5_REVISION_SUFFIX="s5-$(date +%s)"
    BAD_REVISION="${NAME_PREFIX}-inventory-api-${S5_REVISION_SUFFIX}"
    if ! gcloud run services update "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --image="${PINNED_INVENTORY_IMAGE}" \
        --update-env-vars="ATA_INVENTORY_DB_USER=invalid-iam-user@${PROJECT_ID}.iam,DB_USER=invalid-iam-user@${PROJECT_ID}.iam,ATA_INVENTORY_MOCK_DB=false,MOCK_DB=false" \
        --revision-suffix="${S5_REVISION_SUFFIX}" \
        --quiet; then
        log_error "FATAL: Cloud Run update of ${NAME_PREFIX}-inventory-api failed; S5 not injected." >&2
        log_error "The service template may already carry the invalid DB user; run '$0 --revert' to restore the baseline." >&2
        exit 1
    fi
    if ! gcloud run revisions describe "${BAD_REVISION}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --format="value(metadata.name)" >/dev/null; then
        log_error "FATAL: no new ${NAME_PREFIX}-inventory-api revision ${BAD_REVISION} after the update; S5 not injected. Run '$0 --revert'." >&2
        exit 1
    fi
    log_info "    Faulty revision created: ${BAD_REVISION}. Routing 100% traffic to it..."
    if ! gcloud run services update-traffic "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${BAD_REVISION}=100" \
        --quiet; then
        log_error "FATAL: could not route traffic to ${BAD_REVISION}; S5 not injected." >&2
        log_error "Run '$0 --revert' to restore the pinned traffic and the service template." >&2
        exit 1
    fi
    if ! verify_inventory_serving_revision "${BAD_REVISION}"; then
        log_error "FATAL: ${BAD_REVISION} is not serving 100% of traffic; S5 not injected. Run '$0 --revert'." >&2
        exit 1
    fi
    log_success "    ${BAD_REVISION} is serving 100% of ${NAME_PREFIX}-inventory-api traffic."
fi

# 2. Check Service Health
log_info "--> Checking service status to observe startup/DB connection failure..."
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"
API_KEY="$(resolve_api_key)"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        "${AUTH_HEADER[@]}" \
        "${INV_URL}/items" || echo "500")
    log_info "    Direct health/items query HTTP status: ${STATUS} (expected 500)"
fi

# 3. Generate Sustained Traffic via Apigee
log_info "--> Initiating sustained traffic loop (hold duration: ${HOLD_SECS}s / ${HOLD_DURATION})..."
log_info "    Driving traffic through Apigee (/ata/inventory/v1/items and /ata/orders/v1/orders)..."

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

STOP_TRAFFIC=false
cleanup_s5() {
    STOP_TRAFFIC=true
    kill $(jobs -p) 2>/dev/null || true
    echo ""
    log_warn "Traffic generation interrupted by user (Ctrl-C). Finishing scenario..."
}
trap cleanup_s5 INT TERM

TOTAL_REQUESTS=0
ERRORS_SEEN=0
START_EPOCH=$(date +%s)
LAST_REPORT=${START_EPOCH}

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating bad deploy traffic wave (0s hold)..."
    log_info "    Requests completed: 10, simulated 500 errors observed: 10"
else
    while true; do
        if [ "${STOP_TRAFFIC}" = "true" ]; then
            break
        fi

        CURRENT_EPOCH=$(date +%s)
        ELAPSED=$((CURRENT_EPOCH - START_EPOCH))
        if [ ${ELAPSED} -ge ${HOLD_SECS} ]; then
            break
        fi

        STATUS_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            "${KEY_HEADER[@]}" \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" || echo "000")
        STATUS_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            "${KEY_HEADER[@]}" \
            -H "Content-Type: application/json" -X POST \
            "https://${APIGEE_HOST}/ata/orders/v1/orders" \
            -d '{"item_id":"item-101","quantity":1}' || echo "000")

        TOTAL_REQUESTS=$((TOTAL_REQUESTS + 2))
        if [ "${STATUS_INV}" = "500" ]; then
            ERRORS_SEEN=$((ERRORS_SEEN + 1))
        fi
        if [ "${STATUS_ORD}" = "500" ] || [ "${STATUS_ORD}" = "502" ]; then
            ERRORS_SEEN=$((ERRORS_SEEN + 1))
        fi

        if [ $((CURRENT_EPOCH - LAST_REPORT)) -ge 15 ]; then
            PCT=$(( (ELAPSED * 100) / (HOLD_SECS > 0 ? HOLD_SECS : 1) ))
            log_info "[$(date +'%T')] S5 progress: ${ELAPSED}s / ${HOLD_SECS}s (${PCT}%) | Sent: ${TOTAL_REQUESTS} reqs (Errors: ${ERRORS_SEEN}) | Recent: inv=${STATUS_INV} ord=${STATUS_ORD}"
            LAST_REPORT=${CURRENT_EPOCH}
        fi

        sleep 1
    done
    trap - INT TERM
    log_info "    Traffic generation completed: ${TOTAL_REQUESTS} requests sent (${ERRORS_SEEN} errors observed)."
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S5 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api explicitly named revision (e.g. ata-inventory-api-00062-6pj)"
log_info "  - Blast Radius: Inventory & dependent APIs"
log_info "  - Root Cause: Change-induced database authentication failure."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s5

2. Check Incident in ServiceNow:
   - Expected Ticket Count: 1
   - Expected Origin: ata-inventory-api (explicitly named bad revision)
   - Expected Root Cause: Change-induced database authentication failure
   - Blast Radius: Inventory & dependent APIs (ata-inventory-v1, ata-orders-api, ata-orders-v1)
   - Verify HTML diagnostic report is attached to ServiceNow ticket

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
