#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BRIDGE_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${BRIDGE_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
if [ -z "${PROJECT_ID}" ] && command -v gcloud >/dev/null 2>&1; then
    PROJECT_ID="$(gcloud config get-value project 2>/dev/null || true)"
fi
if [ -z "${PROJECT_ID}" ]; then
    echo "ERROR: PROJECT_ID or GCP_PROJECT environment variable is required." >&2
    echo "Usage: PROJECT_ID=<your-project-id> $0" >&2
    exit 1
fi
REGION="${REGION:-${GCP_REGION:-us-central1}}"
NAME_PREFIX="${NAME_PREFIX:-isa}"
REPOSITORY="${REPOSITORY:-${NAME_PREFIX}-incident-bridge}"
IMAGE_NAME="${IMAGE_NAME:-${NAME_PREFIX}-incident-bridge}"
GIT_SHORT_SHA="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo "latest")"
IMAGE_TAG="${IMAGE_TAG:-${TAG_NAME:-${GIT_SHORT_SHA}}}"

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=========================================="
echo "Building and Pushing Incident Bridge Image"
echo "Project ID:  ${PROJECT_ID}"
echo "Region:      ${REGION}"
echo "Name Prefix: ${NAME_PREFIX}"
echo "Image URI:   ${IMAGE_URI}"
echo "=========================================="

if command -v docker >/dev/null 2>&1; then
    echo "Building container image with Docker..."
    docker build -t "${IMAGE_URI}" -f "${BRIDGE_DIR}/Dockerfile" "${BRIDGE_DIR}"
    echo "Pushing container image to Artifact Registry..."
    docker push "${IMAGE_URI}"
elif command -v gcloud >/dev/null 2>&1; then
    echo "Docker not found; submitting build to Google Cloud Build..."
    gcloud builds submit "${REPO_DIR}" \
        --project="${PROJECT_ID}" \
        --config="${BRIDGE_DIR}/cloudbuild.yaml" \
        --substitutions="_REGION=${REGION},_REPOSITORY=${REPOSITORY},_IMAGE_NAME=${IMAGE_NAME},_TAG_NAME=${IMAGE_TAG}"
else
    echo "ERROR: Neither docker nor gcloud is available in PATH." >&2
    exit 1
fi

echo "Successfully built and pushed ${IMAGE_URI}"
