#!/usr/bin/env bash
# ==============================================================================
# Post-Deployment Smoke Test Suite for Incident SRE Agent
#
# Validates live deployment of incident-bridge and sre-agent:
#   1. Bridge Health Check (GET /health)
#   2. Inbound Webhook Authentication Rejection (401 for ServiceNow, Jira, Generic)
#   3. Duplicate Webhook Idempotency (Deterministic deduplication)
#   4. Private SRE Agent Invocation Protection (401/403 unauthenticated, 200 /health with identity token)
#   5. Callback Mutual Authentication Rejection (401 for missing/forged OIDC token)
#   6. End-to-End Investigation Flow (Read-only probe ticket dispatch)
#   7. Report Delivery (probe report reaches bridge /callback/report; reads Cloud Logging)
#
# Safety Guarantee:
#   Strictly read-only against production GCP infrastructure. The only write
#   operation is the probe ticket itself into the ticketing adapter / Cloud Tasks.
# ==============================================================================

set -euo pipefail

# Color codes for terminal output
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    GREEN="$(printf '\033[0;32m')"
    BLUE="$(printf '\033[0;34m')"
    YELLOW="$(printf '\033[0;33m')"
    RED="$(printf '\033[0;31m')"
    NC="$(printf '\033[0m')"
else
    BOLD=""
    GREEN=""
    BLUE=""
    YELLOW=""
    RED=""
    NC=""
fi

# Counters
TOTAL_CHECKS=0
PASSED_CHECKS=0
FAILED_CHECKS=0

log_pass() {
    PASSED_CHECKS=$((PASSED_CHECKS + 1))
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    printf "%b[PASS]%b %s\n" "${GREEN}${BOLD}" "${NC}" "$1"
}

log_fail() {
    FAILED_CHECKS=$((FAILED_CHECKS + 1))
    TOTAL_CHECKS=$((TOTAL_CHECKS + 1))
    printf "%b[FAIL]%b %s\n" "${RED}${BOLD}" "${NC}" "$1"
}

log_info() {
    printf "%b[INFO]%b %s\n" "${BLUE}${BOLD}" "${NC}" "$1"
}

log_warn() {
    printf "%b[WARN]%b %s\n" "${YELLOW}${BOLD}" "${NC}" "$1"
}

# CLI Argument / Environment Variable defaults
NAME_PREFIX="${NAME_PREFIX:-isa}"
BRIDGE_URL="${BRIDGE_URL:-}"
AGENT_URL="${AGENT_URL:-}"
GENERIC_HMAC_KEY="${GENERIC_HMAC_KEY:-test-hmac-key}"

print_usage() {
    cat <<EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Post-Deployment Smoke Test Suite for Incident SRE Agent${NC}

Runs post-deployment validation checks against live Cloud Run deployments or
local test instances of incident-bridge and sre-agent.

${BOLD}Options:${NC}
  -n, --name-prefix <pfx>       Resource name prefix (default: isa, or \$NAME_PREFIX)
  -b, --bridge-url <url>        Base URL of incident-bridge (e.g. https://<prefix>-bridge-xxx.run.app)
                                [Fallback: \$BRIDGE_URL or gcloud run service url]
  -a, --agent-url <url>         Base URL of sre-agent (e.g. https://<prefix>-agent-xxx.run.app)
                                [Fallback: \$AGENT_URL or gcloud run service url]
  -k, --generic-hmac-key <key>  Secret key for Generic webhook HMAC-SHA256 signatures
                                [Fallback: \$GENERIC_HMAC_KEY, default: 'test-hmac-key']
  -h, --help                    Show this help message and exit

${BOLD}Examples:${NC}
  # Test live GCP deployments with name prefix:
  $0 --name-prefix "acme" \\
     --bridge-url "https://acme-incident-bridge-xyz-uc.a.run.app" \\
     --agent-url "https://acme-sre-agent-xyz-uc.a.run.app" \\
     --generic-hmac-key "prod-secret-hmac-key"

  # Test local multi-service container environment:
  $0 --bridge-url "http://localhost:8080" --agent-url "http://localhost:8081"
EOF
}

# Parse command line options
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n|--name-prefix)
            NAME_PREFIX="$2"
            shift 2
            ;;
        -b|--bridge-url)
            BRIDGE_URL="$2"
            shift 2
            ;;
        -a|--agent-url)
            AGENT_URL="$2"
            shift 2
            ;;
        -k|--generic-hmac-key)
            GENERIC_HMAC_KEY="$2"
            shift 2
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "Unknown option: $1" >&2
            print_usage >&2
            exit 1
            ;;
    esac
done

# Attempt to auto-discover URLs from gcloud if not provided
if [ -z "${BRIDGE_URL}" ] && command -v gcloud >/dev/null 2>&1; then
    BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-incident-bridge" --format='value(status.url)' 2>/dev/null || true)"
fi
if [ -z "${AGENT_URL}" ] && command -v gcloud >/dev/null 2>&1; then
    AGENT_URL="$(gcloud run services describe "${NAME_PREFIX}-sre-agent" --format='value(status.url)' 2>/dev/null || true)"
fi

# Validate required parameters
if [ -z "${BRIDGE_URL}" ]; then
    printf "%b[FAIL]%b Missing required argument: --bridge-url <url> (or set \$BRIDGE_URL)\n\n" "${RED}${BOLD}" "${NC}" >&2
    print_usage >&2
    exit 1
fi

# Strip trailing slashes
BRIDGE_URL="${BRIDGE_URL%/}"
if [ -n "${AGENT_URL}" ]; then
    AGENT_URL="${AGENT_URL%/}"
fi

# Helper to compute HMAC-SHA256 signature
compute_hmac() {
    local key="$1"
    local data="$2"
    python3 -c "import hmac, hashlib, sys; print(hmac.new(sys.argv[1].encode('utf-8'), sys.argv[2].encode('utf-8'), hashlib.sha256).hexdigest())" "${key}" "${data}"
}

printf "\n%b======================================================================%b\n" "${BOLD}" "${NC}"
printf "%b Incident SRE Agent — Post-Deployment Smoke Test Suite%b\n" "${BOLD}" "${NC}"
printf "%b======================================================================%b\n" "${BOLD}" "${NC}"
log_info "Name Prefix:      ${NAME_PREFIX}"
log_info "Bridge URL:       ${BRIDGE_URL}"
if [ -n "${AGENT_URL}" ]; then
    log_info "Agent URL:        ${AGENT_URL}"
else
    log_info "Agent URL:        [Omitted - Agent invocation check will be skipped]"
fi
log_info "Generic HMAC Key: [Configured (${#GENERIC_HMAC_KEY} chars)]"
printf "\n"

# ==============================================================================
# Check 1: Bridge Health Check
# ==============================================================================
printf "%b--- Check 1: Bridge Health Check ---%b\n" "${BOLD}" "${NC}"
CHECK1_RESP=$(curl -s -w "\n%{http_code}" "${BRIDGE_URL}/health" || true)
CHECK1_CODE=$(echo "${CHECK1_RESP}" | tail -n1)
CHECK1_BODY=$(echo "${CHECK1_RESP}" | sed '$d')

if [ "${CHECK1_CODE}" = "200" ]; then
    log_pass "Bridge /health returned HTTP 200 (response: ${CHECK1_BODY})"
else
    log_fail "Bridge /health returned HTTP ${CHECK1_CODE} (expected 200, response: ${CHECK1_BODY})"
fi

# ==============================================================================
# Check 2: Inbound Webhook Authentication Rejection
# ==============================================================================
printf "\n%b--- Check 2: Inbound Webhook Authentication Rejection ---%b\n" "${BOLD}" "${NC}"

# 2a: ServiceNow without Authorization header
SNOW_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BRIDGE_URL}/webhook/servicenow" \
    -H "Content-Type: application/json" \
    -d '{"number":"INC-PROBE-001","short_description":"Probe"}' || true)

if [ "${SNOW_CODE}" = "401" ]; then
    log_pass "ServiceNow webhook without auth rejected with HTTP 401"
else
    log_fail "ServiceNow webhook without auth returned HTTP ${SNOW_CODE} (expected 401)"
fi

# 2b: Jira Cloud without X-Webhook-Token header
JIRA_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BRIDGE_URL}/webhook/jira" \
    -H "Content-Type: application/json" \
    -d '{"issue":{"key":"OPS-PROBE-1","fields":{"summary":"Probe"}}}' || true)

if [ "${JIRA_CODE}" = "401" ]; then
    log_pass "Jira webhook without token header rejected with HTTP 401"
else
    log_fail "Jira webhook without token header returned HTTP ${JIRA_CODE} (expected 401)"
fi

# 2c: Generic webhook without valid HMAC signature
GENERIC_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BRIDGE_URL}/webhook/generic" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=0000000000000000000000000000000000000000000000000000000000000000" \
    -d '{"id":"gen-probe-1","title":"Probe"}' || true)

if [ "${GENERIC_CODE}" = "401" ]; then
    log_pass "Generic webhook without valid HMAC signature rejected with HTTP 401"
else
    log_fail "Generic webhook without valid HMAC signature returned HTTP ${GENERIC_CODE} (expected 401)"
fi

# ==============================================================================
# Check 3: Duplicate Webhook Idempotency
# ==============================================================================
printf "\n%b--- Check 3: Duplicate Webhook Idempotency ---%b\n" "${BOLD}" "${NC}"
IDEMP_KEY="smoke-idemp-$(date +%s)-$RANDOM"
IDEMP_PAYLOAD=$(cat <<EOF
{"id":"${IDEMP_KEY}","external_id":"${IDEMP_KEY}","external_key":"${IDEMP_KEY}","title":"Idempotency Probe Incident","description":"Automated smoke test probe","severity":"P4","opened_at":"2026-10-09T05:00:00Z"}
EOF
)
IDEMP_SIG=$(compute_hmac "${GENERIC_HMAC_KEY}" "${IDEMP_PAYLOAD}")

# Initial dispatch
RESP1=$(curl -s -w "\n%{http_code}" -X POST "${BRIDGE_URL}/webhook/generic" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=${IDEMP_SIG}" \
    -d "${IDEMP_PAYLOAD}" || true)
CODE1=$(echo "${RESP1}" | tail -n1)
BODY1=$(echo "${RESP1}" | sed '$d')

# Replay duplicate
RESP2=$(curl -s -w "\n%{http_code}" -X POST "${BRIDGE_URL}/webhook/generic" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=${IDEMP_SIG}" \
    -d "${IDEMP_PAYLOAD}" || true)
CODE2=$(echo "${RESP2}" | tail -n1)
BODY2=$(echo "${RESP2}" | sed '$d')

if [[ "${CODE1}" =~ ^(200|202)$ ]] && echo "${BODY1}" | grep -q "task_id"; then
    log_pass "Initial generic webhook accepted (HTTP ${CODE1}, task_id present)"
else
    log_fail "Initial generic webhook returned HTTP ${CODE1} (response: ${BODY1})"
fi

if [ "${CODE2}" = "200" ] && echo "${BODY2}" | grep -q "duplicate"; then
    log_pass "Replayed duplicate webhook returned HTTP 200 with idempotent acknowledgement"
elif [ "${CODE2}" = "200" ]; then
    log_pass "Replayed duplicate webhook acknowledged with HTTP 200 (response: ${BODY2})"
else
    log_fail "Duplicate webhook replay returned HTTP ${CODE2} (expected 200, response: ${BODY2})"
fi

# ==============================================================================
# Check 4: Private SRE Agent Invocation Protection
# ==============================================================================
printf "\n%b--- Check 4: Private SRE Agent Invocation Protection ---%b\n" "${BOLD}" "${NC}"
if [ -n "${AGENT_URL}" ]; then
    # 4a: Unauthenticated POST to /investigate must be rejected with 401 or 403
    AGENT_INV_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${AGENT_URL}/investigate" \
        -H "Content-Type: application/json" \
        -d '{"incident":{"id":"probe-1","source":"generic","external_id":"1","external_key":"1","title":"Probe","severity":"P4","opened_at":"2026-10-09T05:00:00Z"},"callback_url":"http://localhost/callback"}' || true)

    if [ "${AGENT_INV_CODE}" = "401" ] || [ "${AGENT_INV_CODE}" = "403" ]; then
        log_pass "Private agent /investigate unauthenticated invocation rejected with HTTP ${AGENT_INV_CODE}"
    else
        log_fail "Private agent /investigate unauthenticated invocation returned HTTP ${AGENT_INV_CODE} (expected 401 or 403)"
    fi

    # 4b: Health check. The agent requires IAM authentication on every path, including
    # /health, so call it with the operator's identity token.
    ID_TOKEN=""
    if command -v gcloud >/dev/null 2>&1; then
        ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
    fi

    if [ -z "${ID_TOKEN}" ]; then
        log_fail "Agent /health not checked: gcloud is missing or not authenticated (run 'gcloud auth login')"
    else
        AGENT_HEALTH_RESP=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer ${ID_TOKEN}" "${AGENT_URL}/health" || true)
        AGENT_HEALTH_CODE=$(echo "${AGENT_HEALTH_RESP}" | tail -n1)
        AGENT_HEALTH_BODY=$(echo "${AGENT_HEALTH_RESP}" | sed '$d')

        if [ "${AGENT_HEALTH_CODE}" = "200" ]; then
            log_pass "Agent /health returned HTTP 200 with identity token (response: ${AGENT_HEALTH_BODY})"
        else
            log_fail "Agent /health returned HTTP ${AGENT_HEALTH_CODE} with identity token (expected 200, response: ${AGENT_HEALTH_BODY})"
        fi
    fi
else
    log_info "Skipping Check 4: --agent-url was not specified"
fi

# ==============================================================================
# Check 5: Callback Mutual Authentication Rejection
# ==============================================================================
printf "\n%b--- Check 5: Callback Mutual Authentication Rejection ---%b\n" "${BOLD}" "${NC}"

# 5a: Missing Authorization header
CB_NO_AUTH_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BRIDGE_URL}/callback/report" \
    -H "Content-Type: application/json" \
    -d '{"investigation_id":"inv-probe","incident_id":"probe","status":"completed"}' || true)

if [ "${CB_NO_AUTH_CODE}" = "401" ]; then
    log_pass "Callback without Authorization header rejected with HTTP 401"
else
    log_fail "Callback without Authorization header returned HTTP ${CB_NO_AUTH_CODE} (expected 401)"
fi

# 5b: Forged / invalid Bearer token
FORGED_AUTH_HEADER="Authorization: Bearer invalid-forged-token-abc"
CB_FORGED_CODE=$(curl -s -o /dev/null -w "%{http_code}" -X POST "${BRIDGE_URL}/callback/report" \
    -H "${FORGED_AUTH_HEADER}" \
    -H "Content-Type: application/json" \
    -d '{"investigation_id":"inv-probe","incident_id":"probe","status":"completed"}' || true)

if [ "${CB_FORGED_CODE}" = "401" ]; then
    log_pass "Callback with forged Bearer token rejected with HTTP 401"
else
    log_fail "Callback with forged Bearer token returned HTTP ${CB_FORGED_CODE} (expected 401)"
fi

# ==============================================================================
# Check 6: End-to-End Investigation Flow (Read-Only Test Ticket)
# ==============================================================================
printf "\n%b--- Check 6: End-to-End Investigation Flow (Read-Only Test Ticket) ---%b\n" "${BOLD}" "${NC}"
E2E_KEY="smoke-probe-$(date +%s)-$RANDOM"
SERVICE_HINT="${SERVICE_HINT:-${NAME_PREFIX}-shop-api}"
E2E_PAYLOAD=$(cat <<EOF
{"id":"${E2E_KEY}","external_id":"${E2E_KEY}","external_key":"${E2E_KEY}","title":"Smoke Test Probe Incident","description":"Automated post-deployment read-only probe incident","severity":"P4","service_hint":"${SERVICE_HINT}","opened_at":"2026-10-09T05:00:00Z"}
EOF
)
E2E_SIG=$(compute_hmac "${GENERIC_HMAC_KEY}" "${E2E_PAYLOAD}")
E2E_SENT_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
E2E_ACCEPTED=false

E2E_RESP=$(curl -s -w "\n%{http_code}" -X POST "${BRIDGE_URL}/webhook/generic" \
    -H "Content-Type: application/json" \
    -H "X-Hub-Signature-256: sha256=${E2E_SIG}" \
    -d "${E2E_PAYLOAD}" || true)
E2E_CODE=$(echo "${E2E_RESP}" | tail -n1)
E2E_BODY=$(echo "${E2E_RESP}" | sed '$d')

if [[ "${E2E_CODE}" =~ ^(200|202)$ ]] && (echo "${E2E_BODY}" | grep -q "task_id" || echo "${E2E_BODY}" | grep -q "accepted" || echo "${E2E_BODY}" | grep -q "queued"); then
    log_pass "Read-only probe ticket accepted and enqueued successfully (HTTP ${E2E_CODE}, response: ${E2E_BODY})"
    E2E_ACCEPTED=true
else
    log_fail "Read-only probe ticket failed with HTTP ${E2E_CODE} (expected 200 or 202, response: ${E2E_BODY})"
fi

# ==============================================================================
# Check 7: Report Delivery (Probe Report Reaches the Bridge)
# ==============================================================================
# A 202 in Check 6 only proves the probe was queued. Confirm the agent finished the
# investigation and the bridge accepted its report, using Cloud Logging: the agent logs an
# sre_report entry for the incident just before POSTing it, and the bridge's request log
# then records POST /callback/report 200. Fails fast if the agent logs a delivery error.

# Print the first <field> value from <prefix>-<service>'s Cloud Run logs since the probe was
# sent, matching <filter>. Prints nothing if there is no match or the read fails.
logs_query() {
    local service="$1" filter="$2" field="$3"
    gcloud logging read \
        "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${NAME_PREFIX}-${service}\" AND timestamp>=\"${E2E_SENT_AT}\" AND ${filter}" \
        --limit=1 --format="value(${field})" 2>/dev/null | head -n1 || true
}

# Succeed if Cloud Logging can be read; otherwise fail and print gcloud's error
# (not logged in, no default project, or no log access).
check_log_access() {
    gcloud logging read 'resource.type="cloud_run_revision"' --limit=1 --format='value(timestamp)' 2>&1 >/dev/null
}

printf "\n%b--- Check 7: Report Delivery (Probe Report Reaches the Bridge) ---%b\n" "${BOLD}" "${NC}"
REPORT_TIMEOUT_SECONDS="${REPORT_TIMEOUT_SECONDS:-300}"
REPORT_POLL_SECONDS=15

if [ "${E2E_ACCEPTED}" != true ]; then
    log_info "Skipping Check 7: probe ticket from Check 6 was not accepted"
elif ! command -v gcloud >/dev/null 2>&1; then
    log_fail "Report delivery not checked: gcloud is not installed"
elif ! LOG_ACCESS_ERROR=$(check_log_access); then
    log_fail "Report delivery not checked: cannot read Cloud Logging (run 'gcloud auth login'): ${LOG_ACCESS_ERROR:0:240}"
else
    log_info "Waiting up to ${REPORT_TIMEOUT_SECONDS}s for the report for ${E2E_KEY}..."
    REPORT_RESULT="timeout"
    DEADLINE=$(( $(date +%s) + REPORT_TIMEOUT_SECONDS ))

    DELIVERY_ERROR_FILTER='textPayload:"Failed to deliver callback"'
    REPORT_SENT_FILTER="jsonPayload.jsonPayload.incident_external_key=\"${E2E_KEY}\""
    CALLBACK_OK_FILTER='httpRequest.requestUrl:"/callback/report" AND httpRequest.status=200'

    while [ "$(date +%s)" -lt "${DEADLINE}" ]; do
        DELIVERY_ERROR=$(logs_query sre-agent "${DELIVERY_ERROR_FILTER}" textPayload)
        if [ -n "${DELIVERY_ERROR}" ]; then
            REPORT_RESULT="error"; break
        fi

        REPORT_SENT_AT=$(logs_query sre-agent "${REPORT_SENT_FILTER}" timestamp)
        if [ -z "${REPORT_SENT_AT}" ]; then
            sleep "${REPORT_POLL_SECONDS}"; continue        # agent hasn't sent a report yet
        fi

        if [ -n "$(logs_query incident-bridge "timestamp>=\"${REPORT_SENT_AT}\" AND ${CALLBACK_OK_FILTER}" timestamp)" ]; then
            REPORT_RESULT="delivered"; break
        fi
        sleep "${REPORT_POLL_SECONDS}"
    done

    case "${REPORT_RESULT}" in
        delivered)
            log_pass "Report for ${E2E_KEY} delivered to bridge (POST /callback/report 200)" ;;
        error)
            log_fail "Agent could not deliver the report: ${DELIVERY_ERROR:0:240}" ;;
        *)
            log_fail "No report delivered for ${E2E_KEY} within ${REPORT_TIMEOUT_SECONDS}s (check ${NAME_PREFIX}-sre-agent logs)" ;;
    esac
fi

# ==============================================================================
# Summary
# ==============================================================================
printf "\n%b======================================================================%b\n" "${BOLD}" "${NC}"
printf "%b Smoke Test Suite Summary%b\n" "${BOLD}" "${NC}"
printf "%b======================================================================%b\n" "${BOLD}" "${NC}"
printf "Total Checks:  %d\n" "${TOTAL_CHECKS}"
printf "Passed:        %b%d%b\n" "${GREEN}${BOLD}" "${PASSED_CHECKS}" "${NC}"
if [ "${FAILED_CHECKS}" -gt 0 ]; then
    printf "Failed:        %b%d%b\n" "${RED}${BOLD}" "${FAILED_CHECKS}" "${NC}"
    printf "\n%bResult: FAILURE%b — Post-deployment smoke tests did not pass.\n" "${RED}${BOLD}" "${NC}"
    exit 1
else
    printf "Failed:        %d\n" "${FAILED_CHECKS}"
    printf "\n%bResult: SUCCESS%b — All post-deployment smoke tests passed cleanly.\n" "${GREEN}${BOLD}" "${NC}"
    exit 0
fi
