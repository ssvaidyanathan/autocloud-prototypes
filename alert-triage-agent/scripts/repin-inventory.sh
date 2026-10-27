#!/usr/bin/env bash
# ==============================================================================
# Re-pin ata-inventory-api: move the demo baseline to a new revision, safely.
#
# The pin (revision + image digest) lives in scripts/scenarios/inventory-pin.env. Every
# scenario, reset and preflight restores and checks traffic against it. Re-pinning is:
#
#   1. prepare    Build the image (or take --image-digest), deploy it as a NEW revision with
#                 NO traffic and a temporary tag, verify it (config + HTTP through the tag
#                 URL), remove the tag. Traffic stays 100% on the current pin.
#   2. write-pin  Check the candidate again (read-only) and rewrite inventory-pin.env; the
#                 current pin becomes PIN_INVENTORY_PREVIOUS_* (the rollback target).
#                 Commit the file.
#   3. switch     Move 100% of traffic to the pinned revision, then verify serving, health,
#                 the database path and the admin probes.
#   rollback      Traffic back to PIN_INVENTORY_PREVIOUS_REVISION and the pin file swapped
#                 back. Commit the file.
#   status        Read-only report: pin, traffic, pinned revision vs baseline, instance caps.
#
# Baseline of a pinned revision: image digest of the pin, ATA_INVENTORY_* env only (no
# DB_USER/MOCK_DB aliases), max instances DEMO_MAX_INSTANCES (ATA_DEMO_MAX_INSTANCES,
# default 1), min instances 0, Ready. See DEPLOY.md, "Re-pinning ata-inventory-api".
#
# Fails loudly at every step; never moves traffic unless asked (switch / rollback), and
# never rolls back on its own. bash 3.2 and BSD userland compatible.
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/../demo/ata-inventory-api" && pwd)"
# shellcheck source=scripts/scenarios/common.sh
source "${SCRIPT_DIR}/scenarios/common.sh"

SERVICE="${NAME_PREFIX}-inventory-api"
BASELINE_PY="${SCRIPT_DIR}/scenarios/inventory_baseline.py"
# Temporary traffic tag of the candidate (removed before prepare exits).
CANDIDATE_TAG="${ATA_REPIN_TAG:-repin-candidate}"
# Demo item the database check expects in GET /items.
EXPECTED_ITEM="${ATA_REPIN_EXPECTED_ITEM:-item-101}"

usage() {
    cat <<EOF
Usage: $0 <command> [options]

Commands:
  status                              Read-only report (exit 1 if anything is off baseline)
  prepare --image-tag <tag>           Build demo/ata-inventory-api with Cloud Build as
                                      ${PINNED_INVENTORY_IMAGE_BASE}:<tag>,
                                      deploy it with NO traffic, verify, untag
  prepare --image-digest sha256:<hex> Same, with an image already in Artifact Registry
  write-pin --revision <rev> --digest sha256:<hex>
                                      Verify <rev> (read-only) and rewrite the pin file
  switch                              Route 100% of traffic to the pinned revision and verify
  rollback                            Route 100% to PIN_INVENTORY_PREVIOUS_REVISION, swap the pin back

Pin file: ${INVENTORY_PIN_FILE}
Env: ATA_DEMO_MAX_INSTANCES (default 1), ATA_REPIN_TAG (default repin-candidate),
     ATA_REPIN_EXPECTED_ITEM (default item-101), PROJECT_ID, REGION, NAME_PREFIX.
EOF
}

die() {
    log_error "FATAL: $*" >&2
    exit 1
}

# The pin file is the source of truth here: refuse one-run env overrides of the pin.
require_no_pin_override() {
    if [ "${PINNED_INVENTORY_REVISION}" != "${PIN_INVENTORY_REVISION}" ] \
        || [ "${PINNED_INVENTORY_DIGEST}" != "${PIN_INVENTORY_DIGEST}" ]; then
        die "PINNED_INVENTORY_REVISION/PINNED_INVENTORY_DIGEST are overridden in the environment (${PINNED_INVENTORY_REVISION} / ${PINNED_INVENTORY_DIGEST}); unset them: $0 works on ${INVENTORY_PIN_FILE}."
    fi
}

case "${CANDIDATE_TAG}" in
    ""|*[!a-z0-9-]*|-*) die "ATA_REPIN_TAG '${CANDIDATE_TAG}' must be lowercase letters, digits and '-'." ;;
esac

service_json() {
    gcloud run services describe "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" --format=json
}

# Check the serving split of the service JSON on stdin is exactly $1=100 (tags ignored).
expect_serving() {
    python3 "${BASELINE_PY}" traffic --expect="$1"
}

# http_get <base> <path> <audience>: GET with an ID token; sets HTTP_STATUS and HTTP_BODY.
HTTP_STATUS="" HTTP_BODY=""
http_get() {
    local base="$1" path="$2" aud="$3" token out body_file
    local auth=()
    token="$(resolve_id_token "${aud}")"
    if [ -n "${token}" ]; then
        auth=(-H "Authorization: Bearer ${token}")
    fi
    body_file="$(mktemp "${TMPDIR:-/tmp}/ata-repin.XXXXXX")"
    out="$(curl -s -o "${body_file}" -w "%{http_code}" --connect-timeout 5 --max-time 60 \
        ${auth[@]+"${auth[@]}"} "${base}${path}" 2>/dev/null)" || out="000"
    HTTP_STATUS="${out}"
    HTTP_BODY="$(head -c 600 "${body_file}" 2>/dev/null || true)"
    rm -f "${body_file}"
}

# verify_http <base> <audience>: health, the database path (GET /items lists EXPECTED_ITEM:
# proves the Cloud SQL IAM login with ATA_INVENTORY_DB_USER), and the GET admin probes,
# which must answer {"status": "ok"} (harmless-probe code, and /admin/restock is present).
# Sends no POST. Returns the number of failed checks.
verify_http() {
    local base="$1" aud="$2" failed=0 path
    http_get "${base}" /health "${aud}"
    if [ "${HTTP_STATUS}" = "200" ]; then
        log_success "    GET /health -> 200"
    else
        log_error "    GET ${base}/health -> HTTP ${HTTP_STATUS}: ${HTTP_BODY}" >&2
        failed=$((failed + 1))
    fi
    http_get "${base}" /items "${aud}"
    if [ "${HTTP_STATUS}" = "200" ] && printf '%s' "${HTTP_BODY}" | grep -q "\"${EXPECTED_ITEM}\""; then
        log_success "    GET /items -> 200, lists ${EXPECTED_ITEM} (Cloud SQL IAM login works)"
    else
        log_error "    GET ${base}/items -> HTTP ${HTTP_STATUS} without ${EXPECTED_ITEM}: ${HTTP_BODY}" >&2
        failed=$((failed + 1))
    fi
    for path in /admin/leak-connections /admin/fault/inject /admin/restock; do
        http_get "${base}" "${path}" "${aud}"
        if [ "${HTTP_STATUS}" = "200" ] && printf '%s' "${HTTP_BODY}" | grep -q '"status": *"ok"'; then
            log_success "    GET ${path} -> 200 probe"
        else
            log_error "    GET ${base}${path} -> HTTP ${HTTP_STATUS} (want a 200 probe): ${HTTP_BODY}" >&2
            failed=$((failed + 1))
        fi
    done
    return "${failed}"
}

# Rewrite the pin file atomically (temp file in the same directory, then mv).
write_pin_file() {
    local rev="$1" digest="$2" prev_rev="$3" prev_digest="$4" tmp header
    header="$(sed -n '/^#/p' "${INVENTORY_PIN_FILE}")"
    tmp="$(mktemp "$(dirname "${INVENTORY_PIN_FILE}")/.inventory-pin.XXXXXX")"
    {
        printf '%s\n' "${header}"
        printf 'PIN_INVENTORY_REVISION=%s\n' "${rev}"
        printf 'PIN_INVENTORY_DIGEST=%s\n' "${digest}"
        printf 'PIN_INVENTORY_PREVIOUS_REVISION=%s\n' "${prev_rev}"
        printf 'PIN_INVENTORY_PREVIOUS_DIGEST=%s\n' "${prev_digest}"
    } > "${tmp}"
    # Validate what was written before it replaces the pin.
    if ! read_inventory_pin_file "${tmp}"; then
        rm -f "${tmp}"
        die "the new pin file did not validate; ${INVENTORY_PIN_FILE} is unchanged."
    fi
    mv "${tmp}" "${INVENTORY_PIN_FILE}"
    read_inventory_pin_file "${INVENTORY_PIN_FILE}"
}

# --- status --------------------------------------------------------------------------

cmd_status() {
    local off=0 svc_json svc
    log_info "Pin file ${INVENTORY_PIN_FILE}:"
    log_info "    pinned   ${PIN_INVENTORY_REVISION} (${PIN_INVENTORY_DIGEST})"
    log_info "    previous ${PIN_INVENTORY_PREVIOUS_REVISION:-<none>} ${PIN_INVENTORY_PREVIOUS_DIGEST:+(${PIN_INVENTORY_PREVIOUS_DIGEST})}"
    svc_json="$(service_json)" || die "cannot describe ${SERVICE}."
    log_info "Serving (percent > 0):"
    python3 "${BASELINE_PY}" serving <<<"${svc_json}" | sed 's/^/    /'
    if expect_serving "${PIN_INVENTORY_REVISION}" <<<"${svc_json}"; then
        log_success "Traffic: 100% on the pinned revision."
    else
        off=$((off + 1))
    fi
    if inventory_revision_matches_baseline "${PIN_INVENTORY_REVISION}" "${PIN_INVENTORY_DIGEST}"; then
        log_success "Pinned revision matches the baseline (digest, ATA_INVENTORY_* env, no aliases, max ${DEMO_MAX_INSTANCES}, min 0, Ready)."
    else
        log_warn "Pinned revision ${PIN_INVENTORY_REVISION} is not on the current baseline (above). Re-pin: $0 prepare." >&2
        off=$((off + 1))
    fi
    if inventory_template_matches_baseline; then
        log_success "Service template matches the baseline."
    else
        off=$((off + 1))
    fi
    for svc in "${NAME_PREFIX}-inventory-api" "${NAME_PREFIX}-orders-api"; do
        if service_max_matches_baseline "${svc}"; then
            log_success "${svc}: service-level max instances ${DEMO_MAX_INSTANCES}."
        else
            log_warn "${svc}: fix with scripts/set-demo-instance-caps.sh --apply" >&2
            off=$((off + 1))
        fi
    done
    if [ "${off}" -gt 0 ]; then
        log_warn "${off} item(s) off baseline." >&2
        return 1
    fi
    log_success "ata-inventory-api is on the pinned baseline."
}

# --- prepare -------------------------------------------------------------------------

CANDIDATE_TAGGED=false
remove_candidate_tag() {
    local rc=$? svc_json
    if [ "${CANDIDATE_TAGGED}" = "true" ]; then
        # The update may have failed before the tag was set: nothing to remove then.
        if svc_json="$(service_json 2>/dev/null)" \
            && ! python3 "${BASELINE_PY}" tag-url --tag="${CANDIDATE_TAG}" <<<"${svc_json}" >/dev/null 2>&1; then
            CANDIDATE_TAGGED=false
            return "${rc}"
        fi
        if gcloud run services update-traffic "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" \
            --remove-tags="${CANDIDATE_TAG}" --quiet >&2; then
            CANDIDATE_TAGGED=false
            log_info "    Removed traffic tag ${CANDIDATE_TAG}." >&2
        else
            log_error "STILL LEFT: traffic tag ${CANDIDATE_TAG} on ${SERVICE}. Remove it: gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --remove-tags=${CANDIDATE_TAG}" >&2
            [ "${rc}" -eq 0 ] && rc=1
        fi
    fi
    return "${rc}"
}

cmd_prepare() {
    local image_tag="" digest="" suffix candidate svc_json tag_url
    while [ $# -gt 0 ]; do
        case "$1" in
            --image-tag) [ $# -ge 2 ] || die "--image-tag needs a value"; image_tag="$2"; shift 2 ;;
            --image-tag=*) image_tag="${1#*=}"; shift ;;
            --image-digest) [ $# -ge 2 ] || die "--image-digest needs a value"; digest="$2"; shift 2 ;;
            --image-digest=*) digest="${1#*=}"; shift ;;
            *) die "prepare: unknown option $1 (see --help)" ;;
        esac
    done
    if [ -n "${image_tag}" ] && [ -n "${digest}" ]; then
        die "prepare: give --image-tag (build) or --image-digest (existing image), not both."
    fi
    if [ -z "${image_tag}" ] && [ -z "${digest}" ]; then
        die "prepare: --image-tag <tag> (build) or --image-digest sha256:<hex> is required."
    fi
    if [ -n "${image_tag}" ]; then
        case "${image_tag}" in
            ""|*[!A-Za-z0-9._-]*|[.-]*) die "--image-tag '${image_tag}' is not a valid Docker tag." ;;
        esac
    fi
    if [ -n "${digest}" ] && ! is_valid_image_digest "${digest}"; then
        die "--image-digest '${digest}' is not sha256:<64 hex>."
    fi
    require_no_pin_override

    log_info "=== Re-pin prepare: ${SERVICE} (current pin ${PIN_INVENTORY_REVISION}) ==="
    log_info "--> 1. Checking traffic is 100% on the current pin (the baseline stays serving)..."
    svc_json="$(service_json)" || die "cannot describe ${SERVICE}."
    expect_serving "${PIN_INVENTORY_REVISION}" <<<"${svc_json}" \
        || die "traffic is not 100% on ${PIN_INVENTORY_REVISION}; run ./scripts/reset-scenarios.sh first."
    if python3 "${BASELINE_PY}" tag-url --tag="${CANDIDATE_TAG}" <<<"${svc_json}" >/dev/null 2>&1; then
        die "traffic tag ${CANDIDATE_TAG} already exists on ${SERVICE} (an earlier prepare?). Remove it: gcloud run services update-traffic ${SERVICE} --project=${PROJECT_ID} --region=${REGION} --remove-tags=${CANDIDATE_TAG}"
    fi

    if [ -n "${image_tag}" ]; then
        log_info "--> 2. Building ${PINNED_INVENTORY_IMAGE_BASE}:${image_tag} from ${DEMO_DIR} (Cloud Build)..."
        digest="$(gcloud builds submit "${DEMO_DIR}" --project="${PROJECT_ID}" \
            --tag="${PINNED_INVENTORY_IMAGE_BASE}:${image_tag}" \
            --format="value(results.images[0].digest)")" || die "Cloud Build failed; nothing was deployed."
        is_valid_image_digest "${digest}" || die "Cloud Build returned no image digest ('${digest}'); nothing was deployed."
        log_success "    Built ${PINNED_INVENTORY_IMAGE_BASE}@${digest}"
    else
        log_info "--> 2. Using existing image ${PINNED_INVENTORY_IMAGE_BASE}@${digest} (no build)."
    fi

    suffix="pin-$(date -u +%Y%m%d%H%M%S)"
    candidate="${SERVICE}-${suffix}"
    log_info "--> 3. Deploying candidate ${candidate} with NO traffic (tag ${CANDIDATE_TAG})..."
    trap remove_candidate_tag EXIT
    # The tag may be set even if the update reports a failure: always try to remove it.
    CANDIDATE_TAGGED=true
    gcloud run services update "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" \
        --image="${PINNED_INVENTORY_IMAGE_BASE}@${digest}" \
        --remove-env-vars="${INVENTORY_LEGACY_ENV_NAMES}" \
        --update-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
        --max-instances="${DEMO_MAX_INSTANCES}" \
        --min-instances=0 \
        --revision-suffix="${suffix}" \
        --tag="${CANDIDATE_TAG}" \
        --no-traffic \
        --quiet || die "deploying ${candidate} failed."

    log_info "--> 4. Verifying ${candidate} configuration..."
    inventory_revision_matches_baseline "${candidate}" "${digest}" \
        || die "${candidate} is not a valid baseline revision (above). It has no traffic; prune it later."
    log_success "    digest, ATA_INVENTORY_* env, no aliases, max ${DEMO_MAX_INSTANCES}, min 0, Ready."
    svc_json="$(service_json)" || die "cannot describe ${SERVICE}."
    expect_serving "${PIN_INVENTORY_REVISION}" <<<"${svc_json}" \
        || die "traffic moved off ${PIN_INVENTORY_REVISION} during the deploy (above)."
    log_success "    Traffic still 100% on ${PIN_INVENTORY_REVISION}; ${candidate} has 0%."

    log_info "--> 5. Verifying ${candidate} over HTTP through its tag URL (GET only)..."
    tag_url="$(python3 "${BASELINE_PY}" tag-url --tag="${CANDIDATE_TAG}" <<<"${svc_json}")" \
        || die "no URL for tag ${CANDIDATE_TAG}."
    # Audience: the service URL (Cloud Run accepts it for the service's tag URLs).
    if ! verify_http "${tag_url}" "$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["url"])' <<<"${svc_json}")"; then
        die "${candidate} failed the HTTP checks (above). It has no traffic; prune it later."
    fi

    log_info "--> 6. Removing the temporary tag..."
    remove_candidate_tag || die "could not remove tag ${CANDIDATE_TAG}."
    trap - EXIT
    verify_inventory_serving_pinned || die "traffic is not 100% on ${PIN_INVENTORY_REVISION} after removing the tag."

    log_success "=== Candidate verified: ${candidate} (${digest}); serving is unchanged ==="
    cat <<EOF

NEXT (see DEPLOY.md, "Re-pinning ata-inventory-api"):
  $0 write-pin --revision ${candidate} --digest ${digest}
  git add scripts/scenarios/inventory-pin.env && git commit -m "chore(inventory): pin ${candidate}"
  $0 switch
EOF
}

# --- write-pin -----------------------------------------------------------------------

cmd_write_pin() {
    local rev="" digest=""
    while [ $# -gt 0 ]; do
        case "$1" in
            --revision) [ $# -ge 2 ] || die "--revision needs a value"; rev="$2"; shift 2 ;;
            --revision=*) rev="${1#*=}"; shift ;;
            --digest) [ $# -ge 2 ] || die "--digest needs a value"; digest="$2"; shift 2 ;;
            --digest=*) digest="${1#*=}"; shift ;;
            *) die "write-pin: unknown option $1 (see --help)" ;;
        esac
    done
    is_valid_inventory_revision "${rev}" || die "--revision '${rev}' is not a ${SERVICE} revision name."
    is_valid_image_digest "${digest}" || die "--digest '${digest}' is not sha256:<64 hex>."
    require_no_pin_override
    if [ "${rev}" = "${PIN_INVENTORY_REVISION}" ]; then
        die "${rev} is already the pin."
    fi
    log_info "--> Verifying ${rev} (read-only)..."
    inventory_revision_matches_baseline "${rev}" "${digest}" \
        || die "${rev} is not a valid baseline revision (above); the pin is unchanged."
    write_pin_file "${rev}" "${digest}" "${PIN_INVENTORY_REVISION}" "${PIN_INVENTORY_DIGEST}"
    log_success "Pin file updated: ${rev} (${digest}); previous ${PIN_INVENTORY_PREVIOUS_REVISION}."
    cat <<EOF

NEXT: commit the pin, then switch traffic:
  git add scripts/scenarios/inventory-pin.env && git commit -m "chore(inventory): pin ${rev}"
  $0 switch
EOF
}

# --- switch / rollback ---------------------------------------------------------------

# route_and_verify <revision>: 100% traffic to <revision>, then serving + HTTP checks.
route_and_verify() {
    local rev="$1" svc_json url
    svc_json="$(service_json)" || die "cannot describe ${SERVICE}."
    log_info "    Serving before: $(python3 "${BASELINE_PY}" serving <<<"${svc_json}" | tr '\n' ' ')"
    if expect_serving "${rev}" <<<"${svc_json}" 2>/dev/null; then
        log_info "    Traffic already 100% on ${rev}; not changing it."
    else
        gcloud run services update-traffic "${SERVICE}" --project="${PROJECT_ID}" --region="${REGION}" \
            --to-revisions="${rev}=100" --quiet || die "could not route traffic to ${rev}."
    fi
    PINNED_INVENTORY_REVISION="${rev}"
    verify_inventory_serving_revision "${rev}" || die "${rev} is not serving 100% after the switch."
    log_success "    ${rev} serves 100% of ${SERVICE}."
    url="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["status"]["url"])' <<<"${svc_json}")"
    verify_http "${url}" "${url}"
}

cmd_switch() {
    require_no_pin_override
    log_info "=== Re-pin switch: 100% of ${SERVICE} to ${PIN_INVENTORY_REVISION} ==="
    verify_pinned_inventory_revision || die "the pinned revision does not run the pinned digest."
    inventory_revision_matches_baseline "${PIN_INVENTORY_REVISION}" "${PIN_INVENTORY_DIGEST}" \
        || die "${PIN_INVENTORY_REVISION} is not a valid baseline revision (above); traffic unchanged."
    if ! route_and_verify "${PIN_INVENTORY_REVISION}"; then
        log_error "FATAL: ${PIN_INVENTORY_REVISION} serves traffic but failed the HTTP checks (above)." >&2
        log_error "Roll back: $0 rollback   (traffic to ${PIN_INVENTORY_PREVIOUS_REVISION:-<no previous pin>})" >&2
        exit 1
    fi
    if ! inventory_template_matches_baseline; then
        log_warn "The service template is off baseline (above); ./scripts/reset-scenarios.sh restores it." >&2
    fi
    log_success "=== ${SERVICE} serves the pinned revision ${PIN_INVENTORY_REVISION} ==="
    log_info "Next: ./scripts/reset-scenarios.sh (restocks the demo items), then preflight."
}

cmd_rollback() {
    local back_rev="${PIN_INVENTORY_PREVIOUS_REVISION}" back_digest="${PIN_INVENTORY_PREVIOUS_DIGEST}"
    local failed_rev="${PIN_INVENTORY_REVISION}" failed_digest="${PIN_INVENTORY_DIGEST}" actual
    require_no_pin_override
    [ -n "${back_rev}" ] || die "no PIN_INVENTORY_PREVIOUS_REVISION in ${INVENTORY_PIN_FILE}; nothing to roll back to."
    log_info "=== Re-pin rollback: 100% of ${SERVICE} back to ${back_rev} ==="
    actual="$(gcloud run revisions describe "${back_rev}" --project="${PROJECT_ID}" --region="${REGION}" \
        --format="value(status.imageDigest)")" || die "cannot read ${back_rev}."
    [ "${actual##*@}" = "${back_digest}" ] || die "${back_rev} runs '${actual##*@}', expected ${back_digest}; traffic unchanged."
    # The previous pin may predate the current baseline (e.g. created before the instance
    # cap): report the differences, do not hide them. The service-level max still applies.
    if ! inventory_revision_matches_baseline "${back_rev}" "${back_digest}"; then
        log_warn "Rolling back to ${back_rev}, which differs from the current baseline (above)." >&2
    fi
    PINNED_INVENTORY_REVISION="${back_rev}"
    if ! route_and_verify "${back_rev}"; then
        die "${back_rev} serves traffic but failed the HTTP checks (above); the pin file is unchanged."
    fi
    write_pin_file "${back_rev}" "${back_digest}" "${failed_rev}" "${failed_digest}"
    log_success "=== Rolled back: ${back_rev} serves 100%; pin file swapped back (previous: ${failed_rev}) ==="
    cat <<EOF

NEXT: commit the pin file, then reset:
  git add scripts/scenarios/inventory-pin.env && git commit -m "chore(inventory): roll back pin to ${back_rev}"
  ./scripts/reset-scenarios.sh
EOF
}

# --- main ------------------------------------------------------------------------------

if [ "${MOCK_MODE}" = "true" ] || [ "${DRY_RUN}" = "true" ]; then
    die "$0 has no mock/dry-run mode (status is the read-only command); unset MOCK_MODE/DRY_RUN."
fi

COMMAND="${1:-}"
[ $# -gt 0 ] && shift
case "${COMMAND}" in
    status) cmd_status "$@" ;;
    prepare) cmd_prepare "$@" ;;
    write-pin) cmd_write_pin "$@" ;;
    switch) cmd_switch "$@" ;;
    rollback) cmd_rollback "$@" ;;
    -h|--help|help) usage ;;
    "") usage >&2; exit 2 ;;
    *) log_error "Unknown command: ${COMMAND}" >&2; usage >&2; exit 2 ;;
esac
