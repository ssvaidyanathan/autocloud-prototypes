#!/usr/bin/env bash
# ==============================================================================
# Deploy Stage 1 Vertical Slice Tracer Bullet
# ==============================================================================

set -euo pipefail

export PATH="/home/scion/.local/bin:$PATH"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
ENVIRONMENT="${ENVIRONMENT:-demo}"

echo "==================================================================="
echo "  Deploying Stage 1 Vertical Slice Tracer Bullet"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"
echo "==================================================================="

# 1. Bootstrap Terraform GCS State Bucket
echo ""
echo "--- Step 1: Bootstrapping Terraform Remote State Bucket ---"
bash "${REPO_ROOT}/scripts/bootstrap-tfstate.sh" \
    --project-id "${PROJECT_ID}" \
    --region "${REGION}" \
    --name-prefix "${NAME_PREFIX}" \
    --env "${ENVIRONMENT}"

# 2. Apply Foundation Terraform
echo ""
echo "--- Step 2: Applying Foundation Terraform ---"
cd "${REPO_ROOT}/infra/terraform/foundation"

# Initialize terraform with backend-config
terraform init -reconfigure -backend-config=backend.hcl

# Apply foundation configuration
terraform apply -auto-approve \
    -var="project_id=${PROJECT_ID}" \
    -var="region=${REGION}" \
    -var="name_prefix=${NAME_PREFIX}" \
    -var="environment=${ENVIRONMENT}"

cd "${REPO_ROOT}"

# 3. Build & Deploy Demo Inventory API
echo ""
echo "--- Step 3: Deploying Demo Inventory API ---"
bash "${REPO_ROOT}/demo/ata-inventory-api/scripts/deploy.sh"

# 4. Build & Deploy ata-agent
echo ""
echo "--- Step 4: Deploying ata-agent ---"
bash "${REPO_ROOT}/agent/scripts/deploy.sh"

# 5. Build & Deploy ata-bridge
echo ""
echo "--- Step 5: Deploying ata-bridge ---"
bash "${REPO_ROOT}/bridge/scripts/deploy.sh"

echo ""
echo "==================================================================="
echo "  Stage 1 Vertical Slice Deployment Completed Successfully"
echo "==================================================================="
