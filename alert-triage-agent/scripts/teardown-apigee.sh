#!/usr/bin/env bash
# Remove the demo Apigee resources of one NAME_PREFIX stack (the inverse of deploy-apigee-proxies.sh).
#
#   scripts/teardown-apigee.sh                  dry run (default): read-only GETs, prints the plan
#   scripts/teardown-apigee.sh --apply          asks you to type NAME_PREFIX, then deletes
#   scripts/teardown-apigee.sh --apply --yes    deletes without the prompt
#
# Required: NAME_PREFIX ([a-z][a-z0-9]{1,9}), APIGEE_ORG, APIGEE_ENV. No defaults.
# Optional: UNDEPLOY_TIMEOUT_SECS (default 300), DEPLOY_POLL_SECS (default 5).
#
# Deletes, in order, only these names (anything not starting with "${NAME_PREFIX}-" is refused):
#   proxies ${NAME_PREFIX}-orders-v1, ${NAME_PREFIX}-inventory-v1: every deployed revision in
#     APIGEE_ENV is undeployed, then the proxy (all revisions) is deleted
#   app ${NAME_PREFIX}-demo-app, developer ${NAME_PREFIX}-dev@example.com, product ${NAME_PREFIX}-product
#   TargetServers ${NAME_PREFIX}-orders-target, ${NAME_PREFIX}-inventory-target (in APIGEE_ENV)
# 404 means "already gone". Any other non-2xx fails with the response body.
# Ends with a verification: list GETs must show nothing named "${NAME_PREFIX}-*" (exit 1 otherwise).
# The service account and the local key/env files are left alone.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=../demo/apigee/apigee-lib.sh
. "${SCRIPT_DIR}/../demo/apigee/apigee-lib.sh"

APPLY=false
YES=false
for arg in "$@"; do
    case "${arg}" in
        --apply) APPLY=true ;;
        --yes) YES=true ;;
        --dry-run) APPLY=false ;;
        -h|--help) sed -n '2,19p' "$0"; exit 0 ;;
        *) apigee_die "unknown argument '${arg}' (use --apply, --yes, --dry-run)." ;;
    esac
done

for v in NAME_PREFIX APIGEE_ORG APIGEE_ENV; do
    apigee_require_var "${v}" "see the header of scripts/teardown-apigee.sh"
done
apigee_validate_prefix "${NAME_PREFIX}" || apigee_die "NAME_PREFIX '${NAME_PREFIX}' must match [a-z][a-z0-9]{1,9}."
apigee_safe_token "${APIGEE_ORG}" || apigee_die "APIGEE_ORG='${APIGEE_ORG}' contains unexpected characters."
apigee_safe_token "${APIGEE_ENV}" || apigee_die "APIGEE_ENV='${APIGEE_ENV}' contains unexpected characters."
UNDEPLOY_TIMEOUT_SECS="${UNDEPLOY_TIMEOUT_SECS:-300}"
DEPLOY_POLL_SECS="${DEPLOY_POLL_SECS:-5}"

P="${NAME_PREFIX}"
PROXIES="${P}-orders-v1 ${P}-inventory-v1"
TARGETSERVERS="${P}-orders-target ${P}-inventory-target"
PRODUCT="${P}-product"
DEVELOPER="${P}-dev@example.com"
APP="${P}-demo-app"
BASE_URL="https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"
ENV_URL="${BASE_URL}/environments/${APIGEE_ENV}"

for name in ${PROXIES} ${TARGETSERVERS} "${PRODUCT}" "${DEVELOPER}" "${APP}"; do
    apigee_assert_owned "${name}"
done

if [ "${APPLY}" = "true" ]; then MODE="APPLY"; else MODE="DRY RUN"; fi
echo "=== Apigee teardown for prefix '${P}' (${MODE}) ==="
echo "    org=${APIGEE_ORG} env=${APIGEE_ENV}"
echo "Targets (only these names are ever deleted):"
for proxy in ${PROXIES}; do echo "  proxy         ${proxy} (undeploy from ${APIGEE_ENV}, delete all revisions)"; done
echo "  app           ${DEVELOPER}/${APP}"
echo "  developer     ${DEVELOPER}"
echo "  apiproduct    ${PRODUCT}"
for ts in ${TARGETSERVERS}; do echo "  targetserver  ${APIGEE_ENV}/${ts}"; done
echo ""

if [ "${APPLY}" = "true" ] && [ "${YES}" != "true" ]; then
    printf 'Type the prefix "%s" to delete these resources: ' "${P}"
    reply=""
    read -r reply || true
    [ "${reply}" = "${P}" ] || apigee_die "confirmation did not match '${P}'; nothing deleted."
fi

if [ -z "${APIGEE_TOKEN:-}" ]; then
    TOKEN_SOURCE="${ATA_TOKEN_SOURCE:-${ATA_MONITORING_TOKEN_SOURCE:-adc}}"
    err_file="$(mktemp "${TMPDIR:-/tmp}/${P}-apigee-token.XXXXXX" 2>/dev/null || mktemp "/tmp/${P}-apigee-token.XXXXXX")"
    case "${TOKEN_SOURCE}" in
        adc)
            if ! APIGEE_TOKEN="$(gcloud auth application-default print-access-token 2>"${err_file}")"; then
                err="$(cat "${err_file}" 2>/dev/null || true)"
                rm -f "${err_file}"
                if [ -n "${err}" ]; then
                    apigee_die "could not get an access token from token source 'adc' (run gcloud auth application-default login). ${err}"
                else
                    apigee_die "could not get an access token from token source 'adc' (run gcloud auth application-default login)."
                fi
            fi
            ;;
        gcloud)
            if ! APIGEE_TOKEN="$(gcloud auth print-access-token 2>"${err_file}")"; then
                err="$(cat "${err_file}" 2>/dev/null || true)"
                rm -f "${err_file}"
                if [ -n "${err}" ]; then
                    apigee_die "could not get an access token from token source 'gcloud' (run gcloud auth login). ${err}"
                else
                    apigee_die "could not get an access token from token source 'gcloud' (run gcloud auth login)."
                fi
            fi
            ;;
        *)
            rm -f "${err_file}"
            apigee_die "Invalid ATA_TOKEN_SOURCE '${TOKEN_SOURCE}' (expected 'gcloud' or 'adc')."
            ;;
    esac
    rm -f "${err_file}"
    [ -n "${APIGEE_TOKEN}" ] || apigee_die "token source '${TOKEN_SOURCE}' returned an empty token."
fi

# remove DESC URL: DELETE (or say what would be deleted); 404 = already gone.
remove() {
    local desc="$1" url="$2"
    if [ "${APPLY}" != "true" ]; then
        apigee_api GET "${url}"
        if [ "${API_CODE}" = "404" ]; then
            echo "    ${desc}: already gone"
        elif apigee_ok; then
            echo "    ${desc}: exists, would DELETE ${url}"
        else
            apigee_fail_call "Reading ${desc}"
        fi
        return 0
    fi
    apigee_api DELETE "${url}"
    if [ "${API_CODE}" = "404" ]; then
        echo "    ${desc}: already gone"
    elif apigee_ok; then
        echo "    ${desc}: deleted"
    else
        apigee_fail_call "Deleting ${desc}"
    fi
}

deployed_revisions() {
    local proxy="$1"
    apigee_api GET "${ENV_URL}/apis/${proxy}/deployments"
    if [ "${API_CODE}" = "404" ]; then
        return 0
    fi
    apigee_ok || apigee_fail_call "Listing deployments of ${proxy} in ${APIGEE_ENV}"
    apigee_json '[x.get("revision") for x in d.get("deployments", [])]' || true
}

teardown_proxy() {
    local proxy="$1" revs rev waited=0
    echo "--> Proxy ${proxy}"
    apigee_api GET "${BASE_URL}/apis/${proxy}"
    if [ "${API_CODE}" = "404" ]; then
        echo "    ${proxy}: already gone"
        return 0
    fi
    apigee_ok || apigee_fail_call "Reading proxy ${proxy}"
    revs="$(deployed_revisions "${proxy}")"
    for rev in ${revs}; do
        case "${rev}" in
            *[!0-9]*) apigee_die "unexpected revision '${rev}' for ${proxy}." ;;
        esac
        remove "${proxy} revision ${rev} deployment in ${APIGEE_ENV}" \
            "${ENV_URL}/apis/${proxy}/revisions/${rev}/deployments"
    done
    if [ "${APPLY}" = "true" ] && [ -n "${revs}" ]; then
        while [ -n "$(deployed_revisions "${proxy}")" ]; do
            [ "${waited}" -lt "${UNDEPLOY_TIMEOUT_SECS}" ] \
                || apigee_die "${proxy} still has deployments in ${APIGEE_ENV} after ${UNDEPLOY_TIMEOUT_SECS}s."
            sleep "${DEPLOY_POLL_SECS}"
            waited=$((waited + DEPLOY_POLL_SECS))
            [ "${DEPLOY_POLL_SECS}" -gt 0 ] || waited=$((waited + 1))
        done
    fi
    remove "${proxy} (all revisions)" "${BASE_URL}/apis/${proxy}"
}

for proxy in ${PROXIES}; do
    teardown_proxy "${proxy}"
done
echo "--> App, developer, product"
remove "app ${APP}" "${BASE_URL}/developers/${DEVELOPER}/apps/${APP}"
remove "developer ${DEVELOPER}" "${BASE_URL}/developers/${DEVELOPER}"
remove "API product ${PRODUCT}" "${BASE_URL}/apiproducts/${PRODUCT}"
echo "--> TargetServers"
for ts in ${TARGETSERVERS}; do
    remove "TargetServer ${ts}" "${ENV_URL}/targetservers/${ts}"
done

echo ""
echo "--> Verification: anything named '${P}-*' left in organizations/${APIGEE_ORG}"
LEFT=""
check_list() {
    local desc="$1" url="$2" names name hits=""
    apigee_api GET "${url}"
    apigee_ok || apigee_fail_call "Listing ${desc}"
    names="$(apigee_list_names)" || apigee_fail_call "Parsing the ${desc} list"
    for name in ${names}; do
        case "${name}" in
            "${P}-"*) hits="${hits} ${name}" ;;
        esac
    done
    if [ -n "${hits}" ]; then
        echo "    ${desc}:${hits}"
        LEFT="${LEFT}${hits}"
    else
        echo "    ${desc}: none"
    fi
}
check_list "proxies" "${BASE_URL}/apis"
check_list "API products" "${BASE_URL}/apiproducts"
check_list "developers" "${BASE_URL}/developers"
check_list "TargetServers in ${APIGEE_ENV}" "${ENV_URL}/targetservers"

if [ "${APPLY}" != "true" ]; then
    echo ""
    echo "DRY RUN: nothing was deleted. Re-run with --apply to delete the resources listed above."
    exit 0
fi
if [ -n "${LEFT}" ]; then
    apigee_die "teardown incomplete; still present:${LEFT}"
fi
echo ""
echo "=== Teardown complete: nothing named '${P}-*' remains ==="
