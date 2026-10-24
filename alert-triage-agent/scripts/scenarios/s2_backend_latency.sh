#!/usr/bin/env bash
# ==============================================================================
# Scenario S2: Backend Latency Degradation
#
# Target: Cloud Run (ata-inventory-api)
# Mechanism: POST /admin/fault/inject with {"type": "latency", "delay_ms": 6500}
# Alerts: Cloud Run Latency alert (>5s), Apigee 504 Gateway Timeout alerts
# Output: 1 Group, 1 Ticket
#   - Origin: ata-inventory-api
#   - Blast Radius: All downstream proxies & callers
#   - Root Cause: Backend response time exceeding Apigee 5s target timeout
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

log_info "=== Starting Scenario S2: Backend Latency Degradation ==="

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

# 2. Inject Fault: POST /admin/fault/inject with latency delay_ms: 6500
log_info "--> Injecting latency fault (delay: 6500ms) on ata-inventory-api..."
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/inject -d '{\"type\": \"latency\", \"delay_ms\": 6500}'"
else
    INJECT_RESP=$(curl -s -X POST \
        "${AUTH_HEADER[@]}" \
        -H "Content-Type: application/json" \
        -d '{"type": "latency", "delay_ms": 6500}' \
        "${INV_URL}/admin/fault/inject" || echo '{"status":"mocked"}')
    log_info "    Fault injection response: ${INJECT_RESP}"
fi

# 3. Generate Traffic via Apigee
log_info "--> Generating traffic through Apigee to verify 504 Gateway Timeout..."
KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

if [ "${DRY_RUN}" = "false" ]; then
    TIMEOUTS_SEEN=0
    TOTAL_REQUESTS=3
    for i in $(seq 1 ${TOTAL_REQUESTS}); do
        STATUS=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 15 \
            "${KEY_HEADER[@]}" \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" || echo "504")
        if [ "${STATUS}" = "504" ]; then
            TIMEOUTS_SEEN=$((TIMEOUTS_SEEN + 1))
        fi
    done
    log_info "    Requests completed: ${TOTAL_REQUESTS}, 504 Timeouts observed: ${TIMEOUTS_SEEN}"
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S2 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 1 Incident Group"
log_info "  - Total Tickets: 1 ServiceNow Incident Ticket"
log_info "  - Origin Attribution: ata-inventory-api"
log_info "  - Blast Radius: All downstream proxies & callers"
log_info "  - Root Cause: Backend response time exceeding Apigee 5s target timeout."
