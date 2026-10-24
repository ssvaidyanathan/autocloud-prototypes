#!/usr/bin/env bash
# ==============================================================================
# Helper Script to Populate ServiceNow Credentials into Secret Manager
# Never echoes or prints secret values.
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"

echo "==================================================================="
echo "  Populate Secret Manager Secrets for Alert Triage Agent (${NAME_PREFIX}) "
echo "  Project: ${PROJECT_ID}"
echo "==================================================================="

set_secret() {
    local secret_name="$1"
    local prompt_msg="$2"
    local is_sensitive="${3:-true}"
    local full_name="${NAME_PREFIX}-${secret_name}"

    # Ensure secret resource exists
    if ! gcloud secrets describe "${full_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "Creating secret placeholder ${full_name}..."
        gcloud secrets create "${full_name}" \
            --project="${PROJECT_ID}" \
            --replication-policy="automatic" \
            --labels="app=alert-triage-agent,prefix=${NAME_PREFIX}"
    fi

    # Read value safely
    local secret_val=""
    if [ "${is_sensitive}" = true ]; then
        read -r -s -p "${prompt_msg}: " secret_val
        echo ""
    else
        read -r -p "${prompt_msg}: " secret_val
    fi

    if [ -n "${secret_val}" ]; then
        printf "%s" "${secret_val}" | gcloud secrets versions add "${full_name}" \
            --project="${PROJECT_ID}" \
            --data-file=- >/dev/null
        echo "  Updated secret ${full_name} with a new version."
    else
        echo "  Skipped ${full_name} (empty value provided)."
    fi
}

set_secret "servicenow-username" "Enter ServiceNow Username (e.g. admin)" false
set_secret "servicenow-password" "Enter ServiceNow Password" true

echo ""
echo "Secrets configuration completed."
