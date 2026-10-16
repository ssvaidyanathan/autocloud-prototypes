#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_FILE="${APP_DIR}/tests/test_main.py"

echo "=================================================="
echo "Injecting Scenario 2: Broken Unit Test Assertion"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

# Ensure backup exists
if [[ ! -f "${TARGET_FILE}.bak" ]]; then
    cp "${TARGET_FILE}" "${TARGET_FILE}.bak"
fi

# Append a deliberately failing unit test assertion
cat << 'EOF' >> "${TARGET_FILE}"


def test_order_discount_calculation_failure(client) -> None:
    """INJECTED FAULT: Broken business logic assertion for demo failure."""
    # Expected order total calculation regression
    expected_discounted_total = 100.00
    actual_calculated_total = 75.50
    assert actual_calculated_total == expected_discounted_total, (
        f"Order discount mismatch: expected ${expected_discounted_total:.2f}, "
        f"got ${actual_calculated_total:.2f}"
    )
EOF

echo "Injected failing assertion 'test_order_discount_calculation_failure' into ${TARGET_FILE}."
echo "Run 'pytest tests/ -v' to verify failure."
