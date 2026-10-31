#!/usr/bin/env bash
set -euo pipefail

# Script to populate Secret Manager secret versions for Incident Bridge.
# Can be run interactively or with values supplied via environment variables.

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
if [ -z "${PROJECT_ID}" ] && command -v gcloud >/dev/null 2>&1; then
    PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [ -z "${PROJECT_ID}" ]; then
    echo "ERROR: PROJECT_ID or GCP_PROJECT environment variable is required." >&2
    echo "Usage: PROJECT_ID=<your-project-id> $0" >&2
    exit 1
fi

NAME_PREFIX="${NAME_PREFIX:-isa}"

echo "=========================================="
echo "Configuring Incident Bridge Secrets"
echo "Project ID:  ${PROJECT_ID}"
echo "Name Prefix: ${NAME_PREFIX}"
echo "=========================================="

set_secret() {
    local secret_name="$1"
    local secret_val="$2"
    local prompt_desc="$3"

    if [ -z "${secret_val}" ]; then
        if [ -t 0 ]; then
            read -r -s -p "Enter value for ${prompt_desc} (${secret_name}) [press enter to skip]: " secret_val
            echo ""
        else
            echo "Skipping ${secret_name} (no value provided and non-interactive)."
            return 0
        fi
    fi

    if [ -n "${secret_val}" ]; then
        echo "Adding secret version for ${secret_name}..."
        printf "%s" "${secret_val}" | gcloud secrets versions add "${secret_name}" \
            --project="${PROJECT_ID}" \
            --data-file=-
        echo "Added secret version for ${secret_name}."
    else
        echo "Skipped ${secret_name}."
    fi
}

# 1. ServiceNow Secrets
set_secret "${NAME_PREFIX}-bridge-servicenow-username" "${SERVICENOW_USERNAME:-}" "ServiceNow Username"
set_secret "${NAME_PREFIX}-bridge-servicenow-password" "${SERVICENOW_PASSWORD:-}" "ServiceNow Password"
set_secret "${NAME_PREFIX}-bridge-servicenow-webhook-token" "${SERVICENOW_WEBHOOK_TOKEN:-}" "ServiceNow Webhook Bearer Token"

# 2. Jira Secrets
set_secret "${NAME_PREFIX}-bridge-jira-email" "${JIRA_EMAIL:-}" "Jira Account Email"
set_secret "${NAME_PREFIX}-bridge-jira-api-token" "${JIRA_API_TOKEN:-}" "Jira API Token"
set_secret "${NAME_PREFIX}-bridge-jira-webhook-token" "${JIRA_WEBHOOK_TOKEN:-}" "Jira Webhook Token"

# 3. Generic Webhook Secret
set_secret "${NAME_PREFIX}-bridge-generic-hmac-key" "${GENERIC_HMAC_KEY:-}" "Generic Webhook HMAC Key"

echo "Secrets setup completed for project ${PROJECT_ID}."
