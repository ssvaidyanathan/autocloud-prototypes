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

HOLD_DURATION="${ATA_HOLD_DURATION:-7m}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --hold)
            HOLD_DURATION="$2"
            shift 2
            ;;
        --hold=*)
            HOLD_DURATION="${1#*=}"
            shift
            ;;
        --dry-run)
            export DRY_RUN="true"
            shift
            ;;
        --mock)
            export MOCK_MODE="true"
            shift
            ;;
        --help|-h)
            echo "Usage: $0 [--hold <duration>] [--dry-run] [--mock]"
            echo "  --hold <duration>   Sustained traffic duration (default: 7m, e.g. 7m, 420s, 0s)"
            echo "  --dry-run           Simulate fault injection and traffic without network calls"
            echo "  --mock              Run in mock mode without GCP cloud dependencies"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

HOLD_SECS="$(parse_duration "${HOLD_DURATION}")"
SCENARIO_START_TIME="$(date -u +'%Y-%m-%dT%H:%M:%SZ')"

# Resolve the Apigee key before any fault is injected: traffic needs it.
API_KEY="$(resolve_api_key)" || exit 1

log_info "=== Starting Scenario S3: Concurrent Disjoint Incidents ==="
log_info "Start Time: ${SCENARIO_START_TIME} | Hold Duration: ${HOLD_SECS}s (${HOLD_DURATION})"

# The strict SpikeArrest bundle and the Apigee token are prepared before any fault is
# injected: a missing bundle or a failed zip then stops the run with nothing to undo.
TOKEN="$(resolve_apigee_token)"
BASE_URL="https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}"
SA_EMAIL="ata-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com"

BUNDLE_SRC="${SCRIPT_DIR}/../../demo/apigee/ata-orders-v1"
TMP_DIR="/tmp/ata-orders-s3-$$"
ZIP_FILE="/tmp/ata-orders-v1-s3.zip"

if [ ! -d "${BUNDLE_SRC}/apiproxy/policies" ]; then
    log_error "FATAL: Apigee bundle ${BUNDLE_SRC} (apiproxy/policies) not found; S3 not started, no fault injected." >&2
    exit 1
fi
mkdir -p "${TMP_DIR}"
cp -r "${BUNDLE_SRC}"/* "${TMP_DIR}/"

cat <<'EOF' > "${TMP_DIR}/apiproxy/policies/SA-SpikeArrest.xml"
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<SpikeArrest continueOnError="false" enabled="true" name="SA-SpikeArrest">
    <DisplayName>Spike Arrest Rate Limiting (Strict 1pm)</DisplayName>
    <Rate>1pm</Rate>
</SpikeArrest>
EOF

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

APIGEE_LIVE=true
if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ -z "${TOKEN}" ] || [ "${TOKEN}" = "mock-apigee-token" ]; then
    APIGEE_LIVE=false
fi
if [ "${APIGEE_LIVE}" = "true" ] && [ ! -s "${ZIP_FILE}" ]; then
    log_error "FATAL: could not build the Apigee bundle zip ${ZIP_FILE} from ${TMP_DIR}; S3 not started, no fault injected." >&2
    rm -rf "${TMP_DIR}"
    exit 1
fi

# 1. Trigger S1 fault directly on inventory API
INV_URL="$(resolve_service_url "${NAME_PREFIX}-inventory-api" "https://ata-inventory-api-4klvlqnmpa-uw.a.run.app")"
ID_TOKEN="$(resolve_id_token "${INV_URL}")"
AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ] && [ "${ID_TOKEN}" != "mock-id-token" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

# From the fault injection on, an exit with an error undoes what S3 changed instead of
# leaving it with no traffic: it resets the inventory 500 fault and, if the strict
# SpikeArrest revision was deployed, redeploys the proxy revision that served before.
# A successful run keeps both: the scenario needs them until reset-scenarios.sh.
FAULT_INJECTED=false
PROXY_DEPLOYED=false
PREV_PROXY_REVISION=""
REVISION=""
restore_orders_proxy_after_abort() {
    local code
    if [ -z "${PREV_PROXY_REVISION}" ]; then
        log_error "FATAL: the ata-orders-v1 revision deployed before S3 is unknown; the proxy was NOT restored." >&2
        return 1
    fi
    if [ "${PREV_PROXY_REVISION}" = "${REVISION}" ]; then
        return 0
    fi
    code=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 60 -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        "${BASE_URL}/environments/${ENV_NAME}/apis/ata-orders-v1/revisions/${PREV_PROXY_REVISION}/deployments?override=true&serviceAccount=projects/-/serviceAccounts/${SA_EMAIL}" \
        2>/dev/null) || code="000"
    case "${code}" in
        2??)
            log_warn "Redeployed ata-orders-v1 revision ${PREV_PROXY_REVISION} (HTTP ${code})." >&2
            return 0
            ;;
    esac
    log_error "FATAL: redeploying ata-orders-v1 revision ${PREV_PROXY_REVISION} returned HTTP ${code:-<none>}." >&2
    return 1
}
undo_s3_on_error() {
    local rc=$? left=""
    if [ "${rc}" -eq 0 ] || [ "${FAULT_INJECTED}" != "true" ]; then
        return 0
    fi
    log_error "S3 aborted (exit ${rc}) after injecting faults; undoing them." >&2
    if ! reset_inventory_fault_after_abort "the 500 error fault"; then
        left="the 500 error fault on ${NAME_PREFIX}-inventory-api"
    fi
    if [ "${PROXY_DEPLOYED}" = "true" ] && ! restore_orders_proxy_after_abort; then
        left="${left:+${left}; }ata-orders-v1 deployed with the strict 1pm SpikeArrest revision ${REVISION:-<unknown>}"
    fi
    rm -rf "${TMP_DIR}" "${ZIP_FILE}"
    if [ -n "${left}" ]; then
        log_error "STILL LEFT after the S3 abort: ${left}. Run ./scripts/reset-scenarios.sh now." >&2
    else
        log_warn "S3 abort cleanup complete: nothing left injected." >&2
    fi
    exit "${rc}"
}
trap undo_s3_on_error EXIT

log_info "--> Injecting 500 error fault (rate: 0.85) on ata-inventory-api..."
FAULT_INJECTED=true
if [ "${DRY_RUN}" = "true" ]; then
    log_info "[DRY-RUN] curl -s -X POST ${INV_URL}/admin/fault/inject -d '{\"type\": \"500\", \"rate\": 0.85}'"
else
    HTTP_CODE=$(curl -s -o /tmp/ata-s3-inject-resp.txt -w "%{http_code}" -X POST \
        ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
        -H "Content-Type: application/json" \
        -d '{"type": "500", "rate": 0.85}' \
        "${INV_URL}/admin/fault/inject" || echo "000")
    check_cloud_run_auth "${HTTP_CODE}" "${INV_URL}/admin/fault/inject" || true
    rm -f /tmp/ata-s3-inject-resp.txt
fi

# 2. Trigger Orders proxy SpikeArrest 429 fault concurrently
log_info "--> Deploying strict 1pm SpikeArrest on ata-orders-v1 Apigee proxy..."

if [ "${APIGEE_LIVE}" != "true" ]; then
    log_info "    [MOCK/DRY-RUN] Simulating import and deployment of strict SpikeArrest revision..."
else
    # The revision serving now, so an abort after the deploy can put it back.
    DEPLOYMENTS_RESP=$(curl -s -H "Authorization: Bearer ${TOKEN}" \
        "${BASE_URL}/environments/${ENV_NAME}/apis/ata-orders-v1/deployments" 2>/dev/null) || DEPLOYMENTS_RESP=""
    PREV_PROXY_REVISION=$(printf '%s' "${DEPLOYMENTS_RESP}" | python3 -c '
import json, sys
try:
    deps = json.load(sys.stdin).get("deployments") or []
    print(max((str(d["revision"]) for d in deps if d.get("revision")), key=int))
except Exception:
    sys.exit(1)
' 2>/dev/null) || PREV_PROXY_REVISION=""
    if [ -n "${PREV_PROXY_REVISION}" ]; then
        log_info "    ata-orders-v1 revision ${PREV_PROXY_REVISION} is deployed in ${ENV_NAME} before S3."
    else
        log_warn "    Could not read the deployed ata-orders-v1 revision; an abort after the deploy cannot restore it (reset-scenarios.sh does)." >&2
    fi

    log_info "    Importing strict SpikeArrest revision to Apigee..."
    if ! IMPORT_RESP=$(curl -s -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        -H "Content-Type: multipart/form-data" \
        -F "file=@${ZIP_FILE}" \
        "${BASE_URL}/apis?name=ata-orders-v1&action=import"); then
        log_error "FATAL: the Apigee import request for ata-orders-v1 failed (curl error); strict SpikeArrest not deployed." >&2
        exit 1
    fi

    if ! REVISION=$(printf '%s' "${IMPORT_RESP}" | python3 -c '
import json, sys
try:
    rev = json.load(sys.stdin).get("revision")
except Exception as exc:
    print("not JSON (%s)" % exc, file=sys.stderr)
    sys.exit(1)
if not rev:
    print("JSON without a revision", file=sys.stderr)
    sys.exit(1)
print(rev)
' 2>/tmp/ata-s3-import-err.txt); then
        log_error "FATAL: the Apigee import response for ata-orders-v1 is $(cat /tmp/ata-s3-import-err.txt 2>/dev/null); strict SpikeArrest not deployed. Response: ${IMPORT_RESP:0:300}" >&2
        rm -f /tmp/ata-s3-import-err.txt
        exit 1
    fi
    rm -f /tmp/ata-s3-import-err.txt
    log_info "    Imported revision: ${REVISION}"

    log_info "    Deploying revision ${REVISION} to environment ${ENV_NAME}..."
    PROXY_DEPLOYED=true
    curl -s -X POST \
        -H "Authorization: Bearer ${TOKEN}" \
        "${BASE_URL}/environments/${ENV_NAME}/apis/ata-orders-v1/revisions/${REVISION}/deployments?override=true&serviceAccount=projects/-/serviceAccounts/${SA_EMAIL}" > /dev/null 2>&1 || true
fi
rm -rf "${TMP_DIR}" "${ZIP_FILE}"

# 3. Generate Sustained Concurrent Traffic
log_info "--> Initiating sustained dual-stream traffic loop (hold duration: ${HOLD_SECS}s / ${HOLD_DURATION})..."
log_info "    Traffic stream 1: GET /ata/inventory/v1/items (driving 5xx errors)"
log_info "    Traffic stream 2: GET /ata/orders/v1/orders/test (breaching SpikeArrest 429 threshold)"

KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

STOP_TRAFFIC=false
cleanup_s3() {
    STOP_TRAFFIC=true
    kill $(jobs -p) 2>/dev/null || true
    echo ""
    log_warn "Traffic generation interrupted by user (Ctrl-C). Finishing scenario..."
}
trap cleanup_s3 INT TERM

TOTAL_INV=0
TOTAL_ORD=0
COUNT_429=0
COUNT_500=0
START_EPOCH=$(date +%s)
LAST_REPORT=${START_EPOCH}

if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ] || [ "${HOLD_SECS}" -le 0 ]; then
    log_info "    [MOCK/DRY-RUN] Simulating concurrent disjoint traffic wave (0s hold)..."
    log_info "    Simulated results: Inventory 500 errors observed, Orders 429 SpikeArrests observed."
else
    while true; do
        if [ "${STOP_TRAFFIC}" = "true" ]; then
            break
        fi

        # A Ctrl-C (SIGINT to the whole process group) also kills the foreground child, which
        # exits 130. Under set -e that would abort the run and the EXIT trap would undo the
        # faults. So when STOP_TRAFFIC is set, end the loop and finish normally (DEMO.md).
        CURRENT_EPOCH=$(date +%s) || { rc=$?; [ "${STOP_TRAFFIC}" = "true" ] && break; exit "${rc}"; }
        ELAPSED=$((CURRENT_EPOCH - START_EPOCH))
        if [ ${ELAPSED} -ge ${HOLD_SECS} ]; then
            break
        fi

        # Inventory traffic (500)
        STATUS_INV=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
            ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
            "https://${APIGEE_HOST}/ata/inventory/v1/items" || echo "000")
        TOTAL_INV=$((TOTAL_INV + 1))
        if [ "${STATUS_INV}" = "500" ]; then
            COUNT_500=$((COUNT_500 + 1))
        fi

        # Orders burst traffic (breaching SpikeArrest 1pm -> 429)
        for b in 1 2 3; do
            STATUS_ORD=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 3 --max-time 5 \
                ${KEY_HEADER[@]+"${KEY_HEADER[@]}"} \
                "https://${APIGEE_HOST}/ata/orders/v1/orders/test" || echo "000")
            TOTAL_ORD=$((TOTAL_ORD + 1))
            if [ "${STATUS_ORD}" = "429" ]; then
                COUNT_429=$((COUNT_429 + 1))
            fi
        done

        if [ $((CURRENT_EPOCH - LAST_REPORT)) -ge 15 ]; then
            PCT=$(( (ELAPSED * 100) / (HOLD_SECS > 0 ? HOLD_SECS : 1) ))
            log_info "[$(date +'%T')] S3 progress: ${ELAPSED}s / ${HOLD_SECS}s (${PCT}%) | Inv: ${TOTAL_INV} reqs (${COUNT_500} 500s) | Orders: ${TOTAL_ORD} reqs (${COUNT_429} 429s)"
            LAST_REPORT=${CURRENT_EPOCH}
        fi

        sleep 1.5 || true  # interrupted by Ctrl-C: the loop ends via STOP_TRAFFIC
    done
    trap - INT TERM
    log_info "    Traffic generation completed: Inv ${TOTAL_INV} reqs, Orders ${TOTAL_ORD} reqs."
fi

# 4. Scenario Assertion & Summary
log_success "=== Scenario S3 Injected Successfully ==="
log_info "Expected Triage & Correlation Outcome:"
log_info "  - Total Groups: 2 Distinct Incident Groups"
log_info "  - Total Tickets: 2 ServiceNow Incident Tickets"
log_info "  - Group 1 (Inventory): Origin ata-inventory-api, Root Cause 5xx Application Error"
log_info "  - Group 2 (Orders): Origin ata-orders-v1, Root Cause new proxy revision rejecting requests with SpikeArrest faults"
log_info "  - Validation: the orders ticket's 'Correlation:' observation states the best score against the open group and the 35 pts threshold."

cat <<EOF

===================================================================
                       NEXT STEPS
===================================================================
1. Verify Triage & Correlation Results:
   ./scripts/scenarios/check-result.sh --since "${SCENARIO_START_TIME}" --scenario s3

2. Check Incidents in ServiceNow:
   - Expected Ticket Count: EXACTLY 2 DISTINCT TICKETS
   - Ticket 1: Origin ata-inventory-api (Inventory 5xx application error)
   - Ticket 2: Origin ata-orders-v1 (Apigee orders SpikeArrest 429 rate limiting)
   - Proves correlation engine does NOT incorrectly merge concurrent independent incidents
   - Verify separate HTML diagnostic reports are attached to both tickets

3. Reset Environment Before Running Next Scenario:
   ./scripts/reset-scenarios.sh
===================================================================

EOF
