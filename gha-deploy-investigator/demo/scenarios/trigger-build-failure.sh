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
TARGET_FILE="${APP_DIR}/Dockerfile"

echo "=================================================="
echo "Injecting Scenario 3: Broken Dockerfile Build"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

if [[ ! -f "${TARGET_FILE}" ]]; then
    echo "ERROR: Target file ${TARGET_FILE} not found." >&2
    exit 1
fi

cat << 'EOF' >> "${TARGET_FILE}"

# cache-warmup configuration asset for startup pre-warming
COPY config/cache-warmup.json /app/config/cache-warmup.json
EOF

echo "Injected missing COPY asset instruction into ${TARGET_FILE}."

if [[ "${DO_COMMIT}" == "true" ]]; then
    if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${APP_DIR}" add Dockerfile
        git -C "${APP_DIR}" commit -m "chore(deps): configure cache warmup asset packaging"
        echo "Created git commit in ${APP_DIR}: 'chore(deps): configure cache warmup asset packaging'"
    else
        echo "Warning: --commit requested but ${APP_DIR} is not a git repository."
    fi
fi

echo "Run 'docker build -t sample-pipeline-app:test .' in ${APP_DIR} to verify failure."
