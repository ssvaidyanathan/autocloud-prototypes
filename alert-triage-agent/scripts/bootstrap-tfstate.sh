#!/usr/bin/env bash
# ==============================================================================
# Bootstrap Terraform State GCS Bucket & Generate Backend Configuration
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Color codes
if [ -t 1 ]; then
    BOLD="$(printf '\033[1m')"
    GREEN="$(printf '\033[0;32m')"
    BLUE="$(printf '\033[0;34m')"
    YELLOW="$(printf '\033[0;33m')"
    RED="$(printf '\033[0;31m')"
    NC="$(printf '\033[0m')"
else
    BOLD=""
    GREEN=""
    BLUE=""
    YELLOW=""
    RED=""
    NC=""
fi

# Defaults
PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"
DRY_RUN=false

print_usage() {
    cat <<EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
  -p, --project-id <id>     GCP Project ID (default: ${PROJECT_ID})
  -r, --region <region>     GCP region (default: ${REGION})
  -b, --bucket <bucket>     GCS bucket name (default: \${PROJECT_ID}-\${NAME_PREFIX}-tfstate)
  -n, --name-prefix <pfx>   Resource name prefix (default: ${NAME_PREFIX})
  -e, --env <env>           Environment name (default: ${ENVIRONMENT})
      --dry-run             Print commands without running
  -h, --help                Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--project-id)
            PROJECT_ID="$2"
            shift 2
            ;;
        -r|--region)
            REGION="$2"
            shift 2
            ;;
        -b|--bucket)
            TFSTATE_BUCKET="$2"
            shift 2
            ;;
        -n|--name-prefix)
            NAME_PREFIX="$2"
            shift 2
            ;;
        -e|--env)
            ENVIRONMENT="$2"
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        -h|--help)
            print_usage
            exit 0
            ;;
        *)
            echo "${RED}Error: Unknown argument '$1'${NC}" >&2
            print_usage
            exit 1
            ;;
    esac
done

if [ -z "${PROJECT_ID}" ]; then
    echo "${RED}Error: GCP Project ID is required.${NC}" >&2
    exit 1
fi

BUCKET_NAME="${TFSTATE_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-tfstate}"
BUCKET_URI="gs://${BUCKET_NAME}"

echo "${BLUE}${BOLD}===================================================================${NC}"
echo "${BLUE}${BOLD}        Bootstrap Terraform State GCS Bucket: ${BUCKET_URI}         ${NC}"
echo "${BLUE}${BOLD}===================================================================${NC}"

if [ "${DRY_RUN}" = true ]; then
    echo "${YELLOW}[DRY RUN] Would verify/create ${BUCKET_URI} in ${REGION}${NC}"
    exit 0
fi

# Ensure bucket exists
if gcloud storage buckets describe "${BUCKET_URI}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
    echo "State bucket ${BUCKET_URI} already exists."
else
    echo "Creating GCS state bucket ${BUCKET_URI} in ${REGION}..."
    gcloud storage buckets create "${BUCKET_URI}" \
        --project="${PROJECT_ID}" \
        --location="${REGION}" \
        --uniform-bucket-level-access \
        --public-access-prevention
fi

# Enforce security configurations
echo "Applying security configurations and labels..."
gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --uniform-bucket-level-access
gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --public-access-prevention
gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --versioning
gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" \
    --update-labels="app=alert-triage-agent,component=tfstate,env=${ENVIRONMENT},prefix=${NAME_PREFIX}"

# Generate backend.hcl for foundation stack
STACKS=("foundation")
for stack in "${STACKS[@]}"; do
    stack_dir="${REPO_ROOT}/infra/terraform/${stack}"
    if [ -d "${stack_dir}" ]; then
        cat <<EOF > "${stack_dir}/backend.hcl"
bucket = "${BUCKET_NAME}"
prefix = "${NAME_PREFIX}/${stack}"
EOF
        echo "  Created ${stack_dir}/backend.hcl (prefix: ${NAME_PREFIX}/${stack})"
    fi
done

echo "${GREEN}${BOLD}SUCCESS: Terraform state bucket ${BUCKET_URI} configured.${NC}"
