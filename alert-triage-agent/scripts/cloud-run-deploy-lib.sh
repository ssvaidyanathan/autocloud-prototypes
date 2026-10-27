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
        echo "ERROR: PROJECT_ID (or GCP_PROJECT) and REGION (or GCP_REGION) are required, e.g. PROJECT_ID=my-project REGION=us-west1 ./scripts/deploy.sh" >&2
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
