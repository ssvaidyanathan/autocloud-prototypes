#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

echo "=== Deploying ATA Demo Infrastructure (Stage 2) ==="

# 1. Database Tier
echo "--> Applying database Terraform..."
(cd "${REPO_DIR}/infra/terraform/database" && \
  terraform init -input=false && \
  terraform apply -auto-approve -input=false)

# 2. Applications Tier
echo "--> Applying applications Terraform..."
(cd "${REPO_DIR}/infra/terraform/applications" && \
  terraform init -input=false && \
  terraform apply -auto-approve -input=false)

# 3. Observability Tier
echo "--> Applying observability Terraform..."
(cd "${REPO_DIR}/infra/terraform/observability" && \
  terraform init -input=false && \
  terraform apply -auto-approve -input=false)

# 4. Apigee Proxies
echo "--> Deploying Apigee Proxies..."
"${SCRIPT_DIR}/deploy-apigee-proxies.sh"

echo "=== Demo Infrastructure Deployed Successfully ==="
