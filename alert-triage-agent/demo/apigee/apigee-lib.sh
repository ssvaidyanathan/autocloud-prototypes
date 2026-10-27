# shellcheck shell=bash
# Shared helpers for scripts/deploy-apigee-proxies.sh and scripts/teardown-apigee.sh.
# Sourced, not executed. bash 3.2 + BSD userland safe (no assoc arrays, mapfile, ${v,,}, \s).
#
# Contract:
#   apigee_validate_prefix P      -> 0 if P matches [a-z][a-z0-9]{1,9}
#   apigee_require_var NAME       -> dies if $NAME is empty
#   apigee_assert_owned NAME      -> dies unless NAME starts with "${NAME_PREFIX}-"
#   apigee_guard_url URL          -> dies unless every resource named in URL is owned
#   apigee_api METHOD URL [curl args...]
#                                 -> sets API_CODE (HTTP status, 000 on transport error)
#                                    and API_BODY; never exits on its own
#   apigee_ok                     -> 0 if API_CODE is 2xx
#   apigee_fail_call DESC         -> prints status + body, dies
#   apigee_json EXPR              -> evaluates a Python expression on `d` = json(API_BODY)
#
# Required globals: NAME_PREFIX, APIGEE_TOKEN, and for apigee_guard_url APIGEE_ORG.

apigee_die() {
    printf 'ERROR: %s\n' "$*" >&2
    exit 1
}

apigee_validate_prefix() {
    local p="$1"
    # Explicit character lists: [a-z] in a glob is locale-dependent.
    case "${p}" in
        ''|*[!abcdefghijklmnopqrstuvwxyz0123456789]*) return 1 ;;
        [abcdefghijklmnopqrstuvwxyz]*) ;;
        *) return 1 ;;
    esac
    [ "${#p}" -ge 2 ] && [ "${#p}" -le 10 ]
}

apigee_require_var() {
    local name="$1" value
    eval "value=\"\${${name}:-}\""
    [ -n "${value}" ] || apigee_die "${name} is required and has no default (${2:-see the script header})."
}

# Names Apigee uses in URLs and JSON: letters, digits, . _ - @ only.
apigee_safe_token() {
    case "$1" in
        ''|*[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._@-]*) return 1 ;;
    esac
    return 0
}

apigee_assert_owned() {
    local name="$1"
    case "${name}" in
        "${NAME_PREFIX}-"?*) ;;
        *) apigee_die "SAFETY GUARD: refusing to touch '${name}': it does not start with '${NAME_PREFIX}-'." ;;
    esac
    apigee_safe_token "${name}" || apigee_die "SAFETY GUARD: refusing unexpected resource name '${name}'."
}

# Every path segment that follows a resource collection (apis, targetservers, apiproducts,
# developers, apps, keys excluded) must be an owned name, and the URL must be for APIGEE_ORG.
apigee_guard_url() {
    local url="$1" path seg want="" i=0
    local -a segs
    path="${url%%\?*}"
    case "${path}" in
        "https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"|"https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}/"*) ;;
        *) apigee_die "SAFETY GUARD: refusing URL outside organizations/${APIGEE_ORG}: ${url}" ;;
    esac
    path="${path#https://apigee.googleapis.com/v1/organizations/"${APIGEE_ORG}"}"
    path="${path#/}"
    [ -n "${path}" ] || return 0
    IFS=/ read -r -a segs <<< "${path}"
    while [ "${i}" -lt "${#segs[@]}" ]; do
        seg="${segs[${i}]}"
        if [ -n "${want}" ]; then
            apigee_assert_owned "${seg}"
            want=""
        else
            case "${seg}" in
                apis|targetservers|apiproducts|developers|apps) want="${seg}" ;;
            esac
        fi
        i=$((i + 1))
    done
    # ?name= on import names the proxy being created.
    case "${url}" in
        *\?name=*|*\&name=*)
            seg="${url#*name=}"
            seg="${seg%%&*}"
            apigee_assert_owned "${seg}" ;;
    esac
    return 0
}

apigee_api() {
    local method="$1" url="$2" out
    shift 2
    apigee_guard_url "${url}"
    out="$(mktemp "${TMPDIR:-/tmp}/apigee-resp.XXXXXX")"
    # The bearer token goes in a curl config read from a process substitution (builtin printf,
    # bash 3.2 safe), so it is never on curl's argv where `ps` would show it.
    API_CODE="$(curl -sS -o "${out}" -w '%{http_code}' -X "${method}" \
        --config <(printf 'header = "Authorization: Bearer %s"\n' "${APIGEE_TOKEN}") \
        "$@" "${url}")" || API_CODE="000"
    API_BODY="$(cat "${out}")"
    rm -f "${out}"
    case "${API_CODE}" in
        [0-9][0-9][0-9]) ;;
        *) API_CODE="000" ;;
    esac
    API_LAST="${method} ${url}"
    return 0
}

apigee_ok() {
    case "${API_CODE}" in
        2[0-9][0-9]) return 0 ;;
    esac
    return 1
}

apigee_fail_call() {
    printf 'ERROR: %s failed: HTTP %s on %s\n' "$1" "${API_CODE}" "${API_LAST}" >&2
    printf '%s\n' "--- response body ---" "${API_BODY:-<empty>}" "---------------------" >&2
    exit 1
}

# For calls whose response or URL carries app credentials (GET .../apps/<app> returns every
# consumerKey and consumerSecret; POST .../keys/<key> has the key in the URL): no body, and the
# key segment of the URL withheld.
apigee_fail_call_redacted() {
    local where="${API_LAST}"
    case "${where}" in
        */keys/*) where="${where%%/keys/*}/keys/<withheld>" ;;
    esac
    printf 'ERROR: %s failed: HTTP %s on %s (response body withheld: it can contain app credentials)\n' \
        "$1" "${API_CODE}" "${where}" >&2
    exit 1
}

apigee_json() {
    printf '%s' "${API_BODY}" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
except ValueError:
    sys.exit(3)
r = eval(sys.argv[1], {"d": d})
if r is None:
    sys.exit(4)
if isinstance(r, (list, tuple)):
    for x in r:
        print(x)
else:
    print(r)
' "$1"
}

# Names in an Apigee list response: {"proxies":[{"name":..}]}, {"apiProduct":[..]},
# {"developer":[{"email":..}]}, or a bare JSON array of strings.
apigee_list_names() {
    apigee_json '[ (x.get("name") or x.get("email") or "") if isinstance(x, dict) else x for x in (d if isinstance(d, list) else next((v for v in d.values() if isinstance(v, list)), [])) ]'
}
