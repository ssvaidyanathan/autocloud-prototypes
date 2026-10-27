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
#   apigee_download URL FILE      -> GET URL into FILE (binary-safe); sets API_CODE, API_LAST,
#                                    and API_BODY (the error body, only when not 2xx)
#   apigee_json_matches JSON      -> 0 if API_BODY equals JSON exactly once the server-set
#                                    fields are dropped (lists order-insensitive); any extra
#                                    or different field -> 1
#   apigee_bundles_equal A B      -> 0 same proxy content, 1 different (reason on stdout),
#                                    2 unreadable bundle (reason on stdout)
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

apigee_download() {
    local url="$1" dest="$2"
    apigee_guard_url "${url}"
    rm -f "${dest}"
    API_CODE="$(curl -sS -o "${dest}" -w '%{http_code}' -X GET \
        --config <(printf 'header = "Authorization: Bearer %s"\n' "${APIGEE_TOKEN}") \
        "${url}")" || API_CODE="000"
    case "${API_CODE}" in
        [0-9][0-9][0-9]) ;;
        *) API_CODE="000" ;;
    esac
    API_BODY=""
    if ! apigee_ok && [ -f "${dest}" ]; then
        API_BODY="$(head -c 2000 "${dest}" | tr -d '\000')"
    fi
    API_LAST="GET ${url}"
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

# Server-set fields of TargetServer, API product and developer GET responses (observed
# read-only on the live org, 2026-10-27). They are never part of what we send, so they are
# dropped from API_BODY before the exact compare. Keep this list explicit: any other key in
# API_BODY that JSON does not have makes the resource differ, so it gets the full-replace PUT.
#   createdAt, lastModifiedAt                     product, developer
#   developerId, organizationName, status, apps   developer
# Server defaults: dropped only when they hold exactly the default value (TargetServer
# echoes "protocol": "HTTP" when none was sent; any other protocol is drift).
apigee_json_matches() {
    printf '%s' "${API_BODY}" | python3 -c '
import json, sys
SERVER_SET = {"createdAt", "lastModifiedAt", "developerId", "organizationName", "status", "apps"}
SERVER_DEFAULTS = {"protocol": "HTTP"}
def canon(v):
    return json.dumps(v, sort_keys=True)
def same(want, have):
    if isinstance(want, dict):
        return (isinstance(have, dict) and sorted(want) == sorted(have)
                and all(same(v, have[k]) for k, v in want.items()))
    if isinstance(want, list):
        return isinstance(have, list) and sorted(map(canon, want)) == sorted(map(canon, have))
    return type(want) is type(have) and want == have
try:
    have = json.load(sys.stdin)
    want = json.loads(sys.argv[1])
except ValueError:
    sys.exit(3)
if not isinstance(want, dict) or not isinstance(have, dict):
    sys.exit(1)
have = {k: v for k, v in have.items()
        if k in want or (k not in SERVER_SET
                         and not (k in SERVER_DEFAULTS and v == SERVER_DEFAULTS[k]))}
sys.exit(0 if same(want, have) else 1)
' "$1"
}

# Proxy bundles are compared by content, not by zip bytes: every file is compared
# byte-for-byte except the root descriptors apiproxy/*.xml, which Apigee regenerates on export
# (revision attribute, CreatedAt/LastModifiedAt, BasePaths, no XML declaration, Policies
# reordered). Those are compared on what an import takes from them: every element except the
# generated metadata, with list elements (Policies, ProxyEndpoints, ...) order-insensitive.
apigee_bundles_equal() {
    python3 - "$1" "$2" <<'PY'
import sys, zipfile
import xml.etree.ElementTree as ET

GENERATED = {"CreatedAt", "CreatedBy", "LastModifiedAt", "LastModifiedBy", "ManifestVersion",
             "ConfigurationVersion", "BasePaths"}

def files(path):
    out = {}
    with zipfile.ZipFile(path) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename
            while name.startswith("./"):
                name = name[2:]
            out[name] = zf.read(info)
    return out

def is_descriptor(name):
    parts = name.split("/")
    return len(parts) == 2 and parts[0] == "apiproxy" and parts[1].endswith(".xml")

def descriptor(data):
    root = ET.fromstring(data)
    fields = {}
    for child in root:
        if child.tag in GENERATED:
            continue
        items = sorted((c.tag, (c.text or "").strip()) for c in child)
        value = items if items else (child.text or "").strip()
        if value:
            fields[child.tag] = value
    return (root.tag, root.get("name"), sorted(fields.items()))

try:
    a, b = files(sys.argv[1]), files(sys.argv[2])
except (OSError, zipfile.BadZipFile) as e:
    print("unreadable bundle: %s" % e)
    sys.exit(2)
if not any(is_descriptor(n) for n in a) or not any(is_descriptor(n) for n in b):
    print("a bundle has no apiproxy/<name>.xml")
    sys.exit(2)
if sorted(a) != sorted(b):
    only = sorted(set(a) ^ set(b))
    print("file lists differ: %s" % ", ".join(only))
    sys.exit(1)
for name in sorted(a):
    if is_descriptor(name):
        try:
            same = descriptor(a[name]) == descriptor(b[name])
        except ET.ParseError as e:
            print("unparsable %s: %s" % (name, e))
            sys.exit(2)
    else:
        same = a[name] == b[name]
    if not same:
        print("%s differs" % name)
        sys.exit(1)
sys.exit(0)
PY
}
