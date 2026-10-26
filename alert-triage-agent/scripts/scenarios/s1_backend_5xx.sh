#!/usr/bin/env bash
# ==============================================================================
# Scenario S1: Backend 5xx Error Spike
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: POST /admin/fault/inject with {"type": "500", "rate": 0.85}
# Alerts: Cloud Run 5xx alert on ata-inventory-api, Apigee 5xx alert on ata-inventory-v1
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api
#   - Blast Radius: ata-inventory-v1, ata-orders-api, ata-orders-v1
#   - Root Cause: Internal application exception in inventory service
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

HOLD_DURATION="${ATA_HOLD_DURATION:-7m}"

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
        --help|-h)
            echo "Usage: $0 [--hold <duration>] [--dry-run] [--mock]"
            echo "  --hold <duration>   Sustained traffic duration (default: 7m, e.g. 7m, 420s, 0s)"
            echo "  --dry-run           Simulate fault injection and traffic without network calls"
            echo "  --mock              Run in mock mode without GCP cloud dependencies"
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

log_info "=== Starting Scenario S1: Backend 5xx Error Spike ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

if [ "${RUN_PREFLIGHT:-false}" = "true" ]; then
    "${SCRIPT_DIR}/preflight.sh"
fi

# 1. Resolve Targets
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"
API_KEY="$(resolve_api_key)"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

log_info "Target Inventory Service: ${INV_URL}"
log_info "Apigee Ingress Host: ${APIGEE_HOST}"

# 2. Inject Fault: POST /admin/fault/inject
log_info "--> Injecting 500 error fault (rate: 0.85) on ata-inventory-api..."
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/inject -d '{\"type\": \"500\", \"rate\": 0.85}'"
else
    HTTP_CODE=$(curl -s -o /tmp/ata-s1-inject-resp.txt -w "%{http_code}" -X POST \
        "${AUTH_HEADER[@]}" \
        -H "Content-Type: application/json" \
        -d '{"type": "500", "rate": 0.85}' \
        "${INV_URL}/admin/fault/inject" || echo "000")
    check_cloud_run_auth "${HTTP_CODE}" "${INV_URL}/admin/fault/inject" || true
    INJECT_RESP="$(cat /tmp/ata-s1-inject-resp.txt 2>/dev/null || echo '{"status":"mocked"}')"
    rm -f /tmp/ata-s1-inject-resp.txt
    log_info "    Fault injection response (HTTP ${HTTP_CODE}): ${INJECT_RESP}"
fi

# 3. Generate Sustained Traffic via Apigee
log_info "--> Initiating sustained traffic loop (hold duration: ${HOLD_SECS}s / ${HOLD_DURATION})..."
log_info "    Driving traffic through Apigee (/ata/inventory/v1/items and /ata/orders/v1/orders)..."

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

STOP_TRAFFIC=false
trap 'STOP_TRAFFIC=true; echo ""; log_warn "Traffic generation interrupted by user (Ctrl-C). Finishing scenario...";' INT TERM

TOTAL_REQUESTS=0
ERRORS_SEEN=0
START_EPOCH=$(date +%s)
LAST_REPORT=${START_EPOCH}

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating traffic wave (1 wave, 0s hold)..."
    log_info "    Requests completed: 10, simulated 500 errors observed: 8"
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
            log_info "[$(date +'%T')] Traffic progress: ${ELAPSED}s / ${HOLD_SECS}s (${PCT}%) | Sent: ${TOTAL_REQUESTS} reqs (Errors: ${ERRORS_SEEN}) | Recent: inv=${STATUS_INV} ord=${STATUS_ORD}"
            LAST_REPORT=${CURRENT_EPOCH}
        fi

        sleep 0.8
    done
    trap - INT TERM
    log_info "    Traffic generation completed: ${TOTAL_REQUESTS} requests sent (${ERRORS_SEEN} errors observed)."
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S1 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api"
log_info "  - Blast Radius: ata-inventory-v1, ata-orders-api, ata-orders-v1"
log_info "  - Root Cause: Internal application exception in inventory service."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s1

2. Check Incident in ServiceNow:
   - Expected Ticket Count: 1
   - Expected Origin: ata-inventory-api
   - Expected Root Cause: Internal application exception in inventory service
   - Blast Radius: ata-inventory-v1, ata-orders-api, ata-orders-v1
   - Verify HTML diagnostic report is attached to ServiceNow ticket

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
