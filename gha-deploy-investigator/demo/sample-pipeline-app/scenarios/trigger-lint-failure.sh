#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_FILE="${APP_DIR}/app/main.py"

echo "=================================================="
echo "Injecting Scenario 1: Lint & Code Style Violation"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

# Ensure backup exists
if [[ ! -f "${TARGET_FILE}.bak" ]]; then
    cp "${TARGET_FILE}" "${TARGET_FILE}.bak"
fi

# Prepend unused imports and syntax/style violations flagged by ruff (F401, E701)
TMP_FILE="$(mktemp)"
cat << 'EOF' > "${TMP_FILE}"
# INJECTED FAULT: Unused imports and syntax violation for demo failure
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
echo "Run 'ruff check app/' to verify failure."
