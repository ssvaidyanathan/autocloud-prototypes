#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_FILE="${APP_DIR}/scripts/deploy.sh"

echo "=================================================="
echo "Injecting Scenario 4: Runtime OOM / Deploy Crash"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

# Ensure backup exists
if [[ ! -f "${TARGET_FILE}.bak" ]]; then
    cp "${TARGET_FILE}" "${TARGET_FILE}.bak"
fi

# Modify deploy.sh to pass DEMO_ALLOCATE_OOM and constrained memory limit (128Mi)
sed -i 's/MEMORY="${MEMORY:-512Mi}"/MEMORY="${MEMORY:-128Mi}"/' "${TARGET_FILE}"
sed -i 's/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-DEMO_ALLOCATE_OOM=true}"/' "${TARGET_FILE}"

echo "Injected OOM fault parameters into ${TARGET_FILE}:"
echo "  - MEMORY: 128Mi"
echo "  - EXTRA_ENV_VARS: DEMO_ALLOCATE_OOM=true"
echo "Run 'CI_SIMULATION=true ./scripts/deploy.sh' to verify deploy failure."
