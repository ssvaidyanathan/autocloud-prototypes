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
TARGET_FILE="${APP_DIR}/tests/test_main.py"

echo "=================================================="
echo "Injecting Scenario 2: Unit Test Calculation Regression"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

if [[ ! -f "${TARGET_FILE}" ]]; then
    echo "ERROR: Target file ${TARGET_FILE} not found." >&2
    exit 1
fi

cat << 'EOF' >> "${TARGET_FILE}"


def test_customer_tier_discount_calculation(client) -> None:
    """Validate customer volume discount tiered calculations."""
    expected_discounted_total = 100.00
    actual_calculated_total = 75.50
    assert actual_calculated_total == expected_discounted_total, (
        f"Order discount tier mismatch: expected ${expected_discounted_total:.2f}, "
        f"got ${actual_calculated_total:.2f}"
    )
EOF

echo "Added 'test_customer_tier_discount_calculation' into ${TARGET_FILE}."

if [[ "${DO_COMMIT}" == "true" ]]; then
    if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
        git -C "${APP_DIR}" add tests/test_main.py
        git -C "${APP_DIR}" commit -m "feat(pricing): update order discount tier calculation"
        echo "Created git commit in ${APP_DIR}: 'feat(pricing): update order discount tier calculation'"
    else
        echo "Warning: --commit requested but ${APP_DIR} is not a git repository."
    fi
fi

echo "Run 'pytest tests/ -v' in ${APP_DIR} to verify failure."
