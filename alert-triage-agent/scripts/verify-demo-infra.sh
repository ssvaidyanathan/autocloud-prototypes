#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --mock)
            export MOCK_MODE="true"
            shift
            ;;
        --dry-run)
            export DRY_RUN="true"
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --mock      Run in mock mode without GCP cloud dependencies"
            echo "  --dry-run   Simulate verification without network calls"
            echo "  -h, --help  Display this help message"
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            exit 1
            ;;
    esac
done

FAILURES=0

record_failure() {
    local msg="$1"
    echo "    [FAILURE] ${msg}" >&2
    FAILURES=$((FAILURES + 1))
}

# A request at least this slow (ms) can raise an ata-*-latency alert: keep it equal to
# threshold_value of the latency policies (infra/terraform/observability/main.tf, 2000).
SLOW_REQUEST_MS="${ATA_VERIFY_SLOW_REQUEST_MS:-2000}"
case "${SLOW_REQUEST_MS}" in
    ""|*[!0-9]*|0)
        echo "Invalid ATA_VERIFY_SLOW_REQUEST_MS '${SLOW_REQUEST_MS}' (expected a positive number of milliseconds, the latency alert threshold)." >&2
        exit 1
        ;;
esac
# Requests that can raise a latency or 5xx alert, one "- <label>: <reason>" line each.
ALERT_RISK_REQUESTS=""
REQ_BODY_FILE="$(mktemp)"
trap 'rm -f "${REQ_BODY_FILE}"' EXIT

# run_request <label> <curl args...>: one timed request. Sets REQ_STATUS ("000" without a
# response), REQ_MS ("" when curl reported no duration) and REQ_BODY. A request that is
# slower than SLOW_REQUEST_MS, returns HTTP 5xx or gets no response is recorded in
# ALERT_RISK_REQUESTS. Call it in the current shell, not in $(...).
run_request() {
    local label="$1" out secs="" reason=""
    shift
    : > "${REQ_BODY_FILE}"
    # LC_ALL=C: time_total and awk's number parsing use a '.' decimal point.
    out="$(LC_ALL=C curl -s -o "${REQ_BODY_FILE}" -w "%{http_code} %{time_total}" --connect-timeout 5 --max-time 10 "$@" 2>/dev/null)" || true
    REQ_STATUS="${out%% *}"
    case "${out}" in
        *" "*) secs="${out#* }" ;;
    esac
    case "${REQ_STATUS}" in
        [0-9][0-9][0-9]) ;;
        *) REQ_STATUS="000" ;;
    esac
    REQ_MS="$(LC_ALL=C awk -v s="${secs}" 'BEGIN { if (s ~ /^[0-9]+([.][0-9]+)?$/) printf "%d", s * 1000 }')"
    REQ_BODY="$(cat "${REQ_BODY_FILE}")"
    case "${REQ_STATUS}" in
        5??|000) reason="HTTP ${REQ_STATUS}" ;;
        *)
            if [ -z "${REQ_MS}" ]; then
                reason="curl reported no request duration"
            elif [ "${REQ_MS}" -ge "${SLOW_REQUEST_MS}" ]; then
                reason="${REQ_MS} ms (>= ${SLOW_REQUEST_MS} ms)"
            fi
            ;;
    esac
    if [ -n "${reason}" ]; then
        ALERT_RISK_REQUESTS="${ALERT_RISK_REQUESTS}      - ${label}: ${reason}
"
    fi
}

# Explain what slow, 5xx or failed requests of this run can still do, and what to run next.
report_alert_risk() {
    if [ -z "${ALERT_RISK_REQUESTS}" ]; then
        echo "--> Every request completed in under ${SLOW_REQUEST_MS} ms without HTTP 5xx: these checks cannot raise a latency or 5xx alert."
        return 0
    fi
    echo "--> [WARN] Requests that can raise an ata-* latency or 5xx alert (slower than ${SLOW_REQUEST_MS} ms, HTTP 5xx or no response):"
    printf '%s' "${ALERT_RISK_REQUESTS}"
    echo "    The first request to an idle revision is a cold start (~5 s). With the Terraform latency retest window"
    echo "    (latency_duration_secs, default 240s) applied, a single cold start cannot open a latency alert. With the"
    echo "    older 60s window it opens one 5.5-7 min from now, and the agent creates a ServiceNow ticket for it"
    echo "    (the reset cannot prevent that ticket). Next, run: ./scripts/reset-scenarios.sh --alert-window-from-start"
    echo "    It keeps watching for alerts for --alert-lag (default 10 min) from its start, waits for such an alert to"
    echo "    close and its incident group to finish, and archives it, so it does not spill into the next scenario."
}

if [ "${MOCK_MODE:-false}" = "true" ] || [ "${DRY_RUN:-false}" = "true" ]; then
    echo "=== Verifying ATA Demo Infrastructure & Endpoints (MOCK/DRY-RUN) ==="
    echo "--> Verifying Cloud Run direct endpoints..."
    echo "    ata-inventory-api /health: 200 (expected 200)"
    echo "    ata-orders-api /health: 200 (expected 200)"
    echo "--> Verifying Apigee Ingress endpoints (${APIGEE_HOST})..."
    echo "    Apigee inventory /health: 200 (expected 200)"
    echo "    Apigee orders /health: 200 (expected 200)"
    echo "    Apigee missing key rejection: 401 (expected 401)"
    echo "    Apigee GET /items with key: 200 (expected 200)"
    echo "    Apigee POST /orders with key: 200 (expected 200 - order placed for item-101)"
    echo "    Apigee admin inventory rejection: 403 (expected 403)"
    echo "    Apigee admin orders rejection: 403 (expected 403)"
    echo "=== Verification Complete: All Checks Passed ==="
    exit 0
fi

API_KEY="$(resolve_api_key)" || exit 1
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ORD_URL="$(resolve_service_url "${NAME_PREFIX}-orders-api" "https://ata-orders-api-4klvlqnmpa-uw.a.run.app")"

ID_TOKEN_INV="$(resolve_id_token "${INV_URL}")"
ID_TOKEN_ORD="$(resolve_id_token "${ORD_URL}")"

AUTH_HEADER_INV=()
if [ -n "${ID_TOKEN_INV}" ] && [ "${ID_TOKEN_INV}" != "mock-id-token" ]; then
  AUTH_HEADER_INV=(-H "Authorization: Bearer ${ID_TOKEN_INV}")
fi

AUTH_HEADER_ORD=()
if [ -n "${ID_TOKEN_ORD}" ] && [ "${ID_TOKEN_ORD}" != "mock-id-token" ]; then
  AUTH_HEADER_ORD=(-H "Authorization: Bearer ${ID_TOKEN_ORD}")
fi

echo "=== Verifying ATA Demo Infrastructure & Endpoints ==="

# 1. Direct Cloud Run Endpoints (Internal / IAM only)
echo "--> Verifying Cloud Run direct endpoints..."
run_request "GET ${INV_URL}/health" ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/health"
INV_STATUS="${REQ_STATUS}"
run_request "GET ${ORD_URL}/health" ${AUTH_HEADER_ORD[@]+"${AUTH_HEADER_ORD[@]}"} "${ORD_URL}/health"
ORDERS_STATUS="${REQ_STATUS}"
echo "    ata-inventory-api /health: ${INV_STATUS} (expected 200)"
if [ "${INV_STATUS}" != "200" ]; then
    record_failure "ata-inventory-api /health returned ${INV_STATUS} (expected 200)"
fi

echo "    ata-orders-api /health: ${ORDERS_STATUS} (expected 200)"
if [ "${ORDERS_STATUS}" != "200" ]; then
    record_failure "ata-orders-api /health returned ${ORDERS_STATUS} (expected 200)"
fi

# 2. Apigee Ingress Endpoints
echo "--> Verifying Apigee Ingress endpoints (${APIGEE_HOST})..."

# 2a. Unprotected Health Checks
run_request "GET Apigee /ata/inventory/v1/health" -k "https://${APIGEE_HOST}/ata/inventory/v1/health"
APIGEE_INV_HEALTH="${REQ_STATUS}"
run_request "GET Apigee /ata/orders/v1/health" -k "https://${APIGEE_HOST}/ata/orders/v1/health"
APIGEE_ORD_HEALTH="${REQ_STATUS}"
echo "    Apigee inventory /health: ${APIGEE_INV_HEALTH} (expected 200)"
if [ "${APIGEE_INV_HEALTH}" != "200" ]; then
    record_failure "Apigee inventory /health returned ${APIGEE_INV_HEALTH} (expected 200)"
fi

echo "    Apigee orders /health: ${APIGEE_ORD_HEALTH} (expected 200)"
if [ "${APIGEE_ORD_HEALTH}" != "200" ]; then
    record_failure "Apigee orders /health returned ${APIGEE_ORD_HEALTH} (expected 200)"
fi

# 2b. Missing Key Security Rejection
run_request "GET Apigee /ata/orders/v1/orders/test (no key)" -k "https://${APIGEE_HOST}/ata/orders/v1/orders/test"
APIGEE_NO_KEY="${REQ_STATUS}"
echo "    Apigee missing key rejection: ${APIGEE_NO_KEY} (expected 401)"
if [ "${APIGEE_NO_KEY}" != "401" ]; then
    record_failure "Apigee missing key rejection returned ${APIGEE_NO_KEY} (expected 401)"
fi

# 2c. Valid Key Inventory Query & In-Stock Selection
run_request "GET Apigee /ata/inventory/v1/items" -k "https://${APIGEE_HOST}/ata/inventory/v1/items" -H "x-api-key: ${API_KEY}"
APIGEE_ITEMS="${REQ_STATUS}"
ITEMS_BODY="${REQ_BODY}"
echo "    Apigee GET /items with key: ${APIGEE_ITEMS} (expected 200)"
if [ "${APIGEE_ITEMS}" != "200" ]; then
    record_failure "Apigee GET /items returned ${APIGEE_ITEMS} (expected 200)"
fi

TARGET_ITEM_ID=$(python3 -c '
import sys, json
try:
    items = json.loads(sys.stdin.read())
    in_stock = [i["id"] for i in items if isinstance(i, dict) and i.get("stock", 0) > 0]
    print(in_stock[0] if in_stock else "item-101")
except Exception:
    print("item-101")
' <<< "${ITEMS_BODY}")

# 2d. Valid Key Order Creation (End-to-End)
run_request "POST Apigee /ata/orders/v1/orders" -k -X POST "https://${APIGEE_HOST}/ata/orders/v1/orders" \
    -H "x-api-key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"item_id\":\"${TARGET_ITEM_ID}\",\"quantity\":1,\"customer_id\":\"verifier\"}"
APIGEE_ORDER="${REQ_STATUS}"
ORDER_BODY="${REQ_BODY}"

if [ "${APIGEE_ORDER}" = "200" ]; then
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200 - order placed for ${TARGET_ITEM_ID})"
elif [ "${APIGEE_ORDER}" = "400" ] && echo "${ORDER_BODY}" | grep -iq "stock"; then
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (accepted 400 - inventory out of stock for ${TARGET_ITEM_ID})"
else
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200 or 400 out-of-stock)"
    record_failure "Apigee POST /orders returned unexpected code ${APIGEE_ORDER}: ${ORDER_BODY}"
fi

# 2e. Admin Route Rejection (Security Hardened)
run_request "POST Apigee /ata/inventory/v1/admin/reset" -k -X POST "https://${APIGEE_HOST}/ata/inventory/v1/admin/reset" -H "x-api-key: ${API_KEY}"
APIGEE_ADMIN_INV="${REQ_STATUS}"
run_request "POST Apigee /ata/orders/v1/admin/reset" -k -X POST "https://${APIGEE_HOST}/ata/orders/v1/admin/reset" -H "x-api-key: ${API_KEY}"
APIGEE_ADMIN_ORD="${REQ_STATUS}"
echo "    Apigee admin inventory rejection: ${APIGEE_ADMIN_INV} (expected 403)"
if [ "${APIGEE_ADMIN_INV}" != "403" ]; then
    record_failure "Apigee admin inventory rejection returned ${APIGEE_ADMIN_INV} (expected 403)"
fi

echo "    Apigee admin orders rejection: ${APIGEE_ADMIN_ORD} (expected 403)"
if [ "${APIGEE_ADMIN_ORD}" != "403" ]; then
    record_failure "Apigee admin orders rejection returned ${APIGEE_ADMIN_ORD} (expected 403)"
fi

report_alert_risk

if [ "${FAILURES}" -gt 0 ]; then
    echo "=== Verification Failed: ${FAILURES} check(s) failed ===" >&2
    exit 1
fi

echo "=== Verification Complete: All Checks Passed ==="
exit 0
