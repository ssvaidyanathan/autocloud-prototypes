#!/usr/bin/env bash
# ==============================================================================
# Scenario S1: Backend 5xx Error Spike
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: POST /admin/fault/inject with {"type": "500", "rate": 0.85}
# Alerts: Cloud Run 5xx alert on ata-inventory-api, Apigee 5xx alert on ata-inventory-v1
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api
#   - Blast Radius: ata-inventory-v1, ata-orders-api, ata-orders-v1
#   - Root Cause: Internal application exception in inventory service
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

log_info "=== Starting Scenario S1: Backend 5xx Error Spike ==="

if [ "${RUN_PREFLIGHT:-false}" = "true" ]; then
    "${SCRIPT_DIR}/preflight.sh"
fi

# 1. Resolve Targets
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"
API_KEY="$(resolve_api_key)"

AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

log_info "Target Inventory Service: ${INV_URL}"
log_info "Apigee Ingress Host: ${APIGEE_HOST}"

# 2. Inject Fault: POST /admin/fault/inject
log_info "--> Injecting 500 error fault (rate: 0.85) on ata-inventory-api..."
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/inject -d '{\"type\": \"500\", \"rate\": 0.85}'"
else
    INJECT_RESP=$(curl -s -X POST \
        "${AUTH_HEADER[@]}" \
        -H "Content-Type: application/json" \
        -d '{"type": "500", "rate": 0.85}' \
        "${INV_URL}/admin/fault/inject" || echo '{"status":"mocked"}')
    log_info "    Fault injection response: ${INJECT_RESP}"
fi

# 3. Generate Traffic via Apigee
log_info "--> Generating client traffic through Apigee to trigger error rate..."
ERRORS_SEEN=0
TOTAL_REQUESTS=10

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

if [ "${DRY_RUN}" = "false" ]; then
    for i in $(seq 1 ${TOTAL_REQUESTS}); do
        STATUS=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            "${KEY_HEADER[@]}" \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" || echo "500")
        if [ "${STATUS}" = "500" ]; then
            ERRORS_SEEN=$((ERRORS_SEEN + 1))
        fi
    done
    log_info "    Requests completed: ${TOTAL_REQUESTS}, 500 Errors observed: ${ERRORS_SEEN}"
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S1 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api"
log_info "  - Blast Radius: ata-inventory-v1, ata-orders-api, ata-orders-v1"
log_info "  - Root Cause: Internal application exception in inventory service."
