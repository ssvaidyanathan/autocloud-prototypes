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
TARGET_FILE="${APP_DIR}/app/main.py"

echo "=================================================="
echo "Injecting Scenario 1: Lint & Code Style Violation"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

if [[ ! -f "${TARGET_FILE}" ]]; then
    echo "ERROR: Target file ${TARGET_FILE} not found." >&2
    exit 1
fi

TMP_FILE="$(mktemp)"
cat << 'EOF' > "${TMP_FILE}"
import math


def format_order_currency(value: float) -> str:
    formatted_val = round(value, 2); return f"${formatted_val:.2f}"

EOF
cat "${TARGET_FILE}" >> "${TMP_FILE}"
mv "${TMP_FILE}" "${TARGET_FILE}"

echo "Added currency formatting function into ${TARGET_FILE} with style violations:"
echo "  - Unused imports (F401)"
echo "  - Multiple statements on single line (E702)"

if [[ "${DO_COMMIT}" == "true" ]]; then
    if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${APP_DIR}" add app/main.py
        git -C "${APP_DIR}" commit -m "refactor(orders): add order export and currency formatting helper"
        echo "Created git commit in ${APP_DIR}: 'refactor(orders): add order export and currency formatting helper'"
    else
        echo "Warning: --commit requested but ${APP_DIR} is not a git repository."
    fi
fi

echo "Run 'ruff check app/' in ${APP_DIR} to verify failure."
