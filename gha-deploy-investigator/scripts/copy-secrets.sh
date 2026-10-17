#!/usr/bin/env bash
set -euo pipefail

# Helper script to copy secret values at deploy time from existing incident-sre-agent
# Secret Manager secrets to gdi- secrets in GCP without committing values or echoing to stdout.

PROJECT_ID="${PROJECT_ID:-}"
SOURCE_PREFIX="${SOURCE_PREFIX:-isa}"
TARGET_PREFIX="${TARGET_PREFIX:-gdi}"

usage() {
    cat <<EOF
Usage: $0 [options]

Options:
  --project-id ID        GCP Project ID (or set PROJECT_ID env var)
  --source-prefix PRE    Source secret prefix (default: isa)
  --target-prefix PRE    Target secret prefix (default: gdi)
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
        --source-prefix)
            SOURCE_PREFIX="$2"
            shift 2
            ;;
        --target-prefix)
            TARGET_PREFIX="$2"
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
    echo "ERROR: --project-id is required or PROJECT_ID environment variable must be set." >&2
    exit 1
fi

echo "========================================================"
echo "Copying Secret Manager secrets for GHA Deploy Investigator"
echo "Project:       ${PROJECT_ID}"
echo "Source prefix: ${SOURCE_PREFIX}"
echo "Target prefix: ${TARGET_PREFIX}"
echo "========================================================"

copy_secret() {
    local source_secret="$1"
    local target_secret="$2"

    echo "Processing secret: ${target_secret}..."

    # Ensure target secret exists
    if ! gcloud secrets describe "${target_secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "Creating target secret resource: ${target_secret}"
        gcloud secrets create "${target_secret}" \
            --project="${PROJECT_ID}" \
            --replication-policy="automatic"
    fi

    # Check if source secret exists with source prefix or without
    local actual_source=""
    if gcloud secrets describe "${source_secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        actual_source="${source_secret}"
    elif gcloud secrets describe "${source_secret#${SOURCE_PREFIX}-}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        actual_source="${source_secret#${SOURCE_PREFIX}-}"
    fi

    if [ -n "${actual_source}" ]; then
        echo "Copying latest secret version from ${actual_source} -> ${target_secret}..."
        gcloud secrets versions access latest \
            --secret="${actual_source}" \
            --project="${PROJECT_ID}" | \
        gcloud secrets versions add "${target_secret}" \
            --project="${PROJECT_ID}" \
            --data-file=- >/dev/null
        echo "[SUCCESS] Copied ${actual_source} to ${target_secret}"
    else
        echo "[WARNING] Source secret ${source_secret} not found in project ${PROJECT_ID}."
        echo "Please add a version manually via: gcloud secrets versions add ${target_secret} --data-file=/path/to/secret --project=${PROJECT_ID}"
    fi
}

# 1. Copy ServiceNow credentials
copy_secret "${SOURCE_PREFIX}-servicenow-password" "${TARGET_PREFIX}-servicenow-password"
copy_secret "${SOURCE_PREFIX}-servicenow-webhook-token" "${TARGET_PREFIX}-servicenow-webhook-token"

# 2. Check / initialize GitHub App credentials
echo "Checking GitHub secrets (${TARGET_PREFIX}-github-webhook-secret, ${TARGET_PREFIX}-github-app-private-key)..."
for gh_secret in "${TARGET_PREFIX}-github-webhook-secret" "${TARGET_PREFIX}-github-app-private-key"; do
    if ! gcloud secrets describe "${gh_secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "Creating target secret resource: ${gh_secret}"
        gcloud secrets create "${gh_secret}" \
            --project="${PROJECT_ID}" \
            --replication-policy="automatic"
    fi
    if ! gcloud secrets versions describe latest --secret="${gh_secret}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        echo "[NOTICE] ${gh_secret} has no versions. Add secret version with: gcloud secrets versions add ${gh_secret} --data-file=<path> --project=${PROJECT_ID}"
    else
        echo "[OK] ${gh_secret} has active version."
    fi
done

echo "========================================================"
echo "Secret replication step complete."
echo "========================================================"
