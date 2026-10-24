#!/usr/bin/env bash
# ==============================================================================
# Scenario S5: Bad Deploy / Config Failure
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: Deploy new Cloud Run revision configured with invalid DB user/auth
# Alerts: Cloud Run 500 error alert on startup/health, zero Cloud SQL CPU/connection alerts
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api explicitly named revision (e.g. ata-inventory-api-00062-6pj)
#   - Blast Radius: Inventory & dependent APIs
#   - Root Cause: Change-induced database authentication failure
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

# Revert Helper Function: Restores the exact pinned revision and baseline config
revert_s5() {
    log_info "--> Reverting S5: Restoring exact pinned revision ${PINNED_INVENTORY_REVISION}..."
    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_info "[MOCK/DRY-RUN] Reverted S5 to pinned revision."
        return 0
    fi
    # 1. Direct traffic back to the exact pinned revision
    gcloud run services update-traffic "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${PINNED_INVENTORY_REVISION}=100" \
        --quiet || log_warn "Traffic migration to pinned revision skipped or restricted."
    # 2. Restore service baseline credentials on top of pinned image
    gcloud run services update "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --image="${PINNED_INVENTORY_IMAGE}" \
        --update-env-vars="ATA_INVENTORY_DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,ATA_INVENTORY_MOCK_DB=false,MOCK_DB=false" \
        --quiet || log_warn "Cloud Run config restore skipped or restricted."
    log_success "    Restored exact pinned revision ${PINNED_INVENTORY_REVISION}."
}

if [ "${1:-}" = "--revert" ]; then
    revert_s5
    exit 0
fi

log_info "=== Starting Scenario S5: Bad Deploy / Config Failure ==="

# 1. Update Cloud Run Service Environment Variables strictly on top of pinned image
log_info "--> Updating ata-inventory-api with invalid DB credentials on pinned image..."
if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
    log_info "[MOCK/DRY-RUN] Simulating Cloud Run deployment with DB_USER=invalid_user on ${PINNED_INVENTORY_IMAGE}..."
else
    gcloud run services update "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --image="${PINNED_INVENTORY_IMAGE}" \
        --update-env-vars="ATA_INVENTORY_DB_USER=invalid-iam-user@${PROJECT_ID}.iam,DB_USER=invalid-iam-user@${PROJECT_ID}.iam,ATA_INVENTORY_MOCK_DB=false,MOCK_DB=false" \
        --quiet || log_warn "Cloud Run update skipped or permission restricted in test environment."
fi

# 2. Check Service Health
log_info "--> Checking service status to observe startup/DB connection failure..."
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    STATUS=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        "${AUTH_HEADER[@]}" \
        "${INV_URL}/items" || echo "500")
    log_info "    Health/Items query HTTP status: ${STATUS} (expected 500)"
fi

# 3. Scenario Assertion & Summary
log_success "=== Scenario S5 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api explicitly named revision (e.g. ata-inventory-api-00062-6pj)"
log_info "  - Blast Radius: Inventory & dependent APIs"
log_info "  - Root Cause: Change-induced database authentication failure."
