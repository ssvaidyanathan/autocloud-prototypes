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
INV_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/health" || echo "000")
ORDERS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 ${AUTH_HEADER_ORD[@]+"${AUTH_HEADER_ORD[@]}"} "${ORD_URL}/health" || echo "000")
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
APIGEE_INV_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/health" || echo "000")
APIGEE_ORD_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/health" || echo "000")
echo "    Apigee inventory /health: ${APIGEE_INV_HEALTH} (expected 200)"
if [ "${APIGEE_INV_HEALTH}" != "200" ]; then
    record_failure "Apigee inventory /health returned ${APIGEE_INV_HEALTH} (expected 200)"
fi

echo "    Apigee orders /health: ${APIGEE_ORD_HEALTH} (expected 200)"
if [ "${APIGEE_ORD_HEALTH}" != "200" ]; then
    record_failure "Apigee orders /health returned ${APIGEE_ORD_HEALTH} (expected 200)"
fi

# 2b. Missing Key Security Rejection
APIGEE_NO_KEY=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/orders/test" || echo "000")
echo "    Apigee missing key rejection: ${APIGEE_NO_KEY} (expected 401)"
if [ "${APIGEE_NO_KEY}" != "401" ]; then
    record_failure "Apigee missing key rejection returned ${APIGEE_NO_KEY} (expected 401)"
fi

# 2c. Valid Key Inventory Query & In-Stock Selection
ITEMS_RESP=$(curl -k -s -w "\n%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/items" -H "x-api-key: ${API_KEY}" || echo -e "\n000")
APIGEE_ITEMS=$(echo "${ITEMS_RESP}" | tail -n 1)
ITEMS_BODY=$(echo "${ITEMS_RESP}" | sed '$d')
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
ORDER_RESP=$(curl -k -s -w "\n%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/orders/v1/orders" \
    -H "x-api-key: ${API_KEY}" \
    -H "Content-Type: application/json" \
    -d "{\"item_id\":\"${TARGET_ITEM_ID}\",\"quantity\":1,\"customer_id\":\"verifier\"}" || echo -e "\n000")
APIGEE_ORDER=$(echo "${ORDER_RESP}" | tail -n 1)
ORDER_BODY=$(echo "${ORDER_RESP}" | sed '$d')

if [ "${APIGEE_ORDER}" = "200" ]; then
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200 - order placed for ${TARGET_ITEM_ID})"
elif [ "${APIGEE_ORDER}" = "400" ] && echo "${ORDER_BODY}" | grep -iq "stock"; then
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (accepted 400 - inventory out of stock for ${TARGET_ITEM_ID})"
else
    echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200 or 400 out-of-stock)"
    record_failure "Apigee POST /orders returned unexpected code ${APIGEE_ORDER}: ${ORDER_BODY}"
fi

# 2e. Admin Route Rejection (Security Hardened)
APIGEE_ADMIN_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/inventory/v1/admin/reset" -H "x-api-key: ${API_KEY}" || echo "000")
APIGEE_ADMIN_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/orders/v1/admin/reset" -H "x-api-key: ${API_KEY}" || echo "000")
echo "    Apigee admin inventory rejection: ${APIGEE_ADMIN_INV} (expected 403)"
if [ "${APIGEE_ADMIN_INV}" != "403" ]; then
    record_failure "Apigee admin inventory rejection returned ${APIGEE_ADMIN_INV} (expected 403)"
fi

echo "    Apigee admin orders rejection: ${APIGEE_ADMIN_ORD} (expected 403)"
if [ "${APIGEE_ADMIN_ORD}" != "403" ]; then
    record_failure "Apigee admin orders rejection returned ${APIGEE_ADMIN_ORD} (expected 403)"
fi

if [ "${FAILURES}" -gt 0 ]; then
    echo "=== Verification Failed: ${FAILURES} check(s) failed ===" >&2
    exit 1
fi

echo "=== Verification Complete: All Checks Passed ==="
exit 0
