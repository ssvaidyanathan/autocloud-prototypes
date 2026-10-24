#!/usr/bin/env bash
set -euo pipefail

echo "=== Deploying ATA Demo Infrastructure (Stage 2) ==="

# 1. Database Tier
echo "--> Applying database Terraform..."
(cd /workspace/alert-triage-agent/infra/terraform/database && \
  /home/scion/.local/bin/terraform init -input=false && \
  /home/scion/.local/bin/terraform apply -auto-approve -input=false)

# 2. Applications Tier
echo "--> Applying applications Terraform..."
(cd /workspace/alert-triage-agent/infra/terraform/applications && \
  /home/scion/.local/bin/terraform init -input=false && \
  /home/scion/.local/bin/terraform apply -auto-approve -input=false)

# 3. Observability Tier
echo "--> Applying observability Terraform..."
(cd /workspace/alert-triage-agent/infra/terraform/observability && \
  /home/scion/.local/bin/terraform init -input=false && \
  /home/scion/.local/bin/terraform apply -auto-approve -input=false)

# 4. Apigee Proxies
echo "--> Deploying Apigee Proxies..."
/workspace/alert-triage-agent/scripts/deploy-apigee-proxies.sh

echo "=== Demo Infrastructure Deployed Successfully ==="
