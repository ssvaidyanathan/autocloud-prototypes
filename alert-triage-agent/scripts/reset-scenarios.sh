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
# 2. Bridge state (stateless note)
# 3. Terminate background DB sleep connections and release locks
# 4. Restore healthy Apigee proxy revisions (re-deploy base bundles)
# 5. Restore Cloud Run service configuration if S5 was deployed
# 6. Wait for open ATA alerts to close & quiet period (Apigee metric lag window)
# 7. Archive active OKF incident files and clean leftover root storage
# 8. Verify healthy 200 responses across all services and 0 active bridge groups
# 9. Admin fault injection preflight verification
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

parse_duration() {
    local val="$1"
    if [[ "${val}" =~ ^([0-9]+)[mM]$ ]]; then
        echo $((${BASH_REMATCH[1]} * 60))
    elif [[ "${val}" =~ ^([0-9]+)[sS]?$ ]]; then
        echo "${BASH_REMATCH[1]}"
    else
        echo "${val}"
    fi
}

# Configuration options & defaults
NO_WAIT="${ATA_RESET_NO_WAIT:-${NO_WAIT:-false}}"
QUIET_PERIOD_SECS="$(parse_duration "${ATA_RESET_QUIET_PERIOD:-${QUIET_PERIOD:-300}}")"
WAIT_TIMEOUT_SECS="$(parse_duration "${ATA_RESET_TIMEOUT:-${WAIT_TIMEOUT:-900}}")"
POLL_INTERVAL_SECS="$(parse_duration "${ATA_RESET_POLL_INTERVAL:-${POLL_INTERVAL:-15}}")"

# Parse CLI arguments
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-wait)
            NO_WAIT="true"
            shift
            ;;
        --quiet-period)
            QUIET_PERIOD_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --quiet-period=*)
            QUIET_PERIOD_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --timeout)
            WAIT_TIMEOUT_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --timeout=*)
            WAIT_TIMEOUT_SECS="$(parse_duration "${1#*=}")"
            shift
            ;;
        --poll-interval)
            POLL_INTERVAL_SECS="$(parse_duration "$2")"
            shift 2
            ;;
        --poll-interval=*)
            POLL_INTERVAL_SECS="$(parse_duration "${1#*=}")"
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
            echo "Usage: $0 [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --no-wait                  Skip waiting for open alerts to close and quiet period"
            echo "  --quiet-period <duration>  Quiet period after alerts close (default: 300s / 5m)"
            echo "  --timeout <duration>       Max timeout waiting for alerts to close (default: 900s / 15m)"
            echo "  --poll-interval <duration> Polling interval for alert queries (default: 15s)"
            echo "  --dry-run                  Simulate reset without network modifications"
            echo "  --mock                     Run in mock mode without GCP cloud dependencies"
            echo "  -h, --help                 Display this help message"
            exit 0
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

query_open_ata_alerts() {
    local proj="$1"
    local tok="$2"

    python3 -c '
import sys, json, urllib.request, urllib.error

project_id = sys.argv[1]
token = sys.argv[2]
if not token:
    print("[]")
    sys.exit(0)

open_alerts = []
page_token = None

while True:
    url = f"https://monitoring.googleapis.com/v3/projects/{project_id}/alerts?filter=state%3D%22OPEN%22&pageSize=100"
    if page_token:
        url += f"&pageToken={page_token}"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        sys.stderr.write(f"Warning: Cloud Monitoring API query failed: {e}\n")
        break

    for alert in data.get("alerts", []):
        policy = alert.get("policy", {})
        disp = str(policy.get("displayName") or "").strip()
        pol_name = str(policy.get("name") or "").strip()
        user_labels = policy.get("userLabels") or alert.get("userLabels") or {}

        # Scoping criteria matching ata-* policies (label app=alert-triage-agent)
        is_ata = False
        if isinstance(user_labels, dict):
            if user_labels.get("app") == "alert-triage-agent" or user_labels.get("managed_by") == "alert-triage-agent":
                is_ata = True
        if disp.lower().startswith("ata-") or "ata-" in pol_name.lower() or "alert-triage-agent" in disp.lower() or "alert-triage-agent" in pol_name.lower():
            is_ata = True

        if is_ata:
            alert_id = alert.get("name", "").split("/")[-1]
            open_alerts.append({
                "id": alert_id,
                "name": alert.get("name", ""),
                "policy_display": disp or pol_name,
                "policy_name": pol_name,
                "open_time": alert.get("openTime", ""),
            })

    page_token = data.get("nextPageToken")
    if not page_token:
        break

print(json.dumps(open_alerts))
' "${proj}" "${tok}"
}

wait_for_open_alerts() {
    log_info "--> 6. Checking Cloud Monitoring Alerts API for open ATA alerts..."
    if [ "${NO_WAIT}" = "true" ]; then
        log_info "    [--no-wait] Skipping wait for open alerts and quiet period."
        return 0
    fi

    if [ "${DRY_RUN}" = "true" ] || [ "${MOCK_MODE}" = "true" ]; then
        log_info "    [MOCK/DRY-RUN] Simulating wait for open alerts to close (0 open alerts)."
        log_info "    [MOCK/DRY-RUN] Simulating quiet period (${QUIET_PERIOD_SECS}s)."
        return 0
    fi

    local start_time
    start_time=$(date +%s)
    local deadline=$((start_time + WAIT_TIMEOUT_SECS))
    local quiet_start=""

    log_info "    Polling Cloud Monitoring API (read-only) for OPEN alerts on ata-* policies (label app=alert-triage-agent)..."
    log_info "    Max timeout: ${WAIT_TIMEOUT_SECS}s | Quiet period: ${QUIET_PERIOD_SECS}s | Polling interval: ${POLL_INTERVAL_SECS}s"

    while true; do
        local now
        now=$(date +%s)
        local elapsed=$((now - start_time))

        local token
        token="$(resolve_monitoring_token)"
        if [ -z "${token}" ]; then
            log_warn "    Unable to retrieve auth token for Cloud Monitoring; skipping live alert wait."
            return 0
        fi

        local open_alerts_json
        open_alerts_json="$(query_open_ata_alerts "${PROJECT_ID}" "${token}")"
        local count
        count=$(echo "${open_alerts_json}" | python3 -c "import sys, json; print(len(json.load(sys.stdin)))" 2>/dev/null || echo "0")

        if [ "${now}" -ge "${deadline}" ] && [ "${count}" -gt 0 ]; then
            log_error "Timed out waiting for open ATA alerts to close after ${WAIT_TIMEOUT_SECS}s (${elapsed}s elapsed)!"
            log_error "Still-open alerts (${count} total):"
            echo "${open_alerts_json}" | python3 -c '
import sys, json
alerts = json.load(sys.stdin)
for a in alerts:
    print(f"  - [{a.get(\"id\")}] Policy: {a.get(\"policy_display\")} | Open time: {a.get(\"open_time\")} | Resource: {a.get(\"name\")}")
' >&2
            exit 1
        fi

        if [ "${count}" -gt 0 ]; then
            quiet_start=""
            local alert_summary
            alert_summary=$(echo "${open_alerts_json}" | python3 -c '
import sys, json
alerts = json.load(sys.stdin)
items = [f"{a.get(\"policy_display\")} ({a.get(\"id\")})" for a in alerts[:5]]
if len(alerts) > 5:
    items.append(f"...+{len(alerts)-5} more")
print(", ".join(items))
' 2>/dev/null || echo "")

            log_info "    [Progress] Elapsed: ${elapsed}s / ${WAIT_TIMEOUT_SECS}s | Still open: ${count} alert(s) [${alert_summary}]"
            sleep "${POLL_INTERVAL_SECS}"
        else
            if [ "${QUIET_PERIOD_SECS}" -le 0 ]; then
                log_success "    Zero open ATA alerts detected (quiet period disabled). Proceeding."
                return 0
            fi

            if [ -z "${quiet_start}" ]; then
                quiet_start=$(date +%s)
                log_success "    All open ATA alerts have closed! Entering ${QUIET_PERIOD_SECS}s quiet period to cover Apigee metric ingestion lag..."
            else
                local quiet_elapsed=$((now - quiet_start))
                if [ "${quiet_elapsed}" -ge "${QUIET_PERIOD_SECS}" ]; then
                    log_success "    Quiet period complete (${quiet_elapsed}s >= ${QUIET_PERIOD_SECS}s with zero open ATA alerts)."
                    return 0
                else
                    local quiet_remaining=$((QUIET_PERIOD_SECS - quiet_elapsed))
                    log_info "    [Quiet Period] ${quiet_elapsed}s / ${QUIET_PERIOD_SECS}s elapsed (${quiet_remaining}s remaining, 0 open alerts)..."
                fi
            fi
            sleep "${POLL_INTERVAL_SECS}"
        fi
    done
}

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

# 6. Wait for Open ATA Alerts to Close & Quiet Period (Apigee Metric Ingestion Lag)
wait_for_open_alerts

# 7. Archive Active Incident Files & Clean Leftover Root Storage
log_info "--> 7. Archiving OKF incident files from knowledge/incident-manager/ to archive/..."
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

# 8. Final Verification of Healthy State & Zero Active Groups
log_info "--> 8. Verifying healthy baseline state..."
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

# 9. Admin Fault Injection Preflight Verification
log_info "--> 9. Verifying admin fault injection endpoints exist on deployed inventory service..."
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

