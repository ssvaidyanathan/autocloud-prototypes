#!/usr/bin/env bash
# Deploy the demo Apigee proxies for one NAME_PREFIX stack.
#
# Required (no defaults that point at any existing environment):
#   PROJECT_ID    GCP project hosting the ${NAME_PREFIX}-{orders,inventory}-api Cloud Run services
#   REGION        Cloud Run region of those services
#   NAME_PREFIX   stack prefix, [a-z][a-z0-9]{1,9}; every Apigee resource touched is "${NAME_PREFIX}-*"
#   APIGEE_ENV    Apigee environment to deploy into
#   APIGEE_HOST   envgroup hostname that serves APIGEE_ENV (written to the env file for the scenarios)
# Optional:
#   APIGEE_ORG    Apigee organization; defaults to PROJECT_ID (echoed when it does)
#   ATA_STACK_DIR          where the key and env files go (default: ALERT_TRIAGE_DIR, else this
#                          checkout's alert-triage-agent/; ALERT_TRIAGE_DIR is the older name)
#   DEPLOY_TIMEOUT_SECS    deployment READY timeout per proxy (default 300)
#   DEPLOY_POLL_SECS       poll interval (default 5)
#
# Creates or updates (idempotent), and prints this exact list before changing anything:
#   TargetServers ${NAME_PREFIX}-orders-target, ${NAME_PREFIX}-inventory-target (in APIGEE_ENV)
#   Proxies ${NAME_PREFIX}-orders-v1 (/${NAME_PREFIX}/orders/v1), ${NAME_PREFIX}-inventory-v1
#     (/${NAME_PREFIX}/inventory/v1): new revision imported and deployed with override=true,
#     running as ${NAME_PREFIX}-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com, ONLY when the
#     rendered bundle differs from the revision deployed in APIGEE_ENV (see deployed_matches);
#     an unchanged proxy is left alone, so a reset adds no revision and no deployment
#   API product ${NAME_PREFIX}-product, developer ${NAME_PREFIX}-dev@example.com,
#     app ${NAME_PREFIX}-demo-app
# Writes:
#   ${ATA_STACK_DIR}/.${NAME_PREFIX}-api-key      consumer key of ${NAME_PREFIX}-demo-app (mode 600)
#   ${ATA_STACK_DIR}/.${NAME_PREFIX}-apigee.env   APIGEE_HOST=, APIGEE_ORG=, APIGEE_ENV= (sourceable)
#
# Every management API call is checked: any non-2xx fails with the response body. 409 on a
# create means "exists" and is followed by an update. TargetServers, the product and the
# developer are read first and not written when they already hold the wanted values.
# Undo with scripts/teardown-apigee.sh.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUNDLE_DIR="${SCRIPT_DIR}/../demo/apigee"
# shellcheck source-path=SCRIPTDIR source=../demo/apigee/apigee-lib.sh
. "${BUNDLE_DIR}/apigee-lib.sh"

for v in PROJECT_ID REGION NAME_PREFIX APIGEE_ENV APIGEE_HOST; do
    apigee_require_var "${v}" "see the header of scripts/deploy-apigee-proxies.sh"
done
apigee_validate_prefix "${NAME_PREFIX}" || apigee_die "NAME_PREFIX '${NAME_PREFIX}' must match [a-z][a-z0-9]{1,9}."
if [ -z "${APIGEE_ORG:-}" ]; then
    APIGEE_ORG="${PROJECT_ID}"
    echo "NOTE: APIGEE_ORG not set; using PROJECT_ID as the Apigee organization: ${APIGEE_ORG}"
fi
for v in PROJECT_ID REGION APIGEE_ORG APIGEE_ENV APIGEE_HOST; do
    eval "val=\"\${${v}}\""
    # shellcheck disable=SC2154
    apigee_safe_token "${val}" || apigee_die "${v}='${val}' contains unexpected characters."
done
ATA_STACK_DIR="${ATA_STACK_DIR:-${ALERT_TRIAGE_DIR:-$(cd "${SCRIPT_DIR}/.." && pwd)}}"
[ -d "${ATA_STACK_DIR}" ] || apigee_die "ATA_STACK_DIR ${ATA_STACK_DIR} is not a directory."
DEPLOY_TIMEOUT_SECS="${DEPLOY_TIMEOUT_SECS:-300}"
DEPLOY_POLL_SECS="${DEPLOY_POLL_SECS:-5}"

P="${NAME_PREFIX}"
PROXIES="${P}-orders-v1 ${P}-inventory-v1"
TS_ORDERS="${P}-orders-target"
TS_INVENTORY="${P}-inventory-target"
PRODUCT="${P}-product"
DEVELOPER="${P}-dev@example.com"
APP="${P}-demo-app"
SA_EMAIL="${P}-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com"
KEY_FILE="${ATA_STACK_DIR}/.${P}-api-key"
ENV_FILE="${ATA_STACK_DIR}/.${P}-apigee.env"
BASE_URL="https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"
ENV_URL="${BASE_URL}/environments/${APIGEE_ENV}"

for name in ${PROXIES} "${TS_ORDERS}" "${TS_INVENTORY}" "${PRODUCT}" "${DEVELOPER}" "${APP}"; do
    apigee_assert_owned "${name}"
done

echo "=== Deploying Apigee proxies for prefix '${P}' ==="
echo "    org=${APIGEE_ORG} env=${APIGEE_ENV} host=${APIGEE_HOST} project=${PROJECT_ID} region=${REGION}"

# --- TargetServer hosts: the stack's own Cloud Run services, or fail. ---
service_host() {
    local svc="$1" url
    url="$(gcloud run services describe "${svc}" --region="${REGION}" --project="${PROJECT_ID}" \
        --format='value(status.url)')" || apigee_die "gcloud run services describe ${svc} failed (project ${PROJECT_ID}, region ${REGION})."
    url="$(printf '%s' "${url}" | sed -e 's|^https://||' -e 's|/.*$||')"
    [ -n "${url}" ] || apigee_die "Cloud Run service ${svc} has no status.url in ${PROJECT_ID}/${REGION}; deploy it first."
    printf '%s\n' "${url}"
}
ORDERS_HOST="$(service_host "${P}-orders-api")"
INVENTORY_HOST="$(service_host "${P}-inventory-api")"
apigee_safe_token "${ORDERS_HOST}" || apigee_die "unexpected orders host '${ORDERS_HOST}'."
apigee_safe_token "${INVENTORY_HOST}" || apigee_die "unexpected inventory host '${INVENTORY_HOST}'."

echo ""
echo "The following Apigee resources in organizations/${APIGEE_ORG} will be created or updated:"
echo "  targetserver  ${APIGEE_ENV}/${TS_ORDERS} -> ${ORDERS_HOST}:443"
echo "  targetserver  ${APIGEE_ENV}/${TS_INVENTORY} -> ${INVENTORY_HOST}:443"
for proxy in ${PROXIES}; do
    echo "  proxy         ${proxy} (new revision, deployed to ${APIGEE_ENV} with override=true, SA ${SA_EMAIL};"
    echo "                skipped when the deployed revision already matches the rendered bundle)"
done
echo "  apiproduct    ${PRODUCT}"
echo "  developer     ${DEVELOPER}"
echo "  app           ${DEVELOPER}/${APP}"
echo "Local files: ${KEY_FILE} ${ENV_FILE}"
echo ""

APIGEE_TOKEN="$(gcloud auth print-access-token)" || apigee_die "gcloud auth print-access-token failed."
[ -n "${APIGEE_TOKEN}" ] || apigee_die "gcloud auth print-access-token returned an empty token."

apigee_api GET "${ENV_URL}"
apigee_ok || apigee_fail_call "Checking environment ${APIGEE_ENV} in org ${APIGEE_ORG}"

WORK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/apigee-deploy-${P}.XXXXXX")"
trap 'rm -rf "${WORK_DIR}"' EXIT

# create_or_update DESC CREATE_URL UPDATE_URL JSON
# A resource that already holds every field of JSON is left alone (no write, no new
# lastModifiedAt). A failed read only costs that shortcut: the create/update path runs.
create_or_update() {
    local desc="$1" create_url="$2" update_url="$3" json="$4"
    apigee_api GET "${update_url}"
    if apigee_ok && apigee_json_contains "${json}"; then
        echo "    ${desc} unchanged; not updated."
        return 0
    fi
    case "${API_CODE}" in
        2[0-9][0-9]|404) ;;
        *) echo "WARNING: reading ${desc} returned HTTP ${API_CODE}; creating or updating it." >&2 ;;
    esac
    apigee_api POST "${create_url}" -H "Content-Type: application/json" -d "${json}"
    if [ "${API_CODE}" = "409" ]; then
        echo "    ${desc} exists; updating."
        apigee_api PUT "${update_url}" -H "Content-Type: application/json" -d "${json}"
        apigee_ok || apigee_fail_call "Updating ${desc}"
    elif apigee_ok; then
        echo "    ${desc} created."
    else
        apigee_fail_call "Creating ${desc}"
    fi
}

ensure_targetserver() {
    local name="$1" host="$2"
    echo "--> TargetServer ${name} -> ${host}"
    create_or_update "TargetServer ${name}" "${ENV_URL}/targetservers" "${ENV_URL}/targetservers/${name}" \
        "{\"name\": \"${name}\", \"host\": \"${host}\", \"port\": 443, \"isEnabled\": true, \"sSLInfo\": {\"enabled\": true}}"
}

proxy_warn() {
    echo "WARNING: ${1}: ${2}; importing and deploying the rendered bundle." >&2
}

# deployed_matches PROXY ZIP
# 0 only when PROXY has exactly one deployment in APIGEE_ENV, it runs as SA_EMAIL, it is
# READY, and its revision's bundle has the same content as ZIP (apigee_bundles_equal; Apigee
# exposes no content hash for a revision, its metadata has only timestamps). Sets
# DEPLOYED_REVISION. Every doubt returns 1, with a WARNING unless the bundles plainly differ.
deployed_matches() {
    local proxy="$1" zip="$2" count revision sa state diff rc=0 deployed_zip
    DEPLOYED_REVISION=""
    apigee_api GET "${ENV_URL}/apis/${proxy}/deployments"
    if [ "${API_CODE}" = "404" ]; then
        proxy_warn "${proxy}" "not deployed in ${APIGEE_ENV} (HTTP 404: no such proxy yet)"
        return 1
    elif ! apigee_ok; then
        proxy_warn "${proxy}" "could not read its deployments in ${APIGEE_ENV} (HTTP ${API_CODE})"
        return 1
    fi
    count="$(apigee_json 'len(d.get("deployments") or [])')" || count=""
    case "${count}" in
        0) proxy_warn "${proxy}" "not deployed in ${APIGEE_ENV}"; return 1 ;;
        1) ;;
        ''|*[!0-9]*) proxy_warn "${proxy}" "unreadable deployments response"; return 1 ;;
        *) proxy_warn "${proxy}" "${count} deployments in ${APIGEE_ENV} (a rollout in progress?)"; return 1 ;;
    esac
    revision="$(apigee_json 'd["deployments"][0].get("revision")')" || revision=""
    case "${revision}" in
        ''|*[!0-9]*) proxy_warn "${proxy}" "deployment has no numeric revision"; return 1 ;;
    esac
    sa="$(apigee_json 'd["deployments"][0].get("serviceAccount")')" || sa=""
    if [ "${sa}" != "projects/-/serviceAccounts/${SA_EMAIL}" ]; then
        proxy_warn "${proxy}" "deployed revision ${revision} runs as '${sa:-<none>}', not ${SA_EMAIL}"
        return 1
    fi
    apigee_api GET "${ENV_URL}/apis/${proxy}/revisions/${revision}/deployments"
    state=""
    if apigee_ok; then
        state="$(apigee_json 'd.get("state")')" || state=""
    fi
    if [ "${state}" != "READY" ]; then
        proxy_warn "${proxy}" "deployed revision ${revision} is not READY (HTTP ${API_CODE}, state '${state}')"
        return 1
    fi
    deployed_zip="${WORK_DIR}/${proxy}-deployed-r${revision}.zip"
    apigee_download "${BASE_URL}/apis/${proxy}/revisions/${revision}?format=bundle" "${deployed_zip}"
    if ! apigee_ok; then
        proxy_warn "${proxy}" "could not download revision ${revision} (HTTP ${API_CODE})"
        return 1
    fi
    diff="$(apigee_bundles_equal "${deployed_zip}" "${zip}")" || rc=$?
    case "${rc}" in
        0) DEPLOYED_REVISION="${revision}"; return 0 ;;
        1) echo "    ${proxy}: deployed revision ${revision} differs from the rendered bundle (${diff})."; return 1 ;;
        *) proxy_warn "${proxy}" "could not compare revision ${revision} with the rendered bundle (${diff:-python3 failed})"; return 1 ;;
    esac
}

deploy_proxy() {
    local proxy="$1" template="${1#"${P}"-}" zip revision state waited=0
    zip="${WORK_DIR}/${proxy}.zip"
    echo "--> Packaging ${proxy} from template demo/apigee/${template}"
    "${BUNDLE_DIR}/render-bundle.sh" "${template}" "${P}" "${WORK_DIR}/${proxy}" "${zip}" > /dev/null

    if deployed_matches "${proxy}" "${zip}"; then
        echo "    ${proxy}: deployed revision ${DEPLOYED_REVISION} matches the rendered bundle; not redeployed."
        return 0
    fi

    echo "--> Importing ${proxy}"
    apigee_api POST "${BASE_URL}/apis?name=${proxy}&action=import" -F "file=@${zip}"
    apigee_ok || apigee_fail_call "Importing ${proxy}"
    revision="$(apigee_json 'd.get("revision")')" || revision=""
    case "${revision}" in
        ''|*[!0-9]*) apigee_fail_call "Importing ${proxy} (response has no numeric revision)" ;;
    esac
    echo "    Imported revision ${revision}"

    echo "--> Deploying ${proxy} revision ${revision} to ${APIGEE_ENV}"
    apigee_api POST "${ENV_URL}/apis/${proxy}/revisions/${revision}/deployments?override=true&serviceAccount=projects/-/serviceAccounts/${SA_EMAIL}"
    apigee_ok || apigee_fail_call "Deploying ${proxy} revision ${revision} (check that ${SA_EMAIL} exists and you hold iam.serviceAccountUser on it)"

    while :; do
        apigee_api GET "${ENV_URL}/apis/${proxy}/revisions/${revision}/deployments"
        apigee_ok || apigee_fail_call "Reading deployment of ${proxy} revision ${revision}"
        state="$(apigee_json 'd.get("state")')" || state=""
        case "${state}" in
            READY) echo "    ${proxy} revision ${revision} is READY"; return 0 ;;
            ERROR) apigee_fail_call "Deployment of ${proxy} revision ${revision} (state ERROR)" ;;
        esac
        if [ "${waited}" -ge "${DEPLOY_TIMEOUT_SECS}" ]; then
            apigee_fail_call "Waiting ${DEPLOY_TIMEOUT_SECS}s for ${proxy} revision ${revision} to be READY (last state '${state}')"
        fi
        sleep "${DEPLOY_POLL_SECS}"
        waited=$((waited + DEPLOY_POLL_SECS))
        [ "${DEPLOY_POLL_SECS}" -gt 0 ] || waited=$((waited + 1))
    done
}

echo "--> Provisioning TargetServers"
ensure_targetserver "${TS_ORDERS}" "${ORDERS_HOST}"
ensure_targetserver "${TS_INVENTORY}" "${INVENTORY_HOST}"

for proxy in ${PROXIES}; do
    deploy_proxy "${proxy}"
done

echo "--> API product, developer and app"
PROXY_JSON="$(for proxy in ${PROXIES}; do printf '"%s",' "${proxy}"; done)"
create_or_update "API product ${PRODUCT}" "${BASE_URL}/apiproducts" "${BASE_URL}/apiproducts/${PRODUCT}" \
    "{\"name\": \"${PRODUCT}\", \"displayName\": \"${PRODUCT}\", \"approvalType\": \"auto\", \"environments\": [\"${APIGEE_ENV}\"], \"proxies\": [${PROXY_JSON%,}]}"
create_or_update "Developer ${DEVELOPER}" "${BASE_URL}/developers" "${BASE_URL}/developers/${DEVELOPER}" \
    "{\"email\": \"${DEVELOPER}\", \"firstName\": \"${P}\", \"lastName\": \"Developer\", \"userName\": \"${P}-dev\"}"

# The key must belong to ${APP} and carry ${PRODUCT}.
app_key() {
    apigee_json '(lambda creds: next((c["consumerKey"] for c in creds if c.get("status", "approved") == "approved" and any(p.get("apiproduct") == "'"${PRODUCT}"'" for p in c.get("apiProducts", []))), None))(d.get("credentials", []) if d.get("name") == "'"${APP}"'" else [])'
}
APP_URL="${BASE_URL}/developers/${DEVELOPER}/apps"
apigee_api POST "${APP_URL}" -H "Content-Type: application/json" \
    -d "{\"name\": \"${APP}\", \"apiProducts\": [\"${PRODUCT}\"]}"
if [ "${API_CODE}" = "409" ]; then
    echo "    App ${APP} exists; reading its credentials."
    apigee_api GET "${APP_URL}/${APP}"
    apigee_ok || apigee_fail_call_redacted "Reading app ${APP}"
    API_KEY="$(app_key)" || API_KEY=""
    if [ -z "${API_KEY}" ]; then
        FIRST_KEY="$(apigee_json 'd["credentials"][0]["consumerKey"] if d.get("name") == "'"${APP}"'" and d.get("credentials") else None')" \
            || apigee_fail_call_redacted "App ${APP} has no credential to attach ${PRODUCT} to"
        apigee_safe_token "${FIRST_KEY}" || apigee_die "unexpected consumer key format on ${APP}."
        echo "    Adding ${PRODUCT} to the existing key of ${APP}."
        apigee_api POST "${APP_URL}/${APP}/keys/${FIRST_KEY}" -H "Content-Type: application/json" \
            -d "{\"apiProducts\": [\"${PRODUCT}\"]}"
        apigee_ok || apigee_fail_call_redacted "Adding ${PRODUCT} to the key of ${APP}"
        apigee_api GET "${APP_URL}/${APP}"
        apigee_ok || apigee_fail_call_redacted "Reading app ${APP}"
        API_KEY="$(app_key)" || API_KEY=""
    fi
elif apigee_ok; then
    echo "    App ${APP} created."
    API_KEY="$(app_key)" || API_KEY=""
else
    apigee_fail_call_redacted "Creating app ${APP}"
fi
[ -n "${API_KEY}" ] || apigee_fail_call_redacted "Finding an approved ${PRODUCT} consumer key on app ${APP}"

(umask 077 && printf '%s\n' "${API_KEY}" > "${KEY_FILE}.tmp" && mv "${KEY_FILE}.tmp" "${KEY_FILE}")
chmod 600 "${KEY_FILE}"
printf 'APIGEE_HOST=%s\nAPIGEE_ORG=%s\nAPIGEE_ENV=%s\n' "${APIGEE_HOST}" "${APIGEE_ORG}" "${APIGEE_ENV}" > "${ENV_FILE}.tmp"
mv "${ENV_FILE}.tmp" "${ENV_FILE}"

echo ""
echo "=== Apigee proxies deployed for prefix '${P}' ==="
echo "Host:               ${APIGEE_HOST}"
echo "Orders base path:   /${P}/orders/v1"
echo "Inventory base path: /${P}/inventory/v1"
echo "API key written to: ${KEY_FILE}"
echo "Env file:           ${ENV_FILE}"
