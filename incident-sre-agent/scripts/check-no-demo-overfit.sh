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
# - shop-api
# - inventory-svc
# - PAYMENT_GATEWAY_KEY
# - sync-tenant-data
# - Scenario [0-9]
# - 1024 MiB (or 1057 mib)
# - checkout (literal)
# - catalog (literal)
# - delay injection (literal)
FORBIDDEN_PATTERN="shop-api|inventory-svc|PAYMENT_GATEWAY_KEY|sync-tenant-data|[Ss]cenario [0-9]|1024 [Mm]i[Bb]|1024 [Mm][Bb]|1057 [Mm]i[Bb]|\bcheckout\b|\bcatalog\b|[Dd]elay injection"

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

# Run AST guard check against demo log hints in demo/shop-api/shop_app.py
python3 "${ROOT_DIR}/scripts/check-no-demo-log-hints.py"

echo "SUCCESS: De-overfit guard check passed cleanly."
