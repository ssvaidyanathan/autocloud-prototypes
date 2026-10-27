#!/usr/bin/env bash
# ==============================================================================
# Verify the triage tier of one stack after a deploy. Read-only by default: it creates no
# incident group and no ServiceNow ticket.
#
# Usage: scripts/verify-stage1.sh [--with-ticket [--wait-secs N]]
#
# Required: PROJECT_ID, REGION, NAME_PREFIX (every name derives from it).
# Checks:
#   1. /health of ${NAME_PREFIX}-{inventory-api,orders-api,agent,bridge,tools} (ID token): 200
#   2. the bridge webhook rejects a wrong token: 401
#   3. the bridge webhook accepts the real token (Secret Manager ${NAME_PREFIX}-webhook-token)
#      for an out-of-scope probe alert: 200 "ignored", nothing is enqueued
#   4. the agent's config points at this stack (BRIDGE_URL, TOOLS_URL, SERVICE_ACCOUNT_EMAIL)
#   5. the incidents bucket is readable; the newest OKF incident document, if any, has the
#      current schema (type: incident, group_id, a known status, alerts)
# --with-ticket (end to end; CREATES a real incident group and, after the settle timer, a
# ServiceNow ticket): sends a ${NAME_PREFIX}-inventory-5xx alert (expects 202), waits up to
# --wait-secs (default 180) for its OKF document and checks its schema, then sends the
# matching "closed" alert as cleanup. The ticket stays in ServiceNow: close it there, then run
# scripts/reset-scenarios.sh to archive the group.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud-run-deploy-lib.sh
source "${SCRIPT_DIR}/cloud-run-deploy-lib.sh"

WITH_TICKET=false
WAIT_SECS=180
while [ "$#" -gt 0 ]; do
    case "$1" in
        --with-ticket) WITH_TICKET=true; shift ;;
        --wait-secs)
            [ "$#" -ge 2 ] || { echo "--wait-secs needs a value" >&2; exit 1; }
            WAIT_SECS="$2"; shift 2 ;;
        -h|--help) sed -n '2,23p' "$0"; exit 0 ;;
        *) echo "Unknown argument: $1 (see --help)" >&2; exit 1 ;;
    esac
done
case "${WAIT_SECS}" in ""|*[!0-9]*) echo "--wait-secs must be a number of seconds" >&2; exit 1 ;; esac

require_vars PROJECT_ID REGION || exit 1
require_name_prefix || exit 1
BUCKET_NAME="${PROJECT_ID}-${NAME_PREFIX}-incidents"
OKF_PREFIX="gs://${BUCKET_NAME}/knowledge/incident-manager"
FAILURES=0

fail() {
    echo "    FAILED: $*" >&2
    FAILURES=$((FAILURES + 1))
}

echo "==================================================================="
echo "  Verifying stack '${NAME_PREFIX}' in ${PROJECT_ID}/${REGION}"
if [ "${WITH_TICKET}" = "true" ]; then
    echo "  --with-ticket: this run CREATES an incident group and a ServiceNow ticket."
fi
echo "==================================================================="

# Exact-name URL lookup; a missing service is a failure, never a fallback URL.
service_url() {
    local url
    url="$(existing_service_url "$1")" || exit 1
    if [ -z "${url}" ]; then
        echo "ERROR: Cloud Run service $1 is not deployed in ${PROJECT_ID}/${REGION}." >&2
        exit 1
    fi
    printf '%s\n' "${url}"
}

INVENTORY_URL="$(service_url "${NAME_PREFIX}-inventory-api")"
ORDERS_URL="$(service_url "${NAME_PREFIX}-orders-api")"
AGENT_URL="$(service_url "${NAME_PREFIX}-agent")"
BRIDGE_URL="$(service_url "${NAME_PREFIX}-bridge")"
TOOLS_URL="$(service_url "${NAME_PREFIX}-tools")"

ID_TOKEN="$(gcloud auth print-identity-token)" || { echo "ERROR: gcloud auth print-identity-token failed (gcloud auth login)." >&2; exit 1; }
[ -n "${ID_TOKEN}" ] || { echo "ERROR: empty identity token." >&2; exit 1; }
AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")

# http <curl args...>: sets HTTP_CODE ("000" without a response) and HTTP_BODY.
BODY_FILE="$(mktemp "${TMPDIR:-/tmp}/${NAME_PREFIX}-verify.XXXXXX")"
trap 'rm -f "${BODY_FILE}"' EXIT
http() {
    : > "${BODY_FILE}"
    HTTP_CODE="$(curl -s -o "${BODY_FILE}" -w "%{http_code}" --connect-timeout 5 --max-time 30 "$@")" || HTTP_CODE="000"
    HTTP_BODY="$(head -c 400 "${BODY_FILE}")"
}

echo ""
echo "--- 1. /health ---"
for pair in "inventory-api|${INVENTORY_URL}" "orders-api|${ORDERS_URL}" "agent|${AGENT_URL}" "bridge|${BRIDGE_URL}" "tools|${TOOLS_URL}"; do
    name="${NAME_PREFIX}-${pair%%|*}"
    url="${pair#*|}"
    http ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} "${url}/health"
    if [ "${HTTP_CODE}" = "200" ]; then
        echo "    ${name}: 200"
    else
        fail "${name} ${url}/health: HTTP ${HTTP_CODE} ${HTTP_BODY}"
    fi
done

echo ""
echo "--- 2-3. Bridge webhook authentication ---"
WEBHOOK_TOKEN="$(gcloud secrets versions access latest --secret="${NAME_PREFIX}-webhook-token" --project="${PROJECT_ID}")" \
    || { echo "ERROR: cannot read secret ${NAME_PREFIX}-webhook-token." >&2; exit 1; }
[ -n "${WEBHOOK_TOKEN}" ] || { echo "ERROR: secret ${NAME_PREFIX}-webhook-token is empty." >&2; exit 1; }
STAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
# Out of scope by design: no field names the stack or the app, so the bridge answers
# "ignored" and enqueues nothing.
PROBE_PAYLOAD="{\"version\": \"1.2\", \"incident\": {\"incident_id\": \"0.verify_probe_$(date +%s)\", \"policy_name\": \"verify-probe\", \"resource_name\": \"verify-probe\", \"state\": \"open\", \"started_at\": \"${STAMP}\", \"summary\": \"deploy verification probe\"}}"
http -X POST -H "Content-Type: application/json" "${BRIDGE_URL}/webhook/monitoring?token=wrong-${RANDOM}" -d "${PROBE_PAYLOAD}"
if [ "${HTTP_CODE}" = "401" ]; then
    echo "    wrong token: 401"
else
    fail "webhook with a wrong token: HTTP ${HTTP_CODE} (expected 401) ${HTTP_BODY}"
fi
http -X POST -H "Content-Type: application/json" "${BRIDGE_URL}/webhook/monitoring?token=${WEBHOOK_TOKEN}" -d "${PROBE_PAYLOAD}"
if [ "${HTTP_CODE}" = "200" ] && printf '%s' "${HTTP_BODY}" | grep -q '"ignored"'; then
    echo "    real token, out-of-scope probe: 200 ignored (nothing enqueued)"
else
    fail "webhook probe with the real token: HTTP ${HTTP_CODE} (expected 200 ignored) ${HTTP_BODY}"
fi

echo ""
echo "--- 4. Agent configuration ---"
AGENT_ENV="$(gcloud run services describe "${NAME_PREFIX}-agent" --project="${PROJECT_ID}" --region="${REGION}" --format=json \
    | python3 -c '
import json, sys
svc = json.load(sys.stdin)
for c in svc.get("spec", {}).get("template", {}).get("spec", {}).get("containers", []):
    for e in c.get("env", []) or []:
        if "value" in e:
            print("%s=%s" % (e["name"], e["value"]))
')" || { echo "ERROR: cannot describe ${NAME_PREFIX}-agent." >&2; exit 1; }
PROJECT_NUMBER="$(resolve_project_number)"
agent_env() { printf '%s\n' "${AGENT_ENV}" | grep -E "^$1=" | head -n 1 | cut -d= -f2-; }
check_url_env() {
    local name="$1" svc="$2" live="$3" value
    value="$(agent_env "${name}")"
    if [ "${value}" = "${live}" ] || [ "${value}" = "https://${svc}-${PROJECT_NUMBER}.${REGION}.run.app" ]; then
        echo "    ${name}=${value}"
    else
        fail "agent ${name}='${value}' is neither ${live} nor the deterministic URL of ${svc}"
    fi
}
check_url_env BRIDGE_URL "${NAME_PREFIX}-bridge" "${BRIDGE_URL}"
check_url_env TOOLS_URL "${NAME_PREFIX}-tools" "${TOOLS_URL}"
if [ "$(agent_env SERVICE_ACCOUNT_EMAIL)" = "${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com" ]; then
    echo "    SERVICE_ACCOUNT_EMAIL=${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
else
    fail "agent SERVICE_ACCOUNT_EMAIL='$(agent_env SERVICE_ACCOUNT_EMAIL)' is not this stack's agent SA"
fi

# check_okf <gs:// object>: the current OKF frontmatter (agent/app/grouping/models.py).
check_okf() {
    local object="$1" content
    content="$(gcloud storage cat "${object}" --project="${PROJECT_ID}")" || { fail "cannot read ${object}"; return 1; }
    if printf '%s\n' "${content}" | python3 -c '
import re, sys
text = sys.stdin.read()
m = re.match(r"---\n(.*?)\n---", text, re.S)
if not m:
    sys.exit("no YAML frontmatter")
keys = {}
for line in m.group(1).splitlines():
    km = re.match(r"([A-Za-z_]+):\s*(.*)$", line)
    if km:
        keys[km.group(1)] = km.group(2).strip().strip("\"'"'"'")
problems = []
if keys.get("type") != "incident":
    problems.append("type is %r, expected incident" % keys.get("type"))
if not keys.get("group_id"):
    problems.append("group_id missing")
if keys.get("status") not in {"open", "investigating", "ticketed", "cooling_down", "resolved", "recovered", "archived", "degraded"}:
    problems.append("unknown status %r" % keys.get("status"))
if "alerts" not in keys:
    problems.append("alerts missing")
if problems:
    sys.exit("; ".join(problems))
print("type=incident group_id=%s status=%s" % (keys["group_id"], keys["status"]))
'; then
        return 0
    fi
    fail "${object} does not have the current OKF schema (see above)"
    return 1
}

echo ""
echo "--- 5. Incident store (gs://${BUCKET_NAME}) ---"
LISTING="$(gcloud storage ls "${OKF_PREFIX}/" --project="${PROJECT_ID}" 2>&1)" || {
    case "${LISTING}" in
        *"matched no objects"*) LISTING="" ;;
        *) echo "${LISTING}" >&2; echo "ERROR: cannot list ${OKF_PREFIX}/." >&2; exit 1 ;;
    esac
}
NEWEST="$(printf '%s\n' "${LISTING}" | grep -E '/incident-[^/]*\.md$' | tail -n 1 || true)"
if [ -n "${NEWEST}" ]; then
    echo "    checking ${NEWEST}"
    check_okf "${NEWEST}" || true
else
    echo "    bucket readable; no incident documents yet (expected on a fresh stack)."
fi

if [ "${WITH_TICKET}" = "true" ]; then
    echo ""
    echo "--- 6. End to end (--with-ticket) ---"
    INCIDENT_ID="0.verify_e2e_$(date +%s)"
    e2e_payload() {
        printf '{"version": "1.2", "incident": {"incident_id": "%s", "policy_name": "%s-inventory-5xx", "condition_name": "Cloud Run 5xx rate high", "resource_name": "%s-inventory-api", "resource_type": "cloud_run_revision", "resource": {"type": "cloud_run_revision", "labels": {"service_name": "%s-inventory-api", "location": "%s", "project_id": "%s"}}, "state": "%s", "started_at": "%s", "summary": "%s-inventory-api deploy verification (verify-stage1.sh --with-ticket)"}}' \
            "${INCIDENT_ID}" "${NAME_PREFIX}" "${NAME_PREFIX}" "${NAME_PREFIX}" "${REGION}" "${PROJECT_ID}" "$1" "${STAMP}" "${NAME_PREFIX}"
    }
    http -X POST -H "Content-Type: application/json" "${BRIDGE_URL}/webhook/monitoring?token=${WEBHOOK_TOKEN}" -d "$(e2e_payload open)"
    if [ "${HTTP_CODE}" = "202" ] || [ "${HTTP_CODE}" = "200" ]; then
        echo "    alert ${INCIDENT_ID}: HTTP ${HTTP_CODE} ${HTTP_BODY}"
        waited=0
        FOUND=""
        while [ -z "${FOUND}" ] && [ "${waited}" -le "${WAIT_SECS}" ]; do
            for object in $(gcloud storage ls "${OKF_PREFIX}/" --project="${PROJECT_ID}" 2>/dev/null | grep -E '/incident-[^/]*\.md$' || true); do
                if gcloud storage cat "${object}" --project="${PROJECT_ID}" 2>/dev/null | grep -q "${INCIDENT_ID}"; then
                    FOUND="${object}"
                    break
                fi
            done
            if [ -z "${FOUND}" ]; then
                sleep 10
                waited=$((waited + 10))
            fi
        done
        if [ -n "${FOUND}" ]; then
            echo "    OKF document ${FOUND}"
            check_okf "${FOUND}" || true
        else
            fail "no OKF document mentions ${INCIDENT_ID} after ${WAIT_SECS} s"
        fi
    else
        fail "webhook alert: HTTP ${HTTP_CODE} (expected 202) ${HTTP_BODY}"
    fi
    echo "    cleanup: sending the closed alert for ${INCIDENT_ID}..."
    http -X POST -H "Content-Type: application/json" "${BRIDGE_URL}/webhook/monitoring?token=${WEBHOOK_TOKEN}" -d "$(e2e_payload closed)"
    case "${HTTP_CODE}" in
        200|202) echo "    closed alert: HTTP ${HTTP_CODE}" ;;
        *) fail "closed alert: HTTP ${HTTP_CODE} ${HTTP_BODY}" ;;
    esac
    echo "    The ServiceNow ticket of this group (created after the settle timer) stays open:"
    echo "    close it in ServiceNow, then run scripts/reset-scenarios.sh to archive the group."
fi

echo ""
if [ "${FAILURES}" -gt 0 ]; then
    echo "=== Verification FAILED: ${FAILURES} check(s) ===" >&2
    exit 1
fi
echo "=== Verification passed ==="
