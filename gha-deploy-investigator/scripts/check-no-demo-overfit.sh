#!/usr/bin/env bash
set -euo pipefail

# Guard script to ensure no demo identifiers, scenario literals, or canned answers
# exist in agent/app/ or agent/skills/ (Python source code, prompt templates, and skills).

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "${ROOT_DIR}"

echo "========================================================"
echo "Checking for forbidden demo identifiers in agent/app and agent/skills..."
echo "========================================================"

TARGET_DIRS=("agent/app" "agent/skills")

# Forbidden patterns:
# - Scenario [0-9]
# - sample-pipeline-app
# - CACHE_PREWARM_BUFFER_MB / cache_prewarm / pre-warm / prewarm / buffer / 2048 / 256Mi / MEMORY=
# - discount_calculation / discount / 75.5
# - fake-order-service / shop-api / inventory-svc / PAYMENT_GATEWAY_KEY / sync-tenant-data
# - StarletteDeprecationWarning / StarletteDeprecation
# - actAs / actas / serviceAccountUser / iam.serviceaccounts
# - E402 / E702 / F401 as expected answers
FORBIDDEN_PATTERN="sample-pipeline-app|discount_calculation|fake-order-service|shop-api|inventory-svc|PAYMENT_GATEWAY_KEY|sync-tenant-data|[Ss]cenario [0-9]|pre-?warm|Pre-warming|cache-warmup|2048|256Mi|MEMORY=|StarletteDeprecation|discount|75\.5|actAs|actas|serviceAccountUser|iam\.serviceaccounts|roles/iam|CACHE_PREWARM|E402|E702|F401"

MATCHES=$(grep -rnIE "${FORBIDDEN_PATTERN}" "${TARGET_DIRS[@]}" --include="*.py" --include="*.txt" --include="*.md" || true)

if [ -n "${MATCHES}" ]; then
    echo "ERROR: Forbidden demo identifier or scenario overfit found in ${TARGET_DIRS[*]}:" >&2
    echo "${MATCHES}" >&2
    echo "Remove all hardcoded demo answers, service names, and scenario numbers from agent/app/ and agent/skills/." >&2
    exit 1
fi

echo "[PASS] Zero demo identifiers or scenario overfit found in ${TARGET_DIRS[*]}."

# Run AST guard check against post-LLM root cause template and remediation overrides
python3 "${ROOT_DIR}/scripts/check-no-rootcause-overrides.py"

echo "SUCCESS: De-overfit guard check passed cleanly."
