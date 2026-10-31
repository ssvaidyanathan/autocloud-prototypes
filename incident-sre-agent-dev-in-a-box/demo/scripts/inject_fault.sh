#!/usr/bin/env bash
set -euo pipefail

SCENARIO="${1:-}"
URL="${SHOP_API_URL:-http://localhost:8080}"

if [[ -z "$SCENARIO" || ! "$SCENARIO" =~ ^[1-5]$ ]]; then
  echo "Usage: $0 <scenario-id (1-5)> [shop-api-url]"
  echo "  1: Error spike (~40% checkout exceptions)"
  echo "  2: Latency spike (3-8s artificial delay)"
  echo "  3: Bad release (missing required PAYMENT_GATEWAY_KEY, 503s)"
  echo "  4: Memory leak (progressive memory exhaustion towards OOM)"
  echo "  5: Dependency failure (inventory-svc 403 Forbidden)"
  exit 1
fi

if [[ $# -ge 2 ]]; then
  URL="$2"
fi

echo "Injecting fault Scenario $SCENARIO against $URL..."
curl -fsSL -X POST "$URL/fault/inject" \
  -H "Content-Type: application/json" \
  -d "{\"scenario\": $SCENARIO}"
echo ""
echo "Fault injected successfully."
