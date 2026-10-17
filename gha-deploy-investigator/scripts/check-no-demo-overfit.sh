#!/usr/bin/env bash
set -euo pipefail

# Guard script to ensure no demo identifiers, scenario literals, or canned answers
# exist in agent/app/ (Python source code and prompt templates).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "========================================================"
echo "Checking for forbidden demo identifiers in agent/app/..."
echo "========================================================"

TARGET_DIR="agent/app"

# Forbidden patterns:
# - Scenario [0-9]
# - sample-pipeline-app
# - CACHE_PREWARM_BUFFER_MB
# - discount_calculation
# - fake-order-service
# - shop-api / inventory-svc
# - PAYMENT_GATEWAY_KEY
# - sync-tenant-data
FORBIDDEN_PATTERN="sample-pipeline-app|CACHE_PREWARM_BUFFER_MB|discount_calculation|fake-order-service|shop-api|inventory-svc|PAYMENT_GATEWAY_KEY|sync-tenant-data|[Ss]cenario [0-9]"

MATCHES=$(grep -rnE "${FORBIDDEN_PATTERN}" "${TARGET_DIR}" --include="*.py" --include="*.txt" || true)

if [ -n "${MATCHES}" ]; then
    echo "ERROR: Forbidden demo identifier or scenario overfit found in ${TARGET_DIR}:" >&2
    echo "${MATCHES}" >&2
    echo "Remove all hardcoded demo answers, service names, and scenario numbers from agent/app/." >&2
    exit 1
fi

echo "[PASS] Zero demo identifiers or scenario overfit found in ${TARGET_DIR}."

# Run AST guard check against post-LLM root cause template overrides
python3 "${ROOT_DIR}/scripts/check-no-rootcause-overrides.py"

echo "SUCCESS: De-overfit guard check passed cleanly."
