#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=================================================="
echo "Resetting Demo Scenarios to Pristine Clean State"
echo "Target App Directory: ${APP_DIR}"
echo "=================================================="

# 1. Restore backup copies if they exist
for file in "${APP_DIR}/app/main.py" "${APP_DIR}/tests/test_main.py" "${APP_DIR}/Dockerfile" "${APP_DIR}/scripts/deploy.sh"; do
    if [[ -f "${file}.bak" ]]; then
        cp "${file}.bak" "${file}"
        rm -f "${file}.bak"
        echo "Restored ${file} from backup."
    fi
done

# 2. If within a git repository, checkout clean files
if git -C "${APP_DIR}" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    git -C "${APP_DIR}" checkout -- \
        "${APP_DIR}/app/main.py" \
        "${APP_DIR}/tests/test_main.py" \
        "${APP_DIR}/Dockerfile" \
        "${APP_DIR}/scripts/deploy.sh" 2>/dev/null || true
fi

# 3. Explicit fallback replacements in case git checkout didn't track new files
DEPLOY_SH="${APP_DIR}/scripts/deploy.sh"
if [[ -f "${DEPLOY_SH}" ]]; then
    sed -i 's/MEMORY="${MEMORY:-128Mi}"/MEMORY="${MEMORY:-512Mi}"/' "${DEPLOY_SH}"
    sed -i 's/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-DEMO_ALLOCATE_OOM=true}"/EXTRA_ENV_VARS="${EXTRA_ENV_VARS:-}"/' "${DEPLOY_SH}"
fi

DOCKERFILE="${APP_DIR}/Dockerfile"
if [[ -f "${DOCKERFILE}" ]]; then
    sed -i '/missing_asset_configuration.json/d' "${DOCKERFILE}"
fi

# Clean up any leftover temporary files
rm -f "${APP_DIR}/app/main.py.bak" "${APP_DIR}/tests/test_main.py.bak" "${APP_DIR}/Dockerfile.bak" "${APP_DIR}/scripts/deploy.sh.bak"
rm -f "${APP_DIR}/non_existent_file.txt"

echo "SUCCESS: Demo app restored to clean pristine state."
