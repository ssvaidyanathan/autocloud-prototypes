#!/usr/bin/env bash
set -euo pipefail

URL="${1:-${SHOP_API_URL:-http://localhost:8080}}"

echo "Resetting all injected faults on $URL..."
curl -fsSL -X POST "$URL/fault/reset" \
  -H "Content-Type: application/json"
echo ""
echo "All faults have been reset to normal baseline operation."
