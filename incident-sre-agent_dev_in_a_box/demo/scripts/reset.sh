#!/usr/bin/env bash
set -euo pipefail

URL="${1:-${SHOP_API_URL:-http://localhost:8080}}"

if [ -n "${INVENTORY_SVC_URL:-}" ]; then
  INVENTORY_URL="$INVENTORY_SVC_URL"
elif [ -n "${INVENTORY_SERVICE_URL:-}" ]; then
  INVENTORY_URL="$INVENTORY_SERVICE_URL"
elif [[ "$URL" == *":8080"* ]]; then
  INVENTORY_URL="${URL//:8080/:8081}"
elif [[ "$URL" == *"shop-api"* ]]; then
  INVENTORY_URL="${URL//shop-api/inventory-svc}"
else
  INVENTORY_URL="http://localhost:8081"
fi

echo "Resetting all injected faults on $URL..."
curl -fsSL -X POST "$URL/fault/reset" \
  -H "Content-Type: application/json"
echo ""

echo "Restocking inventory on $INVENTORY_URL..."
curl -fsSL -X POST "$INVENTORY_URL/inventory/restock" \
  -H "Content-Type: application/json"
echo ""

echo "All faults have been reset to normal baseline operation."
