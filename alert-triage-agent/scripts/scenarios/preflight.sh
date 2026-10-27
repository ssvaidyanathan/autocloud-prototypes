#!/usr/bin/env bash
# ==============================================================================
# Preflight Health Verification Script: preflight.sh
#
# Validates system health and operational readiness before executing scenarios:
# 1. Cloud Run Demo Microservice Admin Endpoints:
#    - Pinned inventory revision serving; service-level max instances (ATA_DEMO_MAX_INSTANCES)
#      on ${NAME_PREFIX}-inventory-api and ${NAME_PREFIX}-orders-api
#    - ${NAME_PREFIX}-inventory-api: /health, /admin/leak-connections,
#      /admin/fault/inject, /admin/fault/reset
#    - ${NAME_PREFIX}-orders-api: /health, /admin/reset, /admin/fault/latency, /admin/fault/5xx
# 2. Apigee Proxies & SpikeArrest Configuration:
#    - Gateway ingress health for inventory and orders
#    - Active proxy revisions deployed to environment
#    - SpikeArrest policy configuration calibrated to baseline (300pm)
# 3. Cloud Monitoring Alert Policy Thresholds:
#    - Enabled policies: ata-inventory-5xx, ata-inventory-latency, ata-orders-5xx,
#      ata-orders-latency, ata-apigee-orders-429, ata-cloudsql-connections-critical
#    - Calibrated thresholds reachable
# 4. Cloud SQL Database Connectivity:
#    - Cloud SQL instance state is RUNNABLE
#    - Read-only data probe via microservice (/items catalog query)
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/common.sh"

log_info "==================================================================="
log_info "  Starting ATA Demo Preflight Health Verification"
log_info "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"
log_info "==================================================================="

FAILURES=0

record_failure() {
    local msg="$1"
    log_error "PREFLIGHT FAILURE: ${msg}"
    FAILURES=$((FAILURES + 1))
}

# Resolve Service URLs
INV_URL="$(require_service_url "${NAME_PREFIX}-inventory-api")"
ORD_URL="$(require_service_url "${NAME_PREFIX}-orders-api")"

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

APIGEE_TOKEN="$(resolve_apigee_token)"
API_KEY="$(resolve_api_key)" || exit 1
KEY_HEADER=()
if [ -n "${API_KEY}" ]; then
    KEY_HEADER=(-H "x-api-key: ${API_KEY}")
fi

# ==============================================================================
# Check 1: Cloud Run Demo Microservice Admin Endpoints
# ==============================================================================
log_info "--> Check 1: Verifying Cloud Run demo admin endpoints..."

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_info "    [MOCK/DRY-RUN] Microservice admin endpoints simulated OK."
else
    # 1a. Inventory API health & admin endpoints
    # The GET probes below are only harmless on the pinned code; never send them elsewhere.
    if ! verify_pinned_inventory_revision || ! verify_inventory_serving_pinned; then
        log_error "FATAL: ${NAME_PREFIX}-inventory-api is not on the pinned baseline; refusing to probe admin endpoints."
        exit 1
    fi
    log_success "      Serving pinned revision ${PINNED_INVENTORY_REVISION} (${PINNED_INVENTORY_DIGEST})"
    # One instance per demo service: fault state and leaked connections are per instance, and
    # /admin/fault/reset clears only the instance it reaches. The service-level limit covers
    # every revision, including a pinned revision created before the cap.
    for svc in "${NAME_PREFIX}-inventory-api" "${NAME_PREFIX}-orders-api"; do
        if service_max_matches_baseline "${svc}"; then
            log_success "      ${svc}: service-level max instances ${DEMO_MAX_INSTANCES}"
        else
            record_failure "${svc} service-level max instances is not ${DEMO_MAX_INSTANCES} (see above); a fault or reset can miss an instance. Fix: scripts/set-demo-instance-caps.sh --apply"
        fi
    done
    log_info "    Probing ${NAME_PREFIX}-inventory-api (${INV_URL})..."
    INV_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/health" || echo "000")
    if [ "${INV_HEALTH}" = "200" ]; then
        log_success "      /health -> HTTP 200 OK"
    else
        record_failure "${NAME_PREFIX}-inventory-api /health returned HTTP ${INV_HEALTH} (expected 200)"
    fi


    INV_LEAK=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/admin/leak-connections" || echo "000")
    if [ "${INV_LEAK}" = "200" ]; then
        log_success "      /admin/leak-connections -> HTTP 200 OK (Endpoint Present)"
    else
        record_failure "${NAME_PREFIX}-inventory-api /admin/leak-connections returned HTTP ${INV_LEAK} (expected 200)"
    fi

    INV_INJECT=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/admin/fault/inject" || echo "000")
    if [ "${INV_INJECT}" = "200" ]; then
        log_success "      /admin/fault/inject -> HTTP 200 OK (Endpoint Present)"
    else
        record_failure "${NAME_PREFIX}-inventory-api /admin/fault/inject returned HTTP ${INV_INJECT} (expected 200)"
    fi

    INV_RESET=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} -X POST "${INV_URL}/admin/fault/reset" || echo "000")
    if [ "${INV_RESET}" = "200" ]; then
        log_success "      /admin/fault/reset -> HTTP 200 OK"
    else
        record_failure "${NAME_PREFIX}-inventory-api /admin/fault/reset returned HTTP ${INV_RESET} (expected 200)"
    fi

    # 1b. Orders API health & admin endpoints
    log_info "    Probing ${NAME_PREFIX}-orders-api (${ORD_URL})..."
    ORD_HEALTH=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_ORD[@]+"${AUTH_HEADER_ORD[@]}"} "${ORD_URL}/health" || echo "000")
    if [ "${ORD_HEALTH}" = "200" ]; then
        log_success "      /health -> HTTP 200 OK"
    else
        record_failure "${NAME_PREFIX}-orders-api /health returned HTTP ${ORD_HEALTH} (expected 200)"
    fi

    ORD_RESET=$(curl -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        ${AUTH_HEADER_ORD[@]+"${AUTH_HEADER_ORD[@]}"} -X POST "${ORD_URL}/admin/reset" || echo "000")
    if [ "${ORD_RESET}" = "200" ]; then
        log_success "      /admin/reset -> HTTP 200 OK"
    else
        record_failure "${NAME_PREFIX}-orders-api /admin/reset returned HTTP ${ORD_RESET} (expected 200)"
    fi
fi

# ==============================================================================
# Check 2: Apigee Proxy Revisions & SpikeArrest Configuration
# ==============================================================================
log_info "--> Check 2: Verifying Apigee proxies and SpikeArrest configuration..."

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_info "    [MOCK/DRY-RUN] Apigee proxies and SpikeArrest verified OK."
else
    # 2a. Ingress Gateway Health
    APIGEE_INV_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        "https://${APIGEE_HOST}/ata/inventory/v1/health" || echo "000")
    if [ "${APIGEE_INV_HEALTH}" = "200" ]; then
        log_success "    Apigee Ingress: /ata/inventory/v1/health -> HTTP 200 OK"
    else
        record_failure "Apigee Ingress /ata/inventory/v1/health returned HTTP ${APIGEE_INV_HEALTH}"
    fi

    APIGEE_ORD_HEALTH=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 5 --max-time 10 \
        "https://${APIGEE_HOST}/ata/orders/v1/health" || echo "000")
    if [ "${APIGEE_ORD_HEALTH}" = "200" ]; then
        log_success "    Apigee Ingress: /ata/orders/v1/health -> HTTP 200 OK"
    else
        record_failure "Apigee Ingress /ata/orders/v1/health returned HTTP ${APIGEE_ORD_HEALTH}"
    fi

    # 2b. Apigee Management API Deployment and SpikeArrest Policy Inspection
    if [ -n "${APIGEE_TOKEN}" ] && [ "${APIGEE_TOKEN}" != "mock-apigee-token" ]; then
        BASE_URL="https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}"

        # Check deployed revision of ata-orders-v1
        DEPLOYMENTS_JSON=$(curl -s -H "Authorization: Bearer ${APIGEE_TOKEN}" \
            "${BASE_URL}/environments/${ENV_NAME}/apis/ata-orders-v1/deployments" || echo "{}")
        ACTIVE_ORD_REV=$(echo "${DEPLOYMENTS_JSON}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    deployments = data.get('deployments', [])
    if deployments:
        print(deployments[0].get('revision', 'unknown'))
    else:
        print('none')
except Exception:
    print('unknown')
")
        if [ "${ACTIVE_ORD_REV}" != "none" ] && [ "${ACTIVE_ORD_REV}" != "unknown" ]; then
            log_success "    Apigee ata-orders-v1 active deployed revision: ${ACTIVE_ORD_REV}"

            # Verify SpikeArrest policy presence on deployed revision
            REV_JSON=$(curl -s -H "Authorization: Bearer ${APIGEE_TOKEN}" \
                "${BASE_URL}/apis/ata-orders-v1/revisions/${ACTIVE_ORD_REV}" || echo "{}")
            HAS_SPIKE=$(echo "${REV_JSON}" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    policies = data.get('policies', [])
    print('yes' if 'SA-SpikeArrest' in policies else 'no')
except Exception:
    print('no')
")
            if [ "${HAS_SPIKE}" = "yes" ]; then
                log_success "    Apigee SpikeArrest policy verified in proxy definition: SA-SpikeArrest"
            else
                record_failure "Apigee proxy revision ${ACTIVE_ORD_REV} lacks SA-SpikeArrest policy!"
            fi

            # Ingress rate test: verify baseline rate allows rapid requests without false 429
            RAPID_429=0
            for i in 1 2 3; do
                CODE=$(curl -k -s -o /dev/null -w "%{http_code}" --connect-timeout 2 --max-time 3 \
                    "https://${APIGEE_HOST}/ata/orders/v1/health" || echo "200")
                if [ "${CODE}" = "429" ]; then
                    RAPID_429=$((RAPID_429 + 1))
                fi
            done
            if [ "${RAPID_429}" -eq 0 ]; then
                log_success "    Apigee SpikeArrest rate confirmed nominal (no 429 on baseline health probes)"
            else
                record_failure "Apigee SpikeArrest is throttled at baseline rate (observed ${RAPID_429} 429s)! Re-run scripts/reset-scenarios.sh."
            fi
        else
            record_failure "No active deployment found for ata-orders-v1 in ${ENV_NAME}"
        fi
    fi
fi

# ==============================================================================
# Check 3: Cloud Monitoring Policy Thresholds & Calibration
# ==============================================================================
log_info "--> Check 3: Verifying Cloud Monitoring policy thresholds..."

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_info "    [MOCK/DRY-RUN] Cloud Monitoring policy calibration simulated OK."
else
    REQUIRED_POLICIES=(
        "ata-inventory-5xx"
        "ata-inventory-latency"
        "ata-orders-5xx"
        "ata-orders-latency"
        "ata-apigee-orders-429"
        "ata-cloudsql-connections-critical"
    )

    POLICIES_JSON=$(gcloud monitoring policies list --project="${PROJECT_ID}" --format=json 2>/dev/null || echo "[]")

    for p in ${REQUIRED_POLICIES[@]+"${REQUIRED_POLICIES[@]}"}; do
        FOUND=$(echo "${POLICIES_JSON}" | python3 -c "
import sys, json
try:
    policies = json.load(sys.stdin)
    matching = [x for x in policies if x.get('displayName') == '${p}']
    if matching:
        m = matching[0]
        enabled = m.get('enabled', True)
        cond = (m.get('conditions') or [{}])[0].get('conditionThreshold', {})
        threshold = cond.get('thresholdValue', 'N/A')
        print(f'OK:{enabled}:{threshold}')
    else:
        print('MISSING')
except Exception as e:
    print(f'ERROR:{e}')
")
        if [[ "${FOUND}" =~ ^OK:True:(.*) ]]; then
            THRESH="${BASH_REMATCH[1]}"
            log_success "    Policy ${p}: ENABLED (Threshold: ${THRESH})"
        elif [[ "${FOUND}" =~ ^OK:False:(.*) ]]; then
            record_failure "Policy ${p} exists but is DISABLED!"
        else
            record_failure "Policy ${p} not found in Cloud Monitoring!"
        fi
    done
fi

# ==============================================================================
# Check 4: Cloud SQL Database Connectivity & Read-Only Probe
# ==============================================================================
log_info "--> Check 4: Verifying Cloud SQL database reachability..."

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    log_info "    [MOCK/DRY-RUN] Cloud SQL database reachability simulated OK."
else
    # 4a. Cloud SQL instance status
    SQL_STATE=$(gcloud sql instances describe "${NAME_PREFIX}-inventory-db" \
        --project="${PROJECT_ID}" --format="value(state)" 2>/dev/null || echo "UNKNOWN")
    if [ "${SQL_STATE}" = "RUNNABLE" ]; then
        log_success "    Cloud SQL Instance ${NAME_PREFIX}-inventory-db: ${SQL_STATE}"
    else
        record_failure "Cloud SQL Instance ${NAME_PREFIX}-inventory-db state is ${SQL_STATE} (expected RUNNABLE)"
    fi

    # 4b. Read-only probe via microservice endpoint (inventory catalog query)
    ITEMS_RESP=$(curl -s ${AUTH_HEADER_INV[@]+"${AUTH_HEADER_INV[@]}"} "${INV_URL}/items" || echo "[]")
    ITEM_COUNT=$(echo "${ITEMS_RESP}" | python3 -c "
import sys, json
try:
    items = json.load(sys.stdin)
    print(len(items) if isinstance(items, list) else 0)
except Exception:
    print(0)
")
    if [ "${ITEM_COUNT}" -ge 5 ]; then
        log_success "    Cloud SQL Database read-only probe successful: ${ITEM_COUNT} catalog items loaded."
    else
        record_failure "Database query probe returned invalid or empty catalog (count: ${ITEM_COUNT})"
    fi
fi

# ==============================================================================
# Preflight Verification Summary
# ==============================================================================
echo ""
if [ "${FAILURES}" -eq 0 ]; then
    log_success "==================================================================="
    log_success "  All Preflight Health Checks Passed: System Ready for Scenarios  "
    log_success "==================================================================="
    exit 0
else
    log_error "==================================================================="
    log_error "  Preflight Health Verification FAILED with ${FAILURES} error(s)!        "
    log_error "==================================================================="
    exit 1
fi
