#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
AGENT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_DIR="$(cd "${AGENT_DIR}/.." && pwd)"

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
SERVICE_NAME="${SERVICE_NAME:-${NAME_PREFIX}-sre-agent}"
REPOSITORY="${REPOSITORY:-${NAME_PREFIX}-sre-agent}"
IMAGE_NAME="${IMAGE_NAME:-${NAME_PREFIX}-sre-agent}"
GIT_SHORT_SHA="$(git -C "${REPO_DIR}" rev-parse --short HEAD 2>/dev/null || echo "latest")"
IMAGE_TAG="${IMAGE_TAG:-${TAG_NAME:-${GIT_SHORT_SHA}}}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        -b|--tfstate-bucket)
            TFSTATE_BUCKET="$2"
            shift 2
            ;;
        -n|--name-prefix)
            NAME_PREFIX="$2"
            shift 2
            ;;
        *)
            shift
            ;;
    esac
done

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPOSITORY}/${IMAGE_NAME}:${IMAGE_TAG}"

echo "=========================================="
echo "Deploying SRE Agent to Cloud Run"
echo "Project ID:   ${PROJECT_ID}"
echo "Region:       ${REGION}"
echo "Name Prefix:  ${NAME_PREFIX}"
echo "TF State Bkt: ${TFSTATE_BUCKET:-<none>}"
echo "Service Name: ${SERVICE_NAME}"
echo "Image URI:    ${IMAGE_URI}"
echo "=========================================="

if command -v terraform >/dev/null 2>&1 && [ -f "${AGENT_DIR}/infra/terraform/main.tf" ]; then
    echo "Deploying via Terraform in ${AGENT_DIR}/infra/terraform..."
    cd "${AGENT_DIR}/infra/terraform"
    if [ -f "backend.hcl" ]; then
        echo "Initializing Terraform with backend.hcl..."
        terraform init -backend-config=backend.hcl
    elif [ -n "${TFSTATE_BUCKET}" ]; then
        echo "Generating backend.hcl for agent stack..."
        cat > backend.hcl <<EOF
bucket = "${TFSTATE_BUCKET}"
prefix = "${NAME_PREFIX}/agent"
EOF
        terraform init -backend-config=backend.hcl
    else
        echo "No backend config provided; initializing with -backend=false..."
        terraform init -backend=false
    fi
    terraform apply -auto-approve \
        -var="project_id=${PROJECT_ID}" \
        -var="region=${REGION}" \
        -var="name_prefix=${NAME_PREFIX}" \
        -var="image_tag=${IMAGE_TAG}"
elif command -v gcloud >/dev/null 2>&1; then
    echo "Deploying directly via gcloud run deploy..."
    gcloud run deploy "${SERVICE_NAME}" \
        --project="${PROJECT_ID}" \
        --region="${REGION}" \
        --image="${IMAGE_URI}" \
        --ingress=internal \
        --no-allow-unauthenticated \
        --no-cpu-throttling \
        --port=8080 \
        --cpu=2 \
        --memory=2Gi \
        --timeout=900s
else
    echo "ERROR: Neither terraform nor gcloud is available in PATH." >&2
    exit 1
fi

echo "Deployment complete for ${SERVICE_NAME}."
