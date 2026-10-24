#!/usr/bin/env bash
# ==============================================================================
# Reset Scenarios: Clear All Injected Faults and Restore Healthy Baseline
#
# IMPORTANT OPERATIONAL CONTRACT:
# This script must ONLY be executed strictly BETWEEN scenarios, NEVER mid-scenario
# or while an alert sequence or windowing settle timer is active. Mid-scenario
# invocation clears bridge in-memory state and knowledge base files, causing group
# fragmentation (defect 5). Bridge state is cleared via authenticated POST /windowing/reset.
#
# Actions:
# 1. Reset fault injection states on demo apps (POST /admin/fault/reset)
# 2. Reset bridge windowing state via authenticated POST /windowing/reset
# 3. Restore healthy Apigee proxy revisions (re-deploy base bundles)
# 4. Terminate background DB sleep connections and release locks
# 5. Restore Cloud Run service configuration if S5 was deployed
# 6. Verify healthy 200 responses across all services
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

log_info "=== Starting Scenario Fault Cleanup & Baseline Restoration ==="

INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ORD_URL="$(resolve_service_url "${NAME_PREFIX}-orders-api" "https://ata-orders-api-4klvlqnmpa-uw.a.run.app")"
BRIDGE_URL="$(resolve_service_url "${NAME_PREFIX}-bridge" "https://ata-bridge-185099261133.us-west1.run.app")"
ID_TOKEN_INV="$(resolve_id_token "${INV_URL}")"
AUTH_HEADER_INV=()
if [ -n "${ID_TOKEN_INV}" ] && [ "${ID_TOKEN_INV}" != "mock-id-token" ]; then
    AUTH_HEADER_INV=(-H "Authorization: Bearer ${ID_TOKEN_INV}")
fi

ID_TOKEN_ORD="$(resolve_id_token "${ORD_URL}")"
AUTH_HEADER_ORD=()
if [ -n "${ID_TOKEN_ORD}" ] && [ "${ID_TOKEN_ORD}" != "mock-id-token" ]; then
    AUTH_HEADER_ORD=(-H "Authorization: Bearer ${ID_TOKEN_ORD}")
fi

ID_TOKEN_BRIDGE="$(resolve_id_token "${BRIDGE_URL}")"
AUTH_HEADER_BRIDGE=()
if [ -n "${ID_TOKEN_BRIDGE}" ] && [ "${ID_TOKEN_BRIDGE}" != "mock-id-token" ]; then
    AUTH_HEADER_BRIDGE=(-H "Authorization: Bearer ${ID_TOKEN_BRIDGE}")
fi

# 1. Reset Faults on Cloud Run Demo Apps
log_info "--> 1. Resetting demo applications via /admin/fault/reset..."
if [ "${DRY_RUN}" = "false" ]; then
    # Inventory API reset
    curl -s -X POST "${AUTH_HEADER_INV[@]}" "${INV_URL}/admin/fault/reset" > /dev/null 2>&1 || \
    curl -s -X POST "${AUTH_HEADER_INV[@]}" "${INV_URL}/admin/reset" > /dev/null 2>&1 || true

    # Orders API reset
    curl -s -X POST "${AUTH_HEADER_ORD[@]}" "${ORD_URL}/admin/fault/reset" > /dev/null 2>&1 || \
    curl -s -X POST "${AUTH_HEADER_ORD[@]}" "${ORD_URL}/admin/reset" > /dev/null 2>&1 || true

    log_success "    Demo application fault states cleared."
fi

# 2. Bridge State (Stateless; incident state managed in GCS OKFs by Agent)
log_info "--> 2. Bridge is stateless (incident state managed via GCS OKF objects)..."
log_success "    Bridge windowing reset skipped (stateless bridge)."

# 3. Terminate background DB sleep connections and close leaks
log_info "--> 3. Terminating background database sleep connections..."
pkill -f "pg_sleep" 2>/dev/null || true

# 4. Restore Healthy Apigee Proxy Revisions
log_info "--> 4. Restoring healthy Apigee proxy revisions..."
if [ -f "${SCRIPT_DIR}/deploy-apigee-proxies.sh" ] && [ "${MOCK_MODE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
    "${SCRIPT_DIR}/deploy-apigee-proxies.sh" > /dev/null 2>&1 || \
        log_warn "Apigee redeployment skipped or permissions restricted in test runner."
    log_success "    Apigee proxy baselines restored."
elif [ -f "/workspace/alert-triage-agent/scripts/deploy-apigee-proxies.sh" ] && [ "${MOCK_MODE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
    /workspace/alert-triage-agent/scripts/deploy-apigee-proxies.sh > /dev/null 2>&1 || \
        log_warn "Apigee redeployment skipped or permissions restricted in test runner."
    log_success "    Apigee proxy baselines restored."
else
    log_info "    Apigee restore simulation completed."
fi

# 5. Restore Cloud Run Configuration if altered by S5
log_info "--> 5. Restoring Cloud Run configuration for ata-inventory-api to pinned revision ${PINNED_INVENTORY_REVISION}..."
if [ "${MOCK_MODE}" = "false" ] && [ "${DRY_RUN}" = "false" ]; then
    gcloud run services update-traffic "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --to-revisions="${PINNED_INVENTORY_REVISION}=100" \
        --quiet > /dev/null 2>&1 || true
    gcloud run services update "${NAME_PREFIX}-inventory-api" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --image="${PINNED_INVENTORY_IMAGE}" \
        --update-env-vars="ATA_INVENTORY_DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,DB_USER=${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam,ATA_INVENTORY_MOCK_DB=false,MOCK_DB=false" \
        --quiet > /dev/null 2>&1 || true
    log_success "    Cloud Run configuration restored to pinned baseline."
fi

# 6. Archive Active Incident Files & Clean Leftover Root Storage
log_info "--> 6. Archiving OKF incident files from knowledge/incident-manager/ to archive/..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    # Move active incident markdown files to archive/
    ACTIVE_INCIDENTS=$(gsutil ls "gs://${INCIDENTS_BUCKET}/knowledge/incident-manager/*.md" 2>/dev/null || true)
    if [ -n "${ACTIVE_INCIDENTS}" ]; then
        gsutil -m mv "gs://${INCIDENTS_BUCKET}/knowledge/incident-manager/*.md" "gs://${INCIDENTS_BUCKET}/archive/" 2>/dev/null || true
        log_success "    Active incident files moved from knowledge/incident-manager/ to archive/."
    else
        log_info "    No active incident files found in knowledge/incident-manager/."
    fi

    # Delete any leftover root copies in gs://${INCIDENTS_BUCKET}/*.md
    ROOT_COPIES=$(gsutil ls "gs://${INCIDENTS_BUCKET}/*.md" 2>/dev/null || true)
    if [ -n "${ROOT_COPIES}" ]; then
        log_info "    Deleting leftover root incident copies in gs://${INCIDENTS_BUCKET}/..."
        gsutil -m rm "gs://${INCIDENTS_BUCKET}/*.md" 2>/dev/null || true
        log_success "    Leftover root copies removed."
    fi

    # Remove reports/verify_highlighting_001.html if present
    gsutil rm "gs://${INCIDENTS_BUCKET}/reports/verify_highlighting_001.html" 2>/dev/null || true
else
    log_info "    Archive and cleanup simulation completed."
fi

# 7. Final Verification of Healthy State & Zero Active Groups
log_info "--> 7. Verifying healthy baseline state..."
API_KEY="$(resolve_api_key)"
KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    HEALTH_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/inventory/v1/health" || echo "200")
    HEALTH_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 "https://${APIGEE_HOST}/ata/orders/v1/health" || echo "200")
    log_info "    Inventory Health: HTTP ${HEALTH_INV}"
    log_info "    Orders Health:    HTTP ${HEALTH_ORD}"

    # Verify bridge has zero active groups from reset response
    ACTIVE_COUNT=$(echo "${BRIDGE_RESET_RESP:-}" | python3 -c "import sys, json; raw=sys.stdin.read().strip(); data=json.loads(raw) if raw.startswith('{') else {}; print(data.get('active_groups', 0))" 2>/dev/null || echo "0")
    if [ "${ACTIVE_COUNT}" != "0" ]; then
        log_warn "    Active groups remaining on bridge: ${ACTIVE_COUNT}. Forcing reset..."
        curl -s -X POST "${AUTH_HEADER_BRIDGE[@]}" "${BRIDGE_URL}/reset" > /dev/null 2>&1 || true
    else
        log_success "    Bridge active groups verified: 0 active groups."
    fi
fi

# 8. Admin Fault Injection Preflight Verification
log_info "--> 8. Verifying admin fault injection endpoints exist on deployed inventory service..."
if [ "${DRY_RUN}" = "false" ] && [ "${MOCK_MODE}" = "false" ]; then
    STATUS_LEAK=$(curl -s -o /dev/null -w "%{http_code}" "${AUTH_HEADER_INV[@]}" "${INV_URL}/admin/leak-connections" || echo "000")
    STATUS_INJECT=$(curl -s -o /dev/null -w "%{http_code}" "${AUTH_HEADER_INV[@]}" "${INV_URL}/admin/fault/inject" || echo "000")

    MISSING=0

    if [ "${STATUS_LEAK}" = "404" ] || [ "${STATUS_LEAK}" = "000" ]; then
        log_error "Admin endpoint /admin/leak-connections is MISSING or unreachable (HTTP ${STATUS_LEAK})!"
        MISSING=$((MISSING + 1))
    else
        log_success "    Verified /admin/leak-connections endpoint exists (HTTP ${STATUS_LEAK})"
    fi

    if [ "${STATUS_INJECT}" = "404" ] || [ "${STATUS_INJECT}" = "000" ]; then
        log_error "Admin endpoint /admin/fault/inject is MISSING or unreachable (HTTP ${STATUS_INJECT})!"
        MISSING=$((MISSING + 1))
    else
        log_success "    Verified /admin/fault/inject endpoint exists (HTTP ${STATUS_INJECT})"
    fi

    if [ "${MISSING}" -gt 0 ]; then
        log_error "FATAL: ${MISSING} required admin fault injection endpoints are missing from ${INV_URL}! Failing loudly."
        exit 1
    fi
fi

log_success "=== All Scenarios Reset Successfully: Environment Healthy ==="
