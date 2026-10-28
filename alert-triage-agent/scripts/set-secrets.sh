#!/usr/bin/env bash
# ==============================================================================
# Set the ServiceNow credentials of one stack in Secret Manager. Never prints secret values.
#
# Usage: scripts/set-secrets.sh [--yes] [--skip-if-set] [--create-missing]
#
# Required: PROJECT_ID, NAME_PREFIX.
# Values: SERVICENOW_USERNAME / SERVICENOW_PASSWORD from the environment when exported
# (non-interactive), else prompted for (the password without echo). An empty answer is an
# error, not a skip.
# Secrets: ${NAME_PREFIX}-servicenow-username, ${NAME_PREFIX}-servicenow-password. The
# foundation Terraform root creates them with a "placeholder" version; run this BEFORE the
# bridge deploy (the bridge reads them when an instance starts).
#   --skip-if-set     leave a secret alone when its latest version is not "placeholder"
#                     (compared in memory, never printed)
#   --create-missing  create a missing secret (labels as in Terraform) instead of failing;
#                     only for stacks without the foundation root
#   --yes             no confirmation prompt
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud-run-deploy-lib.sh
source "${SCRIPT_DIR}/cloud-run-deploy-lib.sh"

SKIP_IF_SET=false
CREATE_MISSING=false
for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ASSUME_YES=true ;;
        --skip-if-set) SKIP_IF_SET=true ;;
        --create-missing) CREATE_MISSING=true ;;
        -h|--help) sed -n '2,21p' "$0"; exit 0 ;;
        *) echo "Unknown argument: ${arg} (see --help)" >&2; exit 1 ;;
    esac
done

require_vars PROJECT_ID || exit 1
require_name_prefix || exit 1
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"

echo "==================================================================="
echo "  ServiceNow credentials for stack '${NAME_PREFIX}'"
echo "  Project: ${PROJECT_ID}"
echo "  Secrets: ${NAME_PREFIX}-servicenow-username, ${NAME_PREFIX}-servicenow-password"
echo "==================================================================="
confirm_or_abort || exit 1

# set_secret <suffix> <env var with the value> <prompt> <sensitive true|false>
set_secret() {
    local suffix="$1" env_name="$2" prompt_msg="$3" is_sensitive="$4"
    local full_name="${NAME_PREFIX}-${suffix}" secret_val="" current=""

    if ! gcloud secrets describe "${full_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        if [ "${CREATE_MISSING}" != "true" ]; then
            echo "ERROR: secret ${full_name} does not exist in ${PROJECT_ID} (or cannot be read). The foundation Terraform root creates it: apply foundation first (or pass --create-missing)." >&2
            return 1
        fi
        echo "Creating secret ${full_name}..."
        gcloud secrets create "${full_name}" \
            --project="${PROJECT_ID}" \
            --replication-policy="automatic" \
            --labels="app=alert-triage-agent,stack=${NAME_PREFIX},component=${NAME_PREFIX}-bridge,role=triage-system,tier=platform,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}"
    fi

    if [ "${SKIP_IF_SET}" = "true" ]; then
        current="$(gcloud secrets versions access latest --secret="${full_name}" --project="${PROJECT_ID}" 2>/dev/null || true)"
        if [ -n "${current}" ] && [ "${current}" != "placeholder" ]; then
            current=""
            echo "  ${full_name} already has a value (not 'placeholder'): left as it is (--skip-if-set)."
            return 0
        fi
        current=""
    fi

    secret_val="${!env_name:-}"
    if [ -z "${secret_val}" ]; then
        if [ ! -t 0 ]; then
            echo "ERROR: ${env_name} is not set and stdin is not a terminal to prompt for it." >&2
            return 1
        fi
        if [ "${is_sensitive}" = "true" ]; then
            read -r -s -p "${prompt_msg}: " secret_val
            echo ""
        else
            read -r -p "${prompt_msg}: " secret_val
        fi
    fi
    if [ -z "${secret_val}" ]; then
        echo "ERROR: empty value for ${full_name}: nothing was written." >&2
        return 1
    fi

    printf "%s" "${secret_val}" | gcloud secrets versions add "${full_name}" \
        --project="${PROJECT_ID}" \
        --data-file=- >/dev/null
    secret_val=""
    echo "  Updated secret ${full_name} with a new version."
}

set_secret "servicenow-username" SERVICENOW_USERNAME "Enter ServiceNow username" false
set_secret "servicenow-password" SERVICENOW_PASSWORD "Enter ServiceNow password" true

echo ""
echo "Secrets configuration completed. A bridge that is already running reads the new values only on a new instance: redeploy it (bridge/scripts/deploy.sh) if it was deployed before."
