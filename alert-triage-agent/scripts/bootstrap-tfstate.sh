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

# Inputs. PROJECT_ID, REGION and NAME_PREFIX identify the target and have no
# defaults: set them in the environment or pass the flags below.
PROJECT_ID="${PROJECT_ID:-}"
REGION="${REGION:-}"
NAME_PREFIX="${NAME_PREFIX:-}"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"
DRY_RUN=false
WRITE_BACKEND_ONLY=false

# Terraform roots that get a backend.hcl (state prefix "<NAME_PREFIX>/<root>").
STACKS="foundation database applications observability triage-iam"

# APIs needed before Terraform can run: the state bucket (storage), project and
# API management (cloudresourcemanager, serviceusage) and IAM. The foundation
# root enables every other API the stack uses.
BOOTSTRAP_APIS="serviceusage.googleapis.com cloudresourcemanager.googleapis.com storage.googleapis.com iam.googleapis.com"

print_usage() {
    cat <<EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

Enables the bootstrap APIs, creates the Terraform state bucket
\${PROJECT_ID}-\${NAME_PREFIX}-tfstate and writes backend.hcl
(prefix "\${NAME_PREFIX}/<root>") for every root under infra/terraform:
${STACKS}

${BOLD}Required${NC} (flag or environment variable, no defaults):
  -p, --project-id <id>     GCP project ID                 (PROJECT_ID)
  -r, --region <region>     GCP region of the stack        (REGION)
  -n, --name-prefix <pfx>   Resource name prefix, e.g. ata (NAME_PREFIX)
                            2-10 chars, [a-z][a-z0-9]*

${BOLD}Options:${NC}
  -b, --bucket <bucket>     State bucket name (default: \${PROJECT_ID}-\${NAME_PREFIX}-tfstate)
  -e, --env <env>           Environment label (default: ${ENVIRONMENT})
  -o, --owner <owner>       Owner label (default: ${OWNER})
      --write-backend-only  Only write the backend.hcl files (no gcloud calls),
                            e.g. to re-init the roots of an existing stack
      --dry-run             Print what would be done without doing it
  -h, --help                Show this help message
EOF
}

need_value() {
    if [ "$#" -lt 2 ] || [ -z "$2" ]; then
        echo "${RED}Error: $1 needs a value.${NC}" >&2
        exit 1
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        -p|--project-id)
            need_value "$@"
            PROJECT_ID="$2"
            shift 2
            ;;
        -r|--region)
            need_value "$@"
            REGION="$2"
            shift 2
            ;;
        -b|--bucket)
            need_value "$@"
            TFSTATE_BUCKET="$2"
            shift 2
            ;;
        -n|--name-prefix)
            need_value "$@"
            NAME_PREFIX="$2"
            shift 2
            ;;
        -e|--env)
            need_value "$@"
            ENVIRONMENT="$2"
            shift 2
            ;;
        -o|--owner)
            need_value "$@"
            OWNER="$2"
            shift 2
            ;;
        --write-backend-only)
            WRITE_BACKEND_ONLY=true
            shift
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

missing=""
[ -n "${PROJECT_ID}" ] || missing="${missing} PROJECT_ID(--project-id)"
[ -n "${REGION}" ] || missing="${missing} REGION(--region)"
[ -n "${NAME_PREFIX}" ] || missing="${missing} NAME_PREFIX(--name-prefix)"
if [ -n "${missing}" ]; then
    echo "${RED}Error: required input(s) not set:${missing}. There are no defaults; set the environment variables or pass the flags (see --help).${NC}" >&2
    exit 1
fi

if ! printf '%s\n' "${NAME_PREFIX}" | grep -Eq '^[a-z][a-z0-9]{1,9}$'; then
    echo "${RED}Error: NAME_PREFIX '${NAME_PREFIX}' is invalid: use 2-10 lowercase letters or digits, starting with a letter.${NC}" >&2
    exit 1
fi

BUCKET_NAME="${TFSTATE_BUCKET:-${PROJECT_ID}-${NAME_PREFIX}-tfstate}"
BUCKET_URI="gs://${BUCKET_NAME}"

write_backend_configs() {
    local stack stack_dir
    for stack in ${STACKS}; do
        stack_dir="${REPO_ROOT}/infra/terraform/${stack}"
        if [ ! -d "${stack_dir}" ]; then
            echo "${RED}Error: Terraform root ${stack_dir} not found.${NC}" >&2
            exit 1
        fi
        if [ "${DRY_RUN}" = true ]; then
            echo "${YELLOW}[DRY RUN] Would write ${stack_dir}/backend.hcl (bucket: ${BUCKET_NAME}, prefix: ${NAME_PREFIX}/${stack})${NC}"
            continue
        fi
        cat <<EOF > "${stack_dir}/backend.hcl"
bucket = "${BUCKET_NAME}"
prefix = "${NAME_PREFIX}/${stack}"
EOF
        echo "  Created ${stack_dir}/backend.hcl (prefix: ${NAME_PREFIX}/${stack})"
    done
}

echo "${BLUE}${BOLD}===================================================================${NC}"
echo "${BLUE}${BOLD}        Bootstrap Terraform State GCS Bucket: ${BUCKET_URI}         ${NC}"
echo "${BLUE}${BOLD}===================================================================${NC}"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Prefix: ${NAME_PREFIX}"

if [ "${WRITE_BACKEND_ONLY}" = true ]; then
    write_backend_configs
    echo "${GREEN}${BOLD}SUCCESS: backend.hcl written for: ${STACKS}${NC}"
    exit 0
fi

if [ "${DRY_RUN}" = true ]; then
    echo "${YELLOW}[DRY RUN] Would enable ${BOOTSTRAP_APIS} in ${PROJECT_ID}${NC}"
    echo "${YELLOW}[DRY RUN] Would verify/create ${BUCKET_URI} in ${REGION}${NC}"
    write_backend_configs
    exit 0
fi

# Enable the APIs needed before Terraform runs (idempotent).
echo "Enabling bootstrap APIs: ${BOOTSTRAP_APIS}"
# shellcheck disable=SC2086 # BOOTSTRAP_APIS is a space-separated list of API names
gcloud services enable ${BOOTSTRAP_APIS} --project="${PROJECT_ID}"

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
    --update-labels="app=alert-triage-agent,component=${NAME_PREFIX}-tools,role=triage-system,tier=platform,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}"

# Generate backend.hcl for every Terraform root
write_backend_configs

echo "${GREEN}${BOLD}SUCCESS: Terraform state bucket ${BUCKET_URI} configured.${NC}"
