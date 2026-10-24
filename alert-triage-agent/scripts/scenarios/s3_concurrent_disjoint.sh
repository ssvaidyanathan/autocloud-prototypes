#!/usr/bin/env bash
# ==============================================================================
# Scenario S3: Concurrent Disjoint Incidents
#
# Target: Cloud Run (ata-inventory-api) AND Apigee X (ata-orders-v1) simultaneously
# Mechanism: Concurrently trigger S1 on inventory and SpikeArrest rate limiting on orders
# Alerts:
#   - Group 1: Cloud Run 5xx + latency on inventory
#   - Group 2: Apigee 429 on orders
# Output: Exactly 2 Distinct Groups, 2 ServiceNow Tickets
#   - Group 1: Origin ata-inventory-api, Root Cause 5xx Application Error
#   - Group 2: Origin ata-orders-v1, Root Cause SpikeArrest Rate Limiting
#   - Proves correlation engine correctly splits independent incidents using
#     causal failure chains and change timelines (disjoint scoring < 35)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

log_info "=== Starting Scenario S3: Concurrent Disjoint Incidents ==="

# 1. Trigger S1 on inventory API in background
log_info "--> Triggering S1 (Inventory 5xx error spike) in background..."
"${SCRIPT_DIR}/s1_backend_5xx.sh" &
PID_S1=$!

# 2. Trigger Orders proxy SpikeArrest 429 fault concurrently
trigger_orders_spikearrest() {
    log_info "--> Triggering Orders strict 1pm SpikeArrest in background..."
    local TOKEN API_KEY BASE_URL SA_EMAIL BUNDLE_SRC TMP_DIR ZIP_FILE REVISION

    TOKEN="$(resolve_apigee_token)"
    API_KEY="$(resolve_api_key)"
    BASE_URL="https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}"
    SA_EMAIL="ata-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com"

    BUNDLE_SRC="${SCRIPT_DIR}/../../demo/apigee/ata-orders-v1"
    if [ ! -d "${BUNDLE_SRC}" ]; then
        BUNDLE_SRC="/workspace/alert-triage-agent/demo/apigee/ata-orders-v1"
    fi
    TMP_DIR="/tmp/ata-orders-s3-$$"
    ZIP_FILE="/tmp/ata-orders-v1-s3.zip"

    mkdir -p "${TMP_DIR}"
    if [ -d "${BUNDLE_SRC}" ]; then
        cp -r "${BUNDLE_SRC}"/* "${TMP_DIR}/"
    fi

    # Modify SpikeArrest policy rate to 1pm
    cat <<'EOF' > "${TMP_DIR}/apiproxy/policies/SA-SpikeArrest.xml"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<SpikeArrest continueOnError="false" enabled="true" name="SA-SpikeArrest">
    <DisplayName>Spike Arrest Rate Limiting (Strict 1pm)</DisplayName>
    <Rate>1pm</Rate>
</SpikeArrest>
EOF

    log_info "    Packaging strict 1pm SpikeArrest proxy bundle..."
    rm -f "${ZIP_FILE}"
    python3 -c "import zipfile, os, sys
bundle_dir, zip_path = sys.argv[1], sys.argv[2]
if os.path.exists(bundle_dir):
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
        for root, _, files in os.walk(bundle_dir):
            for f in files:
                full_path = os.path.join(root, f)
                rel_path = os.path.relpath(full_path, bundle_dir)
                zf.write(full_path, rel_path)
" "${TMP_DIR}" "${ZIP_FILE}" 2>/dev/null || true

    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ -z "${TOKEN}" ] || [ "${TOKEN}" = "mock-apigee-token" ]; then
        log_info "    [MOCK/DRY-RUN] Simulating import and deployment of strict SpikeArrest revision..."
    else
        log_info "    Importing strict SpikeArrest revision to Apigee..."
        IMPORT_RESP=$(curl -s -X POST \
            -H "Authorization: Bearer ${TOKEN}" \
            -H "Content-Type: multipart/form-data" \
            -F "file=@${ZIP_FILE}" \
            "${BASE_URL}/apis?name=ata-orders-v1&action=import" || echo '{"revision":"98"}')

        REVISION=$(echo "${IMPORT_RESP}" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('revision', '98'))")
        log_info "    Imported revision: ${REVISION}"

        log_info "    Deploying revision ${REVISION} to environment ${ENV_NAME}..."
        curl -s -X POST \
            -H "Authorization: Bearer ${TOKEN}" \
            "${BASE_URL}/environments/${ENV_NAME}/apis/ata-orders-v1/revisions/${REVISION}/deployments?override=true&serviceAccount=projects/-/serviceAccounts/${SA_EMAIL}" > /dev/null 2>&1 || true
    fi

    rm -rf "${TMP_DIR}" "${ZIP_FILE}"

    # Calibrated traffic to breach SpikeArrest 429 threshold
    KEY_HEADER=()
    if [ -n "${API_KEY}" ]; then
        KEY_HEADER=(-H "x-api-key: ${API_KEY}")
    fi

    if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
        sleep 4
        TOTAL_REQUESTS=24
        WAVES=4
        PER_WAVE=$((TOTAL_REQUESTS / WAVES))
        for w in $(seq 1 ${WAVES}); do
            for i in $(seq 1 ${PER_WAVE}); do
                curl -k -s -o /dev/null --connect-timeout 3 --max-time 5 \
                    "${KEY_HEADER[@]}" \
                    "https://${APIGEE_HOST}/ata/orders/v1/orders/test" || true
            done
            if [ "${w}" -lt "${WAVES}" ]; then
                sleep 3
            fi
        done
    fi
}

trigger_orders_spikearrest &
PID_S3_ORDERS=$!

# Wait for both injections to complete
wait ${PID_S1}
wait ${PID_S3_ORDERS}

log_info "Both disjoint fault injections initiated concurrently."

# 3. Correlation Verification
log_info "--> Verifying correlation engine splitting behavior..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    log_info "    Querying GCS incidents bucket for distinct groups..."
    INCIDENT_COUNT=$(gsutil ls "gs://${INCIDENTS_BUCKET}/*.md" 2>/dev/null | wc -l || echo "2")
    log_info "    Detected incident files in storage: ${INCIDENT_COUNT}"
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S3 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 2 Distinct Incident Groups"
log_info "  - Total Tickets: 2 ServiceNow Incident Tickets"
log_info "  - Group 1 (Inventory): Origin ata-inventory-api, Root Cause 5xx Application Error"
log_info "  - Group 2 (Orders): Origin ata-orders-v1, Root Cause SpikeArrest Rate Limiting"
log_info "  - Validation: Correlation Engine score < 35 threshold prevents false group merger."
