#!/usr/bin/env bash
# ==============================================================================
# Bootstrap Terraform State GCS Bucket & Generate Backend Configuration
#
# Creates or verifies the Google Cloud Storage bucket used for Terraform state
# storage with required enterprise security settings:
#   - Uniform bucket-level access
#   - Public access prevention
#   - Object versioning (for state recovery)
#   - Resource labels (app, component, env, prefix)
#
# Generates backend.hcl for each Terraform stack (agent, bridge, demo).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GDI_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

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
PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-us-central1}}"
NAME_PREFIX="${NAME_PREFIX:-gdi}"
ENVIRONMENT="${ENVIRONMENT:-prod}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"
DRY_RUN=false

print_usage() {
    cat <<EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
  -p, --project-id <id>     GCP Project ID (or set PROJECT_ID / GCP_PROJECT env var)
  -r, --region <region>     GCP region/location (default: us-central1, or REGION env var)
  -b, --bucket <bucket>     GCS bucket name (default: \${PROJECT_ID}-\${NAME_PREFIX}-tfstate)
  -n, --name-prefix <pfx>   Resource name prefix (default: gdi, or NAME_PREFIX env var)
  -e, --env <env>           Environment name (default: prod, or ENVIRONMENT env var)
      --dry-run             Print the commands that would be executed without running them
  -h, --help                Show this help message and exit

${BOLD}Examples:${NC}
  $0 --project-id my-project-id
  $0 --project-id my-project-id --name-prefix acme
  $0 --project-id my-project-id --bucket my-custom-state-bucket --region us-central1
  $0 --project-id my-project-id --dry-run
EOF
}

# Parse CLI arguments
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
    echo "${RED}Error: GCP Project ID is required. Pass --project-id <id> or set PROJECT_ID.${NC}" >&2
    exit 1
fi

BUCKET_NAME="${TFSTATE_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-tfstate}"
BUCKET_URI="gs://${BUCKET_NAME}"

echo "${BLUE}${BOLD}===================================================================${NC}"
echo "${BLUE}${BOLD}          Terraform State GCS Bucket Bootstrap                    ${NC}"
echo "${BLUE}${BOLD}===================================================================${NC}"
echo "Project ID:   ${BOLD}${PROJECT_ID}${NC}"
echo "Region:       ${BOLD}${REGION}${NC}"
echo "Name Prefix:  ${BOLD}${NAME_PREFIX}${NC}"
echo "Environment:  ${BOLD}${ENVIRONMENT}${NC}"
echo "Bucket Name:  ${BOLD}${BUCKET_NAME}${NC}"
echo "Bucket URI:   ${BOLD}${BUCKET_URI}${NC}"
echo "Dry Run:      ${BOLD}${DRY_RUN}${NC}"
echo "${BLUE}-------------------------------------------------------------------${NC}"

STACKS=("agent" "bridge" "demo")

if [ "${DRY_RUN}" = true ]; then
    echo "${YELLOW}[DRY RUN] Plan for bootstrapping GCS state bucket:${NC}"
    echo "1. Check if bucket exists: gcloud storage buckets describe ${BUCKET_URI} --project=${PROJECT_ID}"
    echo "2. If not exists: gcloud storage buckets create ${BUCKET_URI} --project=${PROJECT_ID} --location=${REGION} --uniform-bucket-level-access --public-access-prevention"
    echo "3. Enforce uniform bucket-level access: gcloud storage buckets update ${BUCKET_URI} --project=${PROJECT_ID} --uniform-bucket-level-access"
    echo "4. Enforce public access prevention: gcloud storage buckets update ${BUCKET_URI} --project=${PROJECT_ID} --public-access-prevention"
    echo "5. Enforce object versioning: gcloud storage buckets update ${BUCKET_URI} --project=${PROJECT_ID} --versioning"
    echo "6. Apply common labels: gcloud storage buckets update ${BUCKET_URI} --project=${PROJECT_ID} --update-labels=app=gha-deploy-investigator,component=tfstate,env=${ENVIRONMENT},prefix=${NAME_PREFIX}"
    echo "7. Generate backend configuration files (backend.hcl):"
    for stack in "${STACKS[@]}"; do
        echo "   - Generate ${stack}/infra/terraform/backend.hcl (bucket=${BUCKET_NAME}, prefix=${NAME_PREFIX}/${stack})"
    done
    printf '\n%s\n' "${GREEN}[DRY RUN] Bootstrap simulation completed successfully.${NC}"
    exit 0
fi

# Check for gcloud or gsutil
USE_GCLOUD_STORAGE=true
if command -v gcloud >/dev/null 2>&1; then
    USE_GCLOUD_STORAGE=true
elif command -v gsutil >/dev/null 2>&1; then
    USE_GCLOUD_STORAGE=false
else
    echo "${RED}Error: Neither 'gcloud' nor 'gsutil' was found in PATH.${NC}" >&2
    exit 1
fi

# Check if bucket already exists
BUCKET_EXISTS=false
if [ "${USE_GCLOUD_STORAGE}" = true ]; then
    if gcloud storage buckets describe "${BUCKET_URI}" --project="${PROJECT_ID}" >/dev/null 2>&1; then
        BUCKET_EXISTS=true
    fi
else
    if gsutil ls -b "${BUCKET_URI}" >/dev/null 2>&1; then
        BUCKET_EXISTS=true
    fi
fi

if [ "${BUCKET_EXISTS}" = true ]; then
    echo "Bucket ${BUCKET_URI} already exists. Verifying and applying security settings..."
else
    echo "Creating GCS state bucket ${BUCKET_URI} in ${REGION}..."
    if [ "${USE_GCLOUD_STORAGE}" = true ]; then
        gcloud storage buckets create "${BUCKET_URI}" \
            --project="${PROJECT_ID}" \
            --location="${REGION}" \
            --uniform-bucket-level-access \
            --public-access-prevention
    else
        gsutil mb -p "${PROJECT_ID}" -c standard -l "${REGION}" -b on "${BUCKET_URI}"
    fi
fi

# Apply security settings and labels
echo "Applying security settings and labels to ${BUCKET_URI}..."
if [ "${USE_GCLOUD_STORAGE}" = true ]; then
    # Uniform bucket-level access
    gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --uniform-bucket-level-access
    # Public access prevention
    gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --public-access-prevention
    # Object versioning
    gcloud storage buckets update "${BUCKET_URI}" --project="${PROJECT_ID}" --versioning
    # Labels
    gcloud storage buckets update "${BUCKET_URI}" \
        --project="${PROJECT_ID}" \
        --update-labels="app=gha-deploy-investigator,component=tfstate,env=${ENVIRONMENT},prefix=${NAME_PREFIX}"
else
    # gsutil fallback
    gsutil uniformbucketlevelaccess set on "${BUCKET_URI}"
    gsutil pap set enabled "${BUCKET_URI}"
    gsutil versioning set on "${BUCKET_URI}"
    gsutil label set <(cat <<EOF
{
  "app": "gha-deploy-investigator",
  "component": "tfstate",
  "env": "${ENVIRONMENT}",
  "prefix": "${NAME_PREFIX}"
}
EOF
) "${BUCKET_URI}"
fi

# Generate backend.hcl for each stack
echo "Writing backend configuration files (backend.hcl)..."
for stack in "${STACKS[@]}"; do
    stack_dir="${GDI_ROOT}/${stack}/infra/terraform"
    if [ -d "${stack_dir}" ]; then
        cat <<EOF > "${stack_dir}/backend.hcl"
bucket = "${BUCKET_NAME}"
prefix = "${NAME_PREFIX}/${stack}"
EOF
        echo "  Created ${stack}/infra/terraform/backend.hcl (prefix: ${NAME_PREFIX}/${stack})"
    fi
done

printf '\n%s\n' "${GREEN}${BOLD}SUCCESS: Terraform state bucket ${BUCKET_URI} configured successfully.${NC}"
echo "Stack backend.hcl files generated. You can now run 'terraform init -backend-config=backend.hcl' in each stack."
