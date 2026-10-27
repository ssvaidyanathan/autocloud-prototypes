#!/usr/bin/env bash
# ==============================================================================
# Scenario S2: Backend Latency Degradation
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: POST /admin/fault/inject with {"type": "latency", "delay_ms": 6500}
# Alerts: Cloud Run Latency alert (>2s), Apigee 504 Gateway Timeout alerts
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api
#   - Blast Radius: All downstream proxies & callers
#   - Root Cause: Backend response time exceeding Apigee 5s target timeout
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

# Resolve the Apigee key before any fault is injected: traffic needs it.
API_KEY="$(resolve_api_key)" || exit 1

log_info "=== Starting Scenario S2: Backend Latency Degradation ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

if [ "${RUN_PREFLIGHT:-false}" = "true" ]; then
    "${SCRIPT_DIR}/preflight.sh"
fi

# 1. Resolve Targets
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

log_info "Target Inventory Service: ${INV_URL}"
log_info "Apigee Ingress Host: ${APIGEE_HOST}"

# 2. Inject Fault: POST /admin/fault/inject with latency delay_ms: 6500
log_info "--> Injecting latency fault (delay: 6500ms) on ata-inventory-api..."
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/inject -d '{\"type\": \"latency\", \"delay_ms\": 6500}'"
else
    HTTP_CODE=$(curl -s -o /tmp/ata-s2-inject-resp.txt -w "%{http_code}" -X POST \
        ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
        -H "Content-Type: application/json" \
        -d '{"type": "latency", "delay_ms": 6500}' \
        "${INV_URL}/admin/fault/inject" || echo "000")
    check_cloud_run_auth "${HTTP_CODE}" "${INV_URL}/admin/fault/inject" || true
    INJECT_RESP="$(cat /tmp/ata-s2-inject-resp.txt 2>/dev/null || echo '{"status":"mocked"}')"
    rm -f /tmp/ata-s2-inject-resp.txt
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
cleanup_jobs() {
    STOP_TRAFFIC=true
    kill $(jobs -p) 2>/dev/null || true
    echo ""
    log_warn "Traffic generation interrupted by user (Ctrl-C). Finishing scenario..."
}
trap cleanup_jobs INT TERM

TOTAL_REQUESTS=0
START_EPOCH=$(date +%s)
LAST_REPORT=${START_EPOCH}

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating latency traffic wave (1 wave, 0s hold)..."
    log_info "    Requests completed: 6, simulated high latency / 504 timeouts observed: 6"
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

        curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 15 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" >/dev/null 2>&1 &
        curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 15 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            -H "Content-Type: application/json" -X POST \
            "https://${APIGEE_HOST}/ata/orders/v1/orders" \
            -d '{"item_id":"item-101","quantity":1}' >/dev/null 2>&1 &

        TOTAL_REQUESTS=$((TOTAL_REQUESTS + 2))

        if [ $((CURRENT_EPOCH - LAST_REPORT)) -ge 15 ]; then
            PCT=$(( (ELAPSED * 100) / (HOLD_SECS > 0 ? HOLD_SECS : 1) ))
            log_info "[$(date +'%T')] Latency traffic progress: ${ELAPSED}s / ${HOLD_SECS}s (${PCT}%) | Dispatched: ${TOTAL_REQUESTS} reqs"
            LAST_REPORT=${CURRENT_EPOCH}
        fi

        sleep 2
    done
    wait 2>/dev/null || true
    trap - INT TERM
    log_info "    Traffic generation completed: ${TOTAL_REQUESTS} requests dispatched."
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S2 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api"
log_info "  - Blast Radius: All downstream proxies & callers"
log_info "  - Root Cause: Backend response time exceeding Apigee 5s target timeout."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s2

2. Check Incident in ServiceNow:
   - Expected Ticket Count: 1
   - Expected Origin: ata-inventory-api
   - Expected Root Cause: Backend response time exceeding Apigee 5s target timeout
   - Blast Radius: ata-inventory-api, ata-apigee-inventory-v1, ata-apigee-orders-v1, ata-orders-api
   - Verify HTML diagnostic report is attached to ServiceNow ticket

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
