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

echo "=================================================="
echo "Resetting Demo Scenarios to Pristine Baseline State"
echo "Target App Directory: ${APP_DIR}"
echo "=================================================="

# 1. If inside a standalone git repository, handle git revert / checkout
if [[ -d "${APP_DIR}/.git" ]]; then
    # If the user committed scenarios, check if HEAD is a scenario commit
    if [[ "${DO_COMMIT}" == "true" ]]; then
        commit_msg="$(git -C "${APP_DIR}" log -1 --pretty=%B 2>/dev/null || true)"
        if [[ "${commit_msg}" =~ (refactor\(orders\)|feat\(pricing\)|chore\(deps\)|feat\(cache\)) ]]; then
            git -C "${APP_DIR}" revert HEAD --no-edit -m 1 2>/dev/null || git -C "${APP_DIR}" revert HEAD --no-edit 2>/dev/null || true
            echo "Reverted scenario commit in ${APP_DIR}."
        fi
    fi

    # Checkout clean copies of modified files from git index/HEAD
    git -C "${APP_DIR}" checkout -- \
        "${APP_DIR}/app/main.py" \
        "${APP_DIR}/tests/test_main.py" \
        "${APP_DIR}/Dockerfile" \
        "${APP_DIR}/scripts/deploy.sh" 2>/dev/null || true
fi

# 2. Idempotent programmatic cleanup in case not in git or uncommitted changes
DEPLOY_SH="${APP_DIR}/scripts/deploy.sh"
if [[ -f "${DEPLOY_SH}" ]]; then
    sed -i 's/MEMORY="${MEMORY:-256Mi}"/MEMORY="${MEMORY:-512Mi}"/' "${DEPLOY_SH}"
    sed -i 's/MEMORY="${MEMORY:-128Mi}"/MEMORY="${MEMORY:-512Mi}"/' "${DEPLOY_SH}"
    sed -i 's/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-CACHE_PREWARM_BUFFER_MB=2048}"/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"/' "${DEPLOY_SH}"
    sed -i 's/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-DEMO_ALLOCATE_OOM=true}"/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"/' "${DEPLOY_SH}"
fi

DOCKERFILE="${APP_DIR}/Dockerfile"
if [[ -f "${DOCKERFILE}" ]]; then
    sed -i '/[Cc]ache.*warmup/d' "${DOCKERFILE}"
    sed -i '/missing_asset_configuration/d' "${DOCKERFILE}"
    python3 -c "
import sys
p = sys.argv[1]
with open(p, 'r') as f:
    c = f.read().rstrip() + '\n'
with open(p, 'w') as f:
    f.write(c)
" "${DOCKERFILE}" 2>/dev/null || true
fi

TEST_MAIN="${APP_DIR}/tests/test_main.py"
if [[ -f "${TEST_MAIN}" ]]; then
    python3 -c "
import sys
p = sys.argv[1]
with open(p, 'r') as f:
    content = f.read()
if 'test_order_discount_calculation_failure' in content:
    idx = content.find('def test_order_discount_calculation_failure')
    clean = content[:idx].rstrip() + '\n'
    with open(p, 'w') as f:
        f.write(clean)
" "${TEST_MAIN}" 2>/dev/null || true
fi

MAIN_PY="${APP_DIR}/app/main.py"
if [[ -f "${MAIN_PY}" ]]; then
    python3 -c "
import sys
p = sys.argv[1]
with open(p, 'r') as f:
    lines = f.readlines()
clean = [l for l in lines if 'INJECTED DEFECT' not in l and 'INJECTED FAULT' not in l and 'faulty_syntax_indent' not in l and 'x = 1; y = 2' not in l and 'return x + y' not in l and 'import non_existent_module' not in l and 'import os, sys, math' not in l]
with open(p, 'w') as f:
    f.writelines(clean)
" "${MAIN_PY}" 2>/dev/null || true
fi

# 3. Clean up any leftover temporary files or backups
find "${APP_DIR}" \( -name "*.bak" -o -name "*.orig" \) -delete 2>/dev/null || true

echo "SUCCESS: Demo app restored to clean pristine state."
