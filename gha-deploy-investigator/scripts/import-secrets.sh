#!/usr/bin/env bash
set -euo pipefail

# Script to import pre-existing Secret Manager secrets into Terraform state
# for GHA Deploy Investigator (Agent and Bridge stacks) to prevent 409 Conflict errors.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-}"
NAME_PREFIX="${NAME_PREFIX:-gdi}"
STACK="${STACK:-all}"
DRY_RUN=false

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --project-id ID        GCP Project ID (or set PROJECT_ID env var)
  --name-prefix PRE      Resource name prefix (default: gdi)
  --stack STACK          Stack to import: agent, bridge, or all (default: all)
  --dry-run              Print the import commands without executing them
  -h, --help             Show this help message
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-id)
            PROJECT_ID="$2"
            shift 2
            ;;
        --name-prefix)
            NAME_PREFIX="$2"
            shift 2
            ;;
        --stack)
            STACK="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown argument: $1" >&2
            usage
            ;;
    esac
done

if [ -z "${PROJECT_ID}" ]; then
    echo "ERROR: --project-id is required or PROJECT_ID environment variable must be set." >&2
    exit 1
fi

TF_AGENT_DIR="${REPO_ROOT}/agent/infra/terraform"
TF_BRIDGE_DIR="${REPO_ROOT}/bridge/infra/terraform"

echo "========================================================"
echo "Secret Manager Terraform Adoption Helper"
echo "Project:     ${PROJECT_ID}"
echo "Name Prefix: ${NAME_PREFIX}"
echo "Stack:       ${STACK}"
echo "Dry run:     ${DRY_RUN}"
echo "========================================================"

# Helper function to check if a secret exists in GCP
secret_exists_in_gcp() {
    local secret_id="$1"
    gcloud secrets describe "${secret_id}" --project="${PROJECT_ID}" >/dev/null 2>&1
}

# Helper function to check if resource is already in Terraform state
is_in_tf_state() {
    local tf_dir="$1"
    local resource_addr="$2"
    if ! command -v terraform >/dev/null 2>&1; then
        return 1
    fi
    terraform -chdir="${tf_dir}" state list 2>/dev/null | grep -Fxq "${resource_addr}" || return 1
}

# Helper to run or print terraform import
import_secret() {
    local tf_dir="$1"
    local resource_addr="$2"
    local secret_id="$3"
    shift 3
    local extra_args=("$@")

    local secret_path="projects/${PROJECT_ID}/secrets/${secret_id}"

    if is_in_tf_state "${tf_dir}" "${resource_addr}"; then
        echo "[SKIP] ${resource_addr} already exists in Terraform state (${tf_dir})."
        return 0
    fi

    echo "Importing ${secret_id} -> ${resource_addr}..."
    if [ "${DRY_RUN}" = true ]; then
        echo "  [DRY RUN] terraform -chdir=\"${tf_dir}\" import -var=\"project_id=${PROJECT_ID}\" -var=\"name_prefix=${NAME_PREFIX}\" ${extra_args[*]} \"${resource_addr}\" \"${secret_path}\""
        return 0
    fi

    if ! command -v terraform >/dev/null 2>&1; then
        echo "[NOTICE] terraform CLI not found in environment."
        echo "Run manually when terraform is installed:"
        echo "  terraform -chdir=\"${tf_dir}\" import -var=\"project_id=${PROJECT_ID}\" -var=\"name_prefix=${NAME_PREFIX}\" ${extra_args[*]} \"${resource_addr}\" \"${secret_path}\""
        return 0
    fi

    terraform -chdir="${tf_dir}" import \
        -var="project_id=${PROJECT_ID}" \
        -var="name_prefix=${NAME_PREFIX}" \
        "${extra_args[@]}" \
        "${resource_addr}" \
        "${secret_path}"
    echo "[SUCCESS] Imported ${resource_addr}."
}

# 1. Agent Stack Secret Adoption
if [[ "${STACK}" == "all" || "${STACK}" == "agent" ]]; then
    echo "--------------------------------------------------------"
    echo "Checking Agent stack secrets..."
    echo "--------------------------------------------------------"
    GH_KEY_SECRET="${NAME_PREFIX}-github-app-private-key"
    if secret_exists_in_gcp "${GH_KEY_SECRET}"; then
        import_secret "${TF_AGENT_DIR}" "google_secret_manager_secret.github_app_private_key" "${GH_KEY_SECRET}"
    else
        echo "[INFO] ${GH_KEY_SECRET} not found in GCP Secret Manager (will be created by Terraform if absent)."
    fi
fi

# 2. Bridge Stack Secret Adoption
if [[ "${STACK}" == "all" || "${STACK}" == "bridge" ]]; then
    echo "--------------------------------------------------------"
    echo "Checking Bridge stack secrets..."
    echo "--------------------------------------------------------"

    # GitHub Webhook Secret
    GH_WEBHOOK_SECRET="${NAME_PREFIX}-github-webhook-secret"
    if secret_exists_in_gcp "${GH_WEBHOOK_SECRET}"; then
        import_secret "${TF_BRIDGE_DIR}" "google_secret_manager_secret.github_webhook_secret" "${GH_WEBHOOK_SECRET}"
    fi

    # ServiceNow Password Secret: check gdi-bridge-servicenow-password then gdi-servicenow-password
    SN_PWD_SECRET=""
    if secret_exists_in_gcp "${NAME_PREFIX}-bridge-servicenow-password"; then
        SN_PWD_SECRET="${NAME_PREFIX}-bridge-servicenow-password"
    elif secret_exists_in_gcp "${NAME_PREFIX}-servicenow-password"; then
        SN_PWD_SECRET="${NAME_PREFIX}-servicenow-password"
    fi

    if [ -n "${SN_PWD_SECRET}" ]; then
        extra_vars=()
        if [ "${SN_PWD_SECRET}" != "${NAME_PREFIX}-servicenow-password" ]; then
            extra_vars+=("-var=servicenow_password_secret_id=${SN_PWD_SECRET}")
        fi
        import_secret "${TF_BRIDGE_DIR}" "google_secret_manager_secret.servicenow_password" "${SN_PWD_SECRET}" "${extra_vars[@]}"
    fi

    # ServiceNow Webhook Token: check gdi-bridge-servicenow-webhook-token then gdi-servicenow-webhook-token
    SN_TOKEN_SECRET=""
    if secret_exists_in_gcp "${NAME_PREFIX}-bridge-servicenow-webhook-token"; then
        SN_TOKEN_SECRET="${NAME_PREFIX}-bridge-servicenow-webhook-token"
    elif secret_exists_in_gcp "${NAME_PREFIX}-servicenow-webhook-token"; then
        SN_TOKEN_SECRET="${NAME_PREFIX}-servicenow-webhook-token"
    fi

    if [ -n "${SN_TOKEN_SECRET}" ]; then
        extra_vars=()
        if [ "${SN_TOKEN_SECRET}" != "${NAME_PREFIX}-servicenow-webhook-token" ]; then
            extra_vars+=("-var=servicenow_webhook_token_secret_id=${SN_TOKEN_SECRET}")
        fi
        import_secret "${TF_BRIDGE_DIR}" "google_secret_manager_secret.servicenow_webhook_token" "${SN_TOKEN_SECRET}" "${extra_vars[@]}"
    fi

    # ServiceNow Username Secret: check gdi-bridge-servicenow-username
    SN_USER_SECRET="${NAME_PREFIX}-bridge-servicenow-username"
    if secret_exists_in_gcp "${SN_USER_SECRET}"; then
        import_secret "${TF_BRIDGE_DIR}" "google_secret_manager_secret.servicenow_username[0]" "${SN_USER_SECRET}" "-var=servicenow_username_secret_id=${SN_USER_SECRET}"
    fi
fi

echo "========================================================"
echo "Secret adoption check completed."
echo "========================================================"
