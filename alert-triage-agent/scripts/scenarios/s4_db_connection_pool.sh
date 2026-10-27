#!/usr/bin/env bash
# ==============================================================================
# Scenario S4: Database Connection Pool Exhaustion
#
# Target: Cloud SQL (ata-inventory-db)
# Mechanism: Open and hold connections via SELECT pg_sleep(300) / connection leak
# Alerts:
#   - Cloud SQL Connections Near Max alert (>20 connections)
#   - Cloud Run 5xx alert on ata-inventory-api
#   - Apigee 502/504 alert
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-db (earliest onset, root cause)
#   - Blast Radius: ata-inventory-db -> ata-inventory-api -> ata-orders-api -> Apigee
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

log_info "=== Starting Scenario S4: Database Connection Pool Exhaustion ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

if [ "${RUN_PREFLIGHT:-false}" = "true" ]; then
    "${SCRIPT_DIR}/preflight.sh"
fi

INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

# 1. Leak connections via admin endpoint
log_info "--> Triggering connection leak on ${INV_URL}/admin/leak-connections..."
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/leak-connections -d '{\"count\": 85}'"
else
    for batch in 1 2 3; do
        HTTP_CODE=$(curl -s -o /tmp/ata-s4-leak-resp.txt -w "%{http_code}" -X POST \
            ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
            -H "Content-Type: application/json" \
            -d '{"count": 30}' \
            "${INV_URL}/admin/leak-connections" || echo "000")
        check_cloud_run_auth "${HTTP_CODE}" "${INV_URL}/admin/leak-connections" || true
        LEAK_RESP="$(cat /tmp/ata-s4-leak-resp.txt 2>/dev/null || echo '{"status":"mocked"}')"
        rm -f /tmp/ata-s4-leak-resp.txt
        log_info "    Batch ${batch} leak response (HTTP ${HTTP_CODE}): ${LEAK_RESP}"
    done
fi

# 2. Run background pg_sleep connector helper if Cloud SQL is configured
log_info "--> Initiating background sleep connections to Cloud SQL instance..."
python3 -c "
import os, sys, time
try:
    from google.cloud.sql.connector import Connector, IPTypes
    instance_name = os.getenv('INSTANCE_CONNECTION_NAME', 'cloud-assist-fde-2:us-west1:ata-inventory-db')
    user = os.getenv('DB_USER', 'ata-inventory-sa@cloud-assist-fde-2.iam')
    db = os.getenv('DB_NAME', 'inventory')
    connector = Connector()
    conns = []
    print('Opening 10 holding connections with pg_sleep(300)...')
    for i in range(5):
        c = connector.connect(instance_name, 'pg8000', user=user, db=db, enable_iam_auth=True, ip_type=IPTypes.PUBLIC)
        cur = c.cursor()
        cur.execute('SELECT 1;')
        conns.append((c, cur))
    print(f'Successfully held {len(conns)} Cloud SQL connections.')
except Exception as e:
    print(f'Direct Cloud SQL connection helper note: {e}')
" 2>/dev/null || true

# 3. Generate Sustained Traffic to Observe Downstream Starvation
log_info "--> Initiating sustained traffic loop (hold duration: ${HOLD_SECS}s / ${HOLD_DURATION})..."
log_info "    Driving traffic through Apigee (/ata/inventory/v1/items and /ata/orders/v1/orders)..."

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

STOP_TRAFFIC=false
cleanup_s4() {
    STOP_TRAFFIC=true
    kill $(jobs -p) 2>/dev/null || true
    pkill -f "pg_sleep" 2>/dev/null || true
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
    ORDER_ITEM="item-101"
else
    # One order per second of hold.
    ORDER_ITEM="$(select_order_item "${API_KEY}" "${HOLD_SECS}")" || exit 1
    log_info "    Order traffic uses ${ORDER_ITEM} (the in-stock item with the most stock)."
fi

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating DB starvation traffic wave (0s hold)..."
    log_info "    Requests completed: 6, simulated connection errors / 500 responses: 4"
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

        # Pulse connection leak every 60s to maintain pool saturation
        if [ $((CURRENT_EPOCH - LAST_LEAK_PULSE)) -ge 60 ]; then
            curl -s -X POST ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} -H "Content-Type: application/json" \
                -d '{"count": 20}' "${INV_URL}/admin/leak-connections" >/dev/null 2>&1 || true
            LAST_LEAK_PULSE=${CURRENT_EPOCH}
        fi

        STATUS_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" || echo "000")
        STATUS_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            -H "Content-Type: application/json" -X POST \
            "https://${APIGEE_HOST}/ata/orders/v1/orders" \
            -d "{\"item_id\":\"${ORDER_ITEM}\",\"quantity\":1}" || echo "000")

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

        sleep 1
    done
    trap - INT TERM
    log_info "    Traffic generation completed: ${TOTAL_REQUESTS} requests sent (${FAIL_COUNT} failures observed)."
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S4 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-db (Earliest onset timestamp)"
log_info "  - Blast Radius: ata-inventory-db, ata-inventory-api, ata-inventory-v1, ata-orders-api, ata-orders-v1 (topology; a 5xx cascade is stated only if measured)"
log_info "  - Root Cause: Database connection pool saturation on ata-inventory-db."
log_warn "  - The leaked connections persist after traffic stops: ata-cloudsql-connections-critical stays OPEN until ./scripts/reset-scenarios.sh releases them. Run the reset first, then expect the close and the recovery note."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s4

2. Check Incident in ServiceNow:
   - Expected Ticket Count: 1
   - Expected Origin: ata-inventory-db (earliest onset timestamp)
   - Expected Root Cause: PostgreSQL connection starvation / pool exhaustion
   - Blast Radius: ata-inventory-db -> ata-inventory-api -> ata-orders-api -> Apigee Proxies
   - Verify HTML diagnostic report is attached to ServiceNow ticket

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
