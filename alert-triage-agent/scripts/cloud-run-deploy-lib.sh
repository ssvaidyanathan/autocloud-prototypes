#!/usr/bin/env bash
# ==============================================================================
# Shared helpers for the Cloud Run deploy scripts (agent, bridge, tools).
# Sourced, not executed. Needs PROJECT_ID and REGION. bash 3.2 and BSD userland safe.
#
# Every lookup either returns a value or fails with the cause: a failed gcloud call is
# never treated as "service not deployed", and no URL is hardcoded.
# ==============================================================================

# Fail unless the deploy target is configured. PROJECT_ID and REGION have no defaults: a
# missing value must not deploy into some other project or region.
require_project_and_region() {
    if [ -z "${PROJECT_ID:-}" ] || [ -z "${REGION:-}" ]; then
        echo "ERROR: PROJECT_ID (or GCP_PROJECT) and REGION (or GCP_REGION) are required, e.g. PROJECT_ID=my-project REGION=us-west1 ./scripts/deploy.sh" >&2  # prefix-ok: example values
        return 1
    fi
}

# Print the status.url of Cloud Run service $1, or nothing when the service does not exist.
# Returns 1 (after an error on stderr) when the services cannot be listed.
existing_service_url() {
    local name="$1" listing
    if ! listing="$(gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" \
            --filter="metadata.name=${name}" --format="value(metadata.name,status.url)")"; then
        echo "ERROR: could not list Cloud Run services in ${PROJECT_ID}/${REGION} to look up ${name} (see gcloud's error above)." >&2
        return 1
    fi
    # The filter is not an exact match: keep only the line whose name is exactly $1.
    printf '%s\n' "${listing}" | awk -v n="${name}" '$1 == n { print $2 }'
}

# Print the numeric project number: PROJECT_NUMBER from the environment, else
# `gcloud projects describe`. Returns 1 (after an error on stderr) when neither works.
resolve_project_number() {
    local number="${PROJECT_NUMBER:-}"
    if [ -z "${number}" ]; then
        number="$(gcloud projects describe "${PROJECT_ID}" --format="value(projectNumber)")" || number=""
    fi
    case "${number}" in
        ""|*[!0-9]*)
            echo "ERROR: PROJECT_NUMBER is required: set it or grant access to 'gcloud projects describe ${PROJECT_ID}' (got '${number}')." >&2
            return 1
            ;;
    esac
    printf '%s\n' "${number}"
}

# Print the URL to use for Cloud Run service $1: its status.url when it is deployed, else
# its deterministic URL https://<service>-<project number>.<region>.run.app (said on stderr).
service_url_or_deterministic() {
    local name="$1" url number
    url="$(existing_service_url "${name}")" || return 1
    if [ -n "${url}" ]; then
        printf '%s\n' "${url}"
        return 0
    fi
    number="$(resolve_project_number)" || return 1
    url="https://${name}-${number}.${REGION}.run.app"
    echo "NOTE: ${name} is not deployed in ${PROJECT_ID}/${REGION} yet; using its deterministic URL ${url}." >&2
    printf '%s\n' "${url}"
}

# assert_stack_url <VAR> <service suffix>: the URL in $VAR must be this stack's Cloud Run
# service https://${NAME_PREFIX}-<suffix>-... or https://${NAME_PREFIX}-<suffix>.<...>.
# Catches URLs exported for another stack in the same shell (an ata deploy, then atat), which
# would otherwise fail only at runtime. ALLOW_CUSTOM_SERVICE_URLS=true accepts any https URL
# (custom domains), said on stderr. Returns 1 with the reason otherwise.
assert_stack_url() {
    local var="$1" suffix="$2" url
    url="${!var:-}"
    case "${url}" in
        https://"${NAME_PREFIX}-${suffix}"[-.]*) return 0 ;;
    esac
    if [ "${ALLOW_CUSTOM_SERVICE_URLS:-}" = "true" ]; then
        case "${url}" in
            https://?*)
                echo "NOTE: ${var}=${url} is not a ${NAME_PREFIX}-${suffix} Cloud Run URL; accepted because ALLOW_CUSTOM_SERVICE_URLS=true." >&2
                return 0 ;;
        esac
    fi
    echo "ERROR: ${var}=${url} is not the ${NAME_PREFIX}-${suffix} Cloud Run URL (https://${NAME_PREFIX}-${suffix}-...)." >&2
    echo "       Unset it to derive the URL, or set ALLOW_CUSTOM_SERVICE_URLS=true for a custom domain." >&2
    return 1
}

# ------------------------------------------------------------------------------
# Inputs, confirmation, labels and image builds (shared by the deploy scripts,
# scripts/build-image.sh and scripts/deploy-stage1.sh).
# ------------------------------------------------------------------------------

# Directory of this file and the alert-triage-agent root (the topology source lives there).
CLOUD_RUN_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CLOUD_RUN_ROOT_DIR="$(cd "${CLOUD_RUN_LIB_DIR}/.." && pwd)"

# require_vars VAR...: fail (return 1) naming every listed variable that is unset or empty.
require_vars() {
    local v missing=""
    for v in "$@"; do
        if [ -z "${!v:-}" ]; then
            missing="${missing} ${v}"
        fi
    done
    if [ -n "${missing}" ]; then
        echo "ERROR: required input(s) not set:${missing}. There are no defaults: export them first." >&2
        return 1
    fi
}

# Fail unless NAME_PREFIX is set and valid (the Terraform roots' rule: ^[a-z][a-z0-9]{1,9}$).
require_name_prefix() {
    if [ -z "${NAME_PREFIX:-}" ]; then
        echo "ERROR: NAME_PREFIX is required (e.g. NAME_PREFIX=ata); it has no default." >&2
        return 1
    fi
    if ! printf '%s\n' "${NAME_PREFIX}" | grep -Eq '^[a-z][a-z0-9]{1,9}$'; then
        echo "ERROR: NAME_PREFIX '${NAME_PREFIX}' is invalid: use 2-10 lowercase letters or digits, starting with a letter." >&2
        return 1
    fi
}

# Ask before a mutating step. ASSUME_YES=true (the scripts' --yes) skips the question. Without
# a terminal on stdin there is nobody to ask: fail and say how to proceed.
confirm_or_abort() {
    local answer=""
    if [ "${ASSUME_YES:-false}" = "true" ]; then
        return 0
    fi
    if [ ! -t 0 ]; then
        echo "ERROR: confirmation required, but stdin is not a terminal. Check the target above and re-run with --yes." >&2
        return 1
    fi
    printf 'Proceed? [y/N] ' >&2
    read -r answer || answer=""
    case "${answer}" in
        y|Y|yes|YES) return 0 ;;
    esac
    echo "Aborted: nothing was changed." >&2
    return 1
}

# Labels of a script-managed Cloud Run service: the Terraform common labels (app, stack, role,
# tier, env, owner) with managed-by=script. Usage: stack_labels <component> <role> <tier>.
stack_labels() {
    printf 'app=alert-triage-agent,stack=%s,component=%s,role=%s,tier=%s,env=%s,managed-by=script,owner=%s\n' \
        "${NAME_PREFIX}" "$1" "$2" "$3" "${ENVIRONMENT}" "${OWNER}"
}

# The stack's Artifact Registry Docker repository (foundation: ${NAME_PREFIX}-docker in REGION).
artifact_repo() {
    printf '%s-docker.pkg.dev/%s/%s-docker\n' "${REGION}" "${PROJECT_ID}" "${NAME_PREFIX}"
}

# Cloud Build flags of every build: the stack's build SA and build bucket (foundation:
# ${NAME_PREFIX}-build-sa, gs://${PROJECT_ID}-${NAME_PREFIX}-build). A user-specified build SA
# needs an explicit log bucket, and the staging dir keeps sources out of the default bucket.
set_cloud_build_flags() {
    CLOUD_BUILD_SA="${NAME_PREFIX}-build-sa@${PROJECT_ID}.iam.gserviceaccount.com"
    CLOUD_BUILD_BUCKET="gs://${PROJECT_ID}-${NAME_PREFIX}-build"
    CLOUD_BUILD_FLAGS=(
        "--service-account=projects/${PROJECT_ID}/serviceAccounts/${CLOUD_BUILD_SA}"
        "--gcs-source-staging-dir=${CLOUD_BUILD_BUCKET}/source"
        "--gcs-log-dir=${CLOUD_BUILD_BUCKET}/logs"
    )
}

# Image tag for a build of directory $1: IMAGE_TAG when set; else the git commit (12 chars),
# with "-dirty-<UTC timestamp>" when $1 has uncommitted changes; else "t<UTC timestamp>".
default_image_tag() {
    local dir="$1" sha stamp
    stamp="$(date -u +%Y%m%d%H%M%S)"
    if [ -n "${IMAGE_TAG:-}" ]; then
        if ! printf '%s\n' "${IMAGE_TAG}" | grep -Eq '^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$'; then
            echo "ERROR: IMAGE_TAG '${IMAGE_TAG}' is not a valid Docker tag." >&2
            return 1
        fi
        printf '%s\n' "${IMAGE_TAG}"
        return 0
    fi
    if sha="$(git -C "${dir}" rev-parse --short=12 HEAD 2>/dev/null)" && [ -n "${sha}" ]; then
        if [ -n "$(git -C "${dir}" status --porcelain -- . 2>/dev/null)" ]; then
            sha="${sha}-dirty-${stamp}"
        fi
        printf '%s\n' "${sha}"
        return 0
    fi
    printf 't%s\n' "${stamp}"
}

# build_image <context dir> <image name>: build the directory with Cloud Build (build SA and
# bucket above) into $(artifact_repo)/<image name>:<tag>, then print the pushed image by
# digest, <repo>/<image name>@sha256:<hex>, on stdout. Progress goes to stderr. A Dockerfile
# that copies topology/ gets the single topology package synced into the context for the
# build. Returns 1, after an error on stderr, if any step fails. Safe in $(...): every step
# is checked explicitly (set -e does not apply in command substitution on bash 3.2).
build_image() {
    local dir="$1" name="$2" repo tag ref digest rc=0 synced=false
    if [ ! -f "${dir}/Dockerfile" ]; then
        echo "ERROR: ${dir}/Dockerfile not found: not a service directory." >&2
        return 1
    fi
    if ! printf '%s\n' "${name}" | grep -Eq '^[a-z0-9][a-z0-9._-]*$'; then
        echo "ERROR: image name '${name}' is invalid (lowercase letters, digits, '.', '_', '-')." >&2
        return 1
    fi
    repo="$(artifact_repo)"
    tag="$(default_image_tag "${dir}")" || return 1
    ref="${repo}/${name}:${tag}"
    set_cloud_build_flags
    if grep -Eq '^[[:space:]]*COPY[[:space:]]+topology/?[[:space:]]' "${dir}/Dockerfile"; then
        echo "Syncing the topology package into the build context of ${name}..." >&2
        rm -rf "${dir}/topology"
        if ! cp -R "${CLOUD_RUN_ROOT_DIR}/topology" "${dir}/topology"; then
            rm -rf "${dir}/topology"
            echo "ERROR: could not copy ${CLOUD_RUN_ROOT_DIR}/topology into ${dir}." >&2
            return 1
        fi
        synced=true
    fi
    echo "Building ${ref} with Cloud Build (service account ${CLOUD_BUILD_SA}, bucket ${CLOUD_BUILD_BUCKET})..." >&2
    gcloud builds submit "${dir}" --project="${PROJECT_ID}" --tag="${ref}" ${CLOUD_BUILD_FLAGS[@]+"${CLOUD_BUILD_FLAGS[@]}"} >&2 || rc=$?
    if [ "${synced}" = "true" ]; then
        rm -rf "${dir}/topology"
    fi
    if [ "${rc}" -ne 0 ]; then
        echo "ERROR: Cloud Build failed for ${ref} (exit ${rc}; see the build log above)." >&2
        return 1
    fi
    if ! digest="$(gcloud artifacts docker images describe "${ref}" --project="${PROJECT_ID}" \
            --format="value(image_summary.digest)")"; then
        echo "ERROR: could not read the digest of ${ref} from Artifact Registry." >&2
        return 1
    fi
    if ! printf '%s\n' "${digest}" | grep -Eq '^sha256:[0-9a-f]{64}$'; then
        echo "ERROR: unexpected digest for ${ref}: '${digest}' (expected sha256:<64 hex>)." >&2
        return 1
    fi
    echo "Pushed ${ref} (${digest})." >&2
    printf '%s@%s\n' "${repo}/${name}" "${digest}"
}

# resolve_service_image <prebuilt image or ""> <context dir> <image name>: print the image to
# deploy. A prebuilt image (e.g. AGENT_IMAGE from scripts/deploy-stage1.sh) must be in this
# stack's repository; otherwise build the directory (build_image).
resolve_service_image() {
    local prebuilt="$1" dir="$2" name="$3" repo
    repo="$(artifact_repo)"
    if [ -n "${prebuilt}" ]; then
        case "${prebuilt}" in
            "${repo}/${name}@sha256:"*|"${repo}/${name}:"*)
                echo "Using the prebuilt image ${prebuilt} (no build)." >&2
                printf '%s\n' "${prebuilt}"
                return 0
                ;;
        esac
        echo "ERROR: prebuilt image '${prebuilt}' is not ${repo}/${name}@sha256:<hex> (or :<tag>) of this stack." >&2
        return 1
    fi
    build_image "${dir}" "${name}"
}
