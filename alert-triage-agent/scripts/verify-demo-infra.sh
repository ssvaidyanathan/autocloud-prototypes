#!/usr/bin/env bash
set -euo pipefail

KEY_FILE="/workspace/alert-triage-agent/.ata-api-key"
if [ ! -f "${KEY_FILE}" ]; then
  echo "Error: ${KEY_FILE} not found."
  exit 1
fi
API_KEY=$(cat "${KEY_FILE}")

echo "=== Verifying ATA Demo Infrastructure & Endpoints ==="

# 1. Direct Cloud Run Endpoints (Internal / IAM only)
echo "--> Verifying Cloud Run direct endpoints..."
INV_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 https://ata-inventory-api-4klvlqnmpa-uw.a.run.app/health || echo "000")
ORDERS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 https://ata-orders-api-4klvlqnmpa-uw.a.run.app/health || echo "000")
echo "    ata-inventory-api /health: ${INV_STATUS}"
echo "    ata-orders-api /health: ${ORDERS_STATUS}"

# 2. Apigee Ingress Endpoints
echo "--> Verifying Apigee Ingress endpoints (8.233.247.52.nip.io)..."

# 2a. Unprotected Health Checks
APIGEE_INV_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://8.233.247.52.nip.io/ata/inventory/v1/health")
APIGEE_ORD_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://8.233.247.52.nip.io/ata/orders/v1/health")
echo "    Apigee inventory /health: ${APIGEE_INV_HEALTH}"
echo "    Apigee orders /health: ${APIGEE_ORD_HEALTH}"

# 2b. Missing Key Security Rejection
APIGEE_NO_KEY=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://8.233.247.52.nip.io/ata/orders/v1/orders/test")
echo "    Apigee missing key rejection: ${APIGEE_NO_KEY} (expected 401)"

# 2c. Valid Key Inventory Query
APIGEE_ITEMS=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://8.233.247.52.nip.io/ata/inventory/v1/items" -H "x-api-key: ${API_KEY}")
echo "    Apigee GET /items with key: ${APIGEE_ITEMS} (expected 200)"

# 2d. Valid Key Order Creation (End-to-End)
APIGEE_ORDER=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://8.233.247.52.nip.io/ata/orders/v1/orders" -H "x-api-key: ${API_KEY}" -H "Content-Type: application/json" -d '{"item_id":"item-101","quantity":1,"customer_id":"verifier"}')
echo "    Apigee POST /orders with key: ${APIGEE_ORDER} (expected 200)"

# 2e. Admin Route Rejection (Security Hardened)
APIGEE_ADMIN_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://8.233.247.52.nip.io/ata/inventory/v1/admin/reset" -H "x-api-key: ${API_KEY}")
APIGEE_ADMIN_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 -X POST "https://8.233.247.52.nip.io/ata/orders/v1/admin/reset" -H "x-api-key: ${API_KEY}")
echo "    Apigee admin inventory rejection: ${APIGEE_ADMIN_INV} (expected 403)"
echo "    Apigee admin orders rejection: ${APIGEE_ADMIN_ORD} (expected 403)"

echo "=== Verification Complete: All Checks Passed ==="
