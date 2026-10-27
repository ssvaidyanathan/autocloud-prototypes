#!/usr/bin/env bash
# Render a prefix-neutral Apigee proxy bundle template for one NAME_PREFIX.
#
#   demo/apigee/render-bundle.sh <template> <prefix> <outdir> [<zip-file>]
#
#   <template>  a directory next to this script: orders-v1 | inventory-v1
#   <prefix>    NAME_PREFIX, must match [a-z][a-z0-9]{1,9}
#   <outdir>    created (must not exist or be empty); receives apiproxy/...
#   <zip-file>  optional: also zip <outdir> into this file (the Apigee import format)
#
# Placeholders: @PREFIX@ -> <prefix>, @PREFIX_UPPER@ -> <PREFIX>. They are replaced in file
# contents and in file names (apiproxy/@PREFIX@-orders-v1.xml -> apiproxy/<prefix>-orders-v1.xml).
# Fails if the prefix is invalid or if any placeholder survives. Prints the root XML path.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source-path=SCRIPTDIR source=apigee-lib.sh
. "${HERE}/apigee-lib.sh"

[ "$#" -eq 3 ] || [ "$#" -eq 4 ] || apigee_die "usage: $0 <template> <prefix> <outdir> [<zip-file>]"
TEMPLATE="$1"
PREFIX="$2"
OUT="$3"
ZIP="${4:-}"

apigee_validate_prefix "${PREFIX}" || apigee_die "invalid prefix '${PREFIX}': must match [a-z][a-z0-9]{1,9}."
case "${TEMPLATE}" in
    ''|*/*|.*) apigee_die "invalid template name '${TEMPLATE}'." ;;
esac
SRC="${HERE}/${TEMPLATE}"
[ -d "${SRC}/apiproxy" ] || apigee_die "template ${SRC}/apiproxy not found."
if [ -e "${OUT}" ] && [ -n "$(ls -A "${OUT}")" ]; then
    apigee_die "output directory ${OUT} is not empty."
fi
mkdir -p "${OUT}"

UPPER="$(printf '%s' "${PREFIX}" | tr 'abcdefghijklmnopqrstuvwxyz' 'ABCDEFGHIJKLMNOPQRSTUVWXYZ')"

(cd "${SRC}" && find apiproxy -type f) | while IFS= read -r rel; do
    dst="$(printf '%s' "${rel}" | sed -e "s/@PREFIX_UPPER@/${UPPER}/g" -e "s/@PREFIX@/${PREFIX}/g")"
    mkdir -p "${OUT}/$(dirname "${dst}")"
    sed -e "s/@PREFIX_UPPER@/${UPPER}/g" -e "s/@PREFIX@/${PREFIX}/g" "${SRC}/${rel}" > "${OUT}/${dst}"
done

LEFT="$(grep -rl '@PREFIX' "${OUT}" || true)"
[ -z "${LEFT}" ] || apigee_die "placeholder left after substitution in: ${LEFT}"
LEFT="$(find "${OUT}" -name '*@PREFIX*' -print)"
[ -z "${LEFT}" ] || apigee_die "placeholder left in file name: ${LEFT}"
ROOT_XML="${OUT}/apiproxy/${PREFIX}-${TEMPLATE}.xml"
[ -f "${ROOT_XML}" ] || apigee_die "rendered bundle has no root XML ${ROOT_XML}."

if [ -n "${ZIP}" ]; then
    rm -f "${ZIP}"
    python3 - "${OUT}" "${ZIP}" <<'PY'
import os, sys, zipfile
src, dst = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(src):
        dirs.sort()
        for f in sorted(files):
            full = os.path.join(root, f)
            zf.write(full, os.path.relpath(full, src))
PY
    [ -s "${ZIP}" ] || apigee_die "could not build bundle zip ${ZIP}."
fi
printf '%s\n' "${ROOT_XML}"
