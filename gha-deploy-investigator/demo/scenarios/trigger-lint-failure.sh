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
# INJECTED DEFECT: Unused imports and syntax violation for lint failure
import non_existent_module  # noqa: F401
import os, sys, math  # unused imports

def faulty_syntax_indent():
    x = 1; y = 2  # multiple statements on one line (E701)
    return x + y

EOF
cat "${TARGET_FILE}" >> "${TMP_FILE}"
mv "${TMP_FILE}" "${TARGET_FILE}"

echo "Injected lint violations into ${TARGET_FILE}:"
echo "  - Unused imports (F401)"
echo "  - Multiple statements on single line (E701)"

if [[ "${DO_COMMIT}" == "true" ]]; then
    if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${APP_DIR}" add app/main.py
        git -C "${APP_DIR}" commit -m "refactor(orders): add order export and audit formatting"
        echo "Created git commit in ${APP_DIR}: 'refactor(orders): add order export and audit formatting'"
    else
        echo "Warning: --commit requested but ${APP_DIR} is not a git repository."
    fi
fi

echo "Run 'ruff check app/' in ${APP_DIR} to verify failure."
