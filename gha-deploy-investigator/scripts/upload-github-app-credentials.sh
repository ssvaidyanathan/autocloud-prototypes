#!/usr/bin/env bash
set -euo pipefail

# Script to upload GitHub App credentials (App ID, Webhook Secret, Private Key)
# to Google Cloud Secret Manager and configure Terraform tfvars for deployment.

PROJECT_ID="${PROJECT_ID:-}"
APP_ID="${APP_ID:-}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-}"
PRIVATE_KEY_FILE="${PRIVATE_KEY_FILE:-}"
PREFIX="${PREFIX:-gdi}"

usage() {
    cat <<EOF
Usage: $0 [options]

Uploads GitHub App credentials to GCP Secret Manager and updates terraform.tfvars.

Options:
  --project-id ID              GCP Project ID (or set PROJECT_ID env var)
  --app-id NUMERIC_ID          GitHub App numeric ID
  --private-key-file PATH      Path to downloaded GitHub App private key (.pem)
  --webhook-secret SECRET      Webhook secret configured in GitHub App
  --prefix PRE                 Resource prefix (default: gdi)
  -h, --help                   Show this help message

Example:
  $0 \\
    --project-id your-gcp-project-id \\
    --app-id 1234567 \\
    --private-key-file ~/Downloads/my-app.2026-10-17.private-key.pem \\
    --webhook-secret "my-secure-webhook-secret"
EOF
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --project-id)
            PROJECT_ID="$2"
            shift 2
            ;;
        --app-id)
            APP_ID="$2"
            shift 2
            ;;
        --private-key-file)
            PRIVATE_KEY_FILE="$2"
            shift 2
            ;;
        --webhook-secret)
            WEBHOOK_SECRET="$2"
            shift 2
            ;;
        --prefix)
            PREFIX="$2"
            shift 2
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
    echo "ERROR: --project-id is required." >&2
    exit 1
fi

if [ -z "${APP_ID}" ]; then
    echo "ERROR: --app-id is required." >&2
    exit 1
fi

if [ -z "${PRIVATE_KEY_FILE}" ] || [ ! -f "${PRIVATE_KEY_FILE}" ]; then
    echo "ERROR: --private-key-file must point to a valid .pem file." >&2
    exit 1
fi

if [ -z "${WEBHOOK_SECRET}" ]; then
    echo "ERROR: --webhook-secret is required." >&2
    exit 1
fi

echo "========================================================"
echo "Uploading GitHub App Credentials to GCP Secret Manager"
echo "Project ID: ${PROJECT_ID}"
echo "App ID:     ${APP_ID}"
echo "Prefix:     ${PREFIX}"
echo "========================================================"

ensure_secret() {
    local secret_name="$1"
    if ! gcloud secrets describe "${secret_name}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "Creating secret: ${secret_name}"
        gcloud secrets create "${secret_name}" \
            --project="${PROJECT_ID}" \
            --replication-policy="automatic"
    fi
}

# 1. Upload Private Key PEM
KEY_SECRET="${PREFIX}-github-app-private-key"
ensure_secret "${KEY_SECRET}"
echo "Adding new version to ${KEY_SECRET} from ${PRIVATE_KEY_FILE}..."
gcloud secrets versions add "${KEY_SECRET}" \
    --data-file="${PRIVATE_KEY_FILE}" \
    --project="${PROJECT_ID}"
echo "[OK] Private key successfully stored in ${KEY_SECRET}"

# 2. Upload Webhook Secret
WEBHOOK_SECRET_NAME="${PREFIX}-github-webhook-secret"
ensure_secret "${WEBHOOK_SECRET_NAME}"
echo "Adding new version to ${WEBHOOK_SECRET_NAME}..."
gcloud secrets versions add "${WEBHOOK_SECRET_NAME}" \
    --data-file=<(printf "%s" "${WEBHOOK_SECRET}") \
    --project="${PROJECT_ID}"
echo "[OK] Webhook secret successfully stored in ${WEBHOOK_SECRET_NAME}"

# 3. Update or generate terraform.tfvars for agent deployment
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TFVARS_FILE="${SCRIPT_DIR}/../agent/infra/terraform/terraform.tfvars"

echo "Updating ${TFVARS_FILE} with github_app_id..."
if [ -f "${TFVARS_FILE}" ]; then
    # Determine sed in-place arguments for portability (macOS vs Linux)
    sedi_args=("-i")
    if [[ "$(uname)" == "Darwin" ]]; then
      sedi_args=("-i" "") # For macOS, sed -i requires an extension argument. "" means no backup.
    fi

    # Replace existing github_app_id or append
    if grep -q "github_app_id" "${TFVARS_FILE}"; then
        sed "${sedi_args[@]}" "s/github_app_id[[:space:]]*=.*/github_app_id = \"${APP_ID}\"/" "${TFVARS_FILE}"
    else
        echo "github_app_id = \"${APP_ID}\"" >> "${TFVARS_FILE}"
    fi
else
    cat <<EOF > "${TFVARS_FILE}"
project_id    = "${PROJECT_ID}"
region        = "us-central1"
environment   = "production"
name_prefix   = "${PREFIX}"
github_app_id = "${APP_ID}"
EOF
fi
echo "[OK] terraform.tfvars configured with App ID: ${APP_ID}"

echo "========================================================"
echo "GitHub App credentials successfully uploaded and configured!"
echo "========================================================"
