#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEFAULT_APP_DIR="$(cd "${SCRIPT_DIR}/../sample-pipeline-app" && pwd)"

DO_COMMIT=false
APP_DIR=""

for arg in "$@"; do
    if [[ "$arg" == "--commit" ]]; then
        DO_COMMIT=true
    elif [[ -z "$APP_DIR" && "$arg" != -* ]]; then
        APP_DIR="$arg"
    fi
done

APP_DIR="${APP_DIR:-${DEFAULT_APP_DIR}}"
TARGET_FILE="${APP_DIR}/scripts/deploy.sh"

echo "=================================================="
echo "Injecting Scenario 4: Runtime OOM / Deploy Crash"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

if [[ ! -f "${TARGET_FILE}" ]]; then
    echo "ERROR: Target file ${TARGET_FILE} not found." >&2
    exit 1
fi

sed -i 's/MEMORY="${MEMORY:-512Mi}"/MEMORY="${MEMORY:-256Mi}"/' "${TARGET_FILE}"
sed -i 's/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-CACHE_PREWARM_BUFFER_MB=2048}"/' "${TARGET_FILE}"

echo "Injected production cache buffer parameters into ${TARGET_FILE}:"
echo "  - MEMORY: 256Mi"
echo "  - EXTRA_ENV_VARS: CACHE_PREWARM_BUFFER_MB=2048"

if [[ "${DO_COMMIT}" == "true" ]]; then
    if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${APP_DIR}" add scripts/deploy.sh
        git -C "${APP_DIR}" commit -m "feat(cache): pre-warm order history cache on startup"
        echo "Created git commit in ${APP_DIR}: 'feat(cache): pre-warm order history cache on startup'"
    else
        echo "Warning: --commit requested but ${APP_DIR} is not a git repository."
    fi
fi

echo "Run './scripts/deploy.sh' in ${APP_DIR} to verify deploy failure."
