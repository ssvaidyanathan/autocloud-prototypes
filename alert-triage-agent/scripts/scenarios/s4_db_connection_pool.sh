#!/usr/bin/env bash
# ==============================================================================
# Scenario S4: Database Connection Pool Exhaustion
#
# Target: Cloud SQL (<prefix>-inventory-db)
# Mechanism: POST /admin/leak-connections (3 x 30 connections held by the inventory app)
# Alerts:
#   - Cloud SQL Connections Near Max alert (>20 connections)
#   - Cloud Run 5xx alert on <prefix>-inventory-api
#   - Apigee 502/504 alert
# Output: 1 Group, 1 Ticket
#   - Origin: <prefix>-inventory-db (earliest onset, root cause)
#   - Blast Radius: <prefix>-inventory-db -> <prefix>-inventory-api -> <prefix>-orders-api -> Apigee
#   - Root Cause: PostgreSQL connection starvation / pool exhaustion
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
require_apigee_host || exit 1

log_info "=== Starting Scenario S4: Database Connection Pool Exhaustion ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

if [ "${RUN_PREFLIGHT:-false}" = "true" ]; then
    "${SCRIPT_DIR}/preflight.sh"
fi

INV_URL="$(require_service_url "${SVC_INVENTORY}")" || exit 1
ID_TOKEN="$(resolve_id_token "${INV_URL}")"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

# Pre-flight BEFORE the fault: with the pool exhausted, GET /items through Apigee returns
# HTTP 500, so selecting the order item after the leak aborted the run with no traffic and
# the connections still leaked (QA round 2, S4).
if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    ORDER_ITEM="item-101"
else
    # One order per second of hold.
    ORDER_ITEM="$(select_order_item "${API_KEY}" "${HOLD_SECS}")" || exit 1
    log_info "    Order traffic uses ${ORDER_ITEM} (the in-stock item with the most stock)."
fi

# From the first leak request on, an exit with an error releases the leaked connections
# (POST /admin/fault/reset, as reset-scenarios.sh does) instead of leaving the pool
# exhausted. A successful run keeps them: the scenario needs the leak to persist until reset.
LEAK_STARTED=false
release_leak_on_error() {
    local rc=$? code
    if [ "${rc}" -eq 0 ] || [ "${LEAK_STARTED}" != "true" ]; then
        return 0
    fi
    log_error "S4 aborted (exit ${rc}) after leaking DB connections; releasing them via POST ${INV_URL}/admin/fault/reset." >&2
    # Same condition as the leak requests below: --mock still sends them.
    if [ "${DRY_RUN}" = "true" ]; then
        log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/reset" >&2
    else
        code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 30 -X POST \
            ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} "${INV_URL}/admin/fault/reset" 2>/dev/null) || code="000"
        case "${code}" in
            2??)
                log_warn "Leaked connections released on the instance the reset reached (POST ${INV_URL}/admin/fault/reset -> HTTP ${code}). With more than one ${SVC_INVENTORY} instance the leak can be on another one: run ./scripts/reset-scenarios.sh, which verifies the release on ${DB_INSTANCE}." >&2
                ;;
            *)
                log_error "FATAL: POST ${INV_URL}/admin/fault/reset returned HTTP ${code}; the DB connections are STILL LEAKED. Run ./scripts/reset-scenarios.sh now." >&2
                ;;
        esac
    fi
    exit "${rc}"
}
trap release_leak_on_error EXIT

# 1. Leak connections via admin endpoint
log_info "--> Triggering connection leak on ${INV_URL}/admin/leak-connections..."
LEAK_STARTED=true
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/leak-connections -d '{\"count\": 85}'"
else
    for batch in 1 2 3; do
        HTTP_CODE=$(curl -s -o "/tmp/${NAME_PREFIX}-s4-leak-resp.txt" -w "%{http_code}" -X POST \
            ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
            -H "Content-Type: application/json" \
            -d '{"count": 30}' \
            "${INV_URL}/admin/leak-connections" || echo "000")
        check_cloud_run_auth "${HTTP_CODE}" "${INV_URL}/admin/leak-connections" || true
        LEAK_RESP="$(cat "/tmp/${NAME_PREFIX}-s4-leak-resp.txt" 2>/dev/null || echo '{"status":"mocked"}')"
        rm -f "/tmp/${NAME_PREFIX}-s4-leak-resp.txt"
        log_info "    Batch ${batch} leak response (HTTP ${HTTP_CODE}): ${LEAK_RESP}"
    done
fi

# The leak is server-side only: the inventory app holds the connections (one instance,
# DEMO_MAX_INSTANCES=1) until POST /admin/fault/reset. There is no local Cloud SQL helper:
# the earlier one connected and exited at once (holding nothing), swallowed every error and
# needed cloud-sql-python-connector on the laptop.

# 2. Generate Sustained Traffic to Observe Downstream Starvation
log_info "--> Initiating sustained traffic loop (hold duration: ${HOLD_SECS}s / ${HOLD_DURATION})..."
log_info "    Driving traffic through Apigee (${BASEPATH_INVENTORY}/items and ${BASEPATH_ORDERS}/orders)..."

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

STOP_TRAFFIC=false
cleanup_s4() {
    STOP_TRAFFIC=true
    kill $(jobs -p) 2>/dev/null || true
    echo ""
    log_warn "Traffic generation interrupted by user (Ctrl-C). Finishing scenario..."
}
trap cleanup_s4 INT TERM

TOTAL_REQUESTS=0
FAIL_COUNT=0
START_EPOCH=$(date +%s)
LAST_REPORT=${START_EPOCH}
LAST_LEAK_PULSE=${START_EPOCH}

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating DB starvation traffic wave (0s hold)..."
    log_info "    Requests completed: 6, simulated connection errors / 500 responses: 4"
else
    while true; do
        if [ "${STOP_TRAFFIC}" = "true" ]; then
            break
        fi

        # Ctrl-C hits every child too (exit 130): end the loop and finish normally (see
        # traffic_interrupted in common.sh). Any other failure is an error: the EXIT trap cleans up.
        CURRENT_EPOCH=$(date +%s) || { rc=$?; traffic_interrupted "${rc}" && break; exit "${rc}"; }
        ELAPSED=$((CURRENT_EPOCH - START_EPOCH))
        if [ ${ELAPSED} -ge ${HOLD_SECS} ]; then
            break
        fi

        # Pulse connection leak every 60s to maintain pool saturation
        if [ $((CURRENT_EPOCH - LAST_LEAK_PULSE)) -ge 60 ]; then
            curl -s -X POST ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -H "Content-Type: application/json" \
                -d '{"count": 20}' "${INV_URL}/admin/leak-connections" >/dev/null 2>&1 || true
            traffic_interrupted && break
            LAST_LEAK_PULSE=${CURRENT_EPOCH}
        fi

        STATUS_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            "https://${APIGEE_HOST}${BASEPATH_INVENTORY}/items") || { rc=$?; traffic_interrupted "${rc}" && break; STATUS_INV="000"; }
        traffic_interrupted && break
        STATUS_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            -H "Content-Type: application/json" -X POST \
            "https://${APIGEE_HOST}${BASEPATH_ORDERS}/orders" \
            -d "{\"item_id\":\"${ORDER_ITEM}\",\"quantity\":1}") || { rc=$?; traffic_interrupted "${rc}" && break; STATUS_ORD="000"; }
        traffic_interrupted && break

        TOTAL_REQUESTS=$((TOTAL_REQUESTS + 2))
        if [ "${STATUS_INV}" = "500" ] || [ "${STATUS_INV}" = "503" ]; then
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi
        if [ "${STATUS_ORD}" = "500" ] || [ "${STATUS_ORD}" = "502" ] || [ "${STATUS_ORD}" = "504" ]; then
            FAIL_COUNT=$((FAIL_COUNT + 1))
        fi

        if [ $((CURRENT_EPOCH - LAST_REPORT)) -ge 15 ]; then
            PCT=$(( (ELAPSED * 100) / (HOLD_SECS > 0 ? HOLD_SECS : 1) ))
            log_info "[$(date +'%T')] S4 progress: ${ELAPSED}s / ${HOLD_SECS}s (${PCT}%) | Sent: ${TOTAL_REQUESTS} reqs (Failures: ${FAIL_COUNT}) | Recent: inv=${STATUS_INV} ord=${STATUS_ORD}"
            LAST_REPORT=${CURRENT_EPOCH}
        fi

        sleep 1 || true  # interrupted by Ctrl-C: the loop ends via STOP_TRAFFIC
    done
    trap - INT TERM
    log_info "    Traffic generation completed: ${TOTAL_REQUESTS} requests sent (${FAIL_COUNT} failures observed)."
fi

# 3. Scenario Assertion & Summary
log_success "=== Scenario S4 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ${DB_INSTANCE} (Earliest onset timestamp)"
log_info "  - Blast Radius: ${DB_INSTANCE}, ${SVC_INVENTORY}, ${PROXY_INVENTORY}, ${SVC_ORDERS}, ${PROXY_ORDERS} (topology; a 5xx cascade is stated only if measured)"
log_info "  - Root Cause: Database connection pool saturation on ${DB_INSTANCE}."
log_warn "  - The leaked connections persist after traffic stops: ${POLICY_CLOUDSQL_CONNECTIONS} stays OPEN until ./scripts/reset-scenarios.sh releases them. Run the reset first, then expect the close and the recovery note."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s4

2. Check Incident in ServiceNow:
   - Expected Ticket Count: 1
   - Expected Origin: ${DB_INSTANCE} (earliest onset timestamp)
   - Expected Root Cause: PostgreSQL connection starvation / pool exhaustion
   - Blast Radius: ${DB_INSTANCE} -> ${SVC_INVENTORY} -> ${SVC_ORDERS} -> Apigee Proxies
   - Verify HTML diagnostic report is attached to ServiceNow ticket

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
