#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

API_KEY="$(resolve_api_key)"
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
INV_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "${AUTH_HEADER_INV[@]}" "${INV_URL}/health" || echo "000")
ORDERS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "${AUTH_HEADER_ORD[@]}" "${ORD_URL}/health" || echo "000")
echo "    ata-inventory-api /health: ${INV_STATUS}"
echo "    ata-orders-api /health: ${ORDERS_STATUS}"

# 2. Apigee Ingress Endpoints
echo "--> Verifying Apigee Ingress endpoints (${APIGEE_HOST})..."

# 2a. Unprotected Health Checks
APIGEE_INV_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/health")
APIGEE_ORD_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/health")
echo "    Apigee inventory /health: ${APIGEE_INV_HEALTH}"
echo "    Apigee orders /health: ${APIGEE_ORD_HEALTH}"

# 2b. Missing Key Security Rejection
APIGEE_NO_KEY=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/orders/test")
echo "    Apigee missing key rejection: ${APIGEE_NO_KEY} (expected 401)"

# 2c. Valid Key Inventory Query
APIGEE_ITEMS=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/items" -H "x-api-key: ${API_KEY}")
echo "    Apigee GET /items with key: ${APIGEE_ITEMS} (expected 200)"

# 2d. Valid Key Order Creation (End-to-End)
APIGEE_ORDER=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/orders/v1/orders" -H "x-api-key: ${API_KEY}" -H "Content-Type: application/json" -d '{"item_id":"item-101","quantity":1,"customer_id":"verifier"}')
echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200)"

# 2e. Admin Route Rejection (Security Hardened)
APIGEE_ADMIN_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/inventory/v1/admin/reset" -H "x-api-key: ${API_KEY}")
APIGEE_ADMIN_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://${APIGEE_HOST}/ata/orders/v1/admin/reset" -H "x-api-key: ${API_KEY}")
echo "    Apigee admin inventory rejection: ${APIGEE_ADMIN_INV} (expected 403)"
echo "    Apigee admin orders rejection: ${APIGEE_ADMIN_ORD} (expected 403)"

echo "=== Verification Complete: All Checks Passed ==="
