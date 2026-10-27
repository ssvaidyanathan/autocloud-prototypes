#!/usr/bin/env bash
# ==============================================================================
# Deploy the demo infrastructure (Stage 2): database, applications and
# observability Terraform roots, then the Apigee proxies.
#
# Required environment (no defaults; they identify the target):
#   PROJECT_ID       GCP project ID
#   REGION           GCP region, e.g. us-central1
#   NAME_PREFIX      resource name prefix, e.g. ata
#   INVENTORY_IMAGE  inventory API image, e.g.
#                    ${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/${NAME_PREFIX}-inventory-api@sha256:...
#   ORDERS_IMAGE     orders API image (same repository, ${NAME_PREFIX}-orders-api)
# Optional:
#   ENVIRONMENT      environment label (default: demo)
#
# Each root must have a backend.hcl written by scripts/bootstrap-tfstate.sh for
# the same PROJECT_ID and NAME_PREFIX.
# ==============================================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

ENVIRONMENT="${ENVIRONMENT:-demo}"

missing=""
for var_name in PROJECT_ID REGION NAME_PREFIX INVENTORY_IMAGE ORDERS_IMAGE; do
  if [ -z "${!var_name:-}" ]; then
    missing="${missing} ${var_name}"
  fi
done
if [ -n "${missing}" ]; then
  echo "Error: required environment variable(s) not set:${missing}. There are no defaults; export them first (see the header of $0)." >&2
  exit 1
fi

TF_VARS=(
  -var "project_id=${PROJECT_ID}"
  -var "region=${REGION}"
  -var "name_prefix=${NAME_PREFIX}"
)
# Only the database and applications roots declare an environment label variable.
ENV_VAR=(-var "environment=${ENVIRONMENT}")

# Refuse a backend.hcl that belongs to another project or prefix, so this run can
# never read or write another stack's state.
check_backend() {
  local root_dir="$1"
  local hcl="${root_dir}/backend.hcl"
  if [ ! -f "${hcl}" ]; then
    echo "Error: ${hcl} not found. Run scripts/bootstrap-tfstate.sh --project-id ${PROJECT_ID} --region ${REGION} --name-prefix ${NAME_PREFIX} first." >&2
    exit 1
  fi
  if ! grep -Fq "prefix = \"${NAME_PREFIX}/$(basename "${root_dir}")\"" "${hcl}"; then
    echo "Error: ${hcl} is not for NAME_PREFIX=${NAME_PREFIX} (expected prefix = \"${NAME_PREFIX}/$(basename "${root_dir}")\"). Re-run scripts/bootstrap-tfstate.sh for this stack." >&2
    exit 1
  fi
  if [ -z "${TFSTATE_BUCKET:-}" ] && ! grep -Fq "bucket = \"${PROJECT_ID}-${NAME_PREFIX}-tfstate\"" "${hcl}"; then
    echo "Error: ${hcl} does not use bucket ${PROJECT_ID}-${NAME_PREFIX}-tfstate. Re-run scripts/bootstrap-tfstate.sh for this stack, or export TFSTATE_BUCKET if a custom bucket (--bucket) was used." >&2
    exit 1
  fi
}

apply_root() {
  local root="$1"
  shift
  local root_dir="${REPO_DIR}/infra/terraform/${root}"
  check_backend "${root_dir}"
  (cd "${root_dir}" && \
    terraform init -input=false -reconfigure -backend-config=backend.hcl && \
    terraform apply -auto-approve -input=false ${TF_VARS[@]+"${TF_VARS[@]}"} "$@")
}

echo "=== Deploying ATA Demo Infrastructure (Stage 2) ==="
echo "    Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX} | Environment: ${ENVIRONMENT}"

# 1. Database Tier
echo "--> Applying database Terraform..."
apply_root database ${ENV_VAR[@]+"${ENV_VAR[@]}"}

# 2. Applications Tier
echo "--> Applying applications Terraform..."
apply_root applications ${ENV_VAR[@]+"${ENV_VAR[@]}"} \
  -var "inventory_image=${INVENTORY_IMAGE}" \
  -var "orders_image=${ORDERS_IMAGE}"

# 3. Observability Tier
echo "--> Applying observability Terraform..."
apply_root observability

# 4. Apigee Proxies
echo "--> Deploying Apigee Proxies..."
export PROJECT_ID REGION NAME_PREFIX ENVIRONMENT
"${SCRIPT_DIR}/deploy-apigee-proxies.sh"

echo "=== Demo Infrastructure Deployed Successfully ==="
