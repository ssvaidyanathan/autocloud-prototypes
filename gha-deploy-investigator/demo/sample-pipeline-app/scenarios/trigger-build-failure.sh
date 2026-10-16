#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_FILE="${APP_DIR}/Dockerfile"

echo "=================================================="
echo "Injecting Scenario 3: Broken Dockerfile Build"
echo "Target File: ${TARGET_FILE}"
echo "=================================================="

# Ensure backup exists
if [[ ! -f "${TARGET_FILE}.bak" ]]; then
    cp "${TARGET_FILE}" "${TARGET_FILE}.bak"
fi

# Append invalid COPY command that fails during build context resolution
cat << 'EOF' >> "${TARGET_FILE}"

# INJECTED FAULT: Non-existent file in build context
COPY missing_asset_configuration.json /app/missing_asset_configuration.json
EOF

echo "Injected failing COPY instruction into ${TARGET_FILE}."
echo "Run 'docker build -t sample-pipeline-app:test .' to verify failure."
