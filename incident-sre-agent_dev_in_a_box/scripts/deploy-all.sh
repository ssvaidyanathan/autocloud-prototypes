#!/usr/bin/env bash
# ==============================================================================
# Master Deployment Script for Incident SRE Agent
#
# Orchestrates end-to-end deployment across GCP:
#   1. Agent Base Infrastructure (APIs, IAM, Artifact Registry, Cloud Tasks)
#   2. Agent Container Build & Push
#   3. Agent Cloud Run Service (Private ingress, 15m timeout)
#   4. Bridge Base Infrastructure (APIs, IAM, Artifact Registry, Secrets)
#   5. Secret Manager Secrets Setup
#   6. Bridge Container Build & Push
#   7. Bridge Cloud Run Service (Public ingress)
#   8. (Optional) Demo Microservices Stack (Shop API, Inventory Service)
#   9. Summary Table with Webhook Endpoints and Verification Instructions
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"

# Ensure venv is in PATH if available
if [ -d "${ROOT_DIR}/.venv/bin" ]; then
    export PATH="${ROOT_DIR}/.venv/bin:${PATH}"
fi

# Color codes for terminal output (only if interactive TTY)
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

# Default values
PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-}}"
REGION="${REGION:-${GCP_REGION:-us-central1}}"
NAME_PREFIX="${NAME_PREFIX:-isa}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-}"
NO_BACKEND=false
WITH_DEMO=false
SKIP_BUILD=false
DRY_RUN=false
GIT_SHORT_SHA="$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo "latest")"
IMAGE_TAG="${IMAGE_TAG:-${GIT_SHORT_SHA}}"

print_usage() {
    cat <<EOF
${BOLD}Usage:${NC} $0 [OPTIONS]

${BOLD}Options:${NC}
  -p, --project-id <id>     GCP Project ID (or set PROJECT_ID / GCP_PROJECT env var)
  -r, --region <region>     GCP region (default: us-central1, or REGION env var)
  -n, --name-prefix <pfx>   Resource name prefix (default: isa, or NAME_PREFIX env var)
  -b, --tfstate-bucket <bkt> GCS bucket for Terraform state (or TFSTATE_BUCKET env var)
      --no-backend          Disable remote GCS backend (initialize with -backend=false)
  -t, --image-tag <tag>     Image tag for container images (default: git short SHA, or latest)
      --with-demo           Deploy the optional Demo Stack (shop-api & inventory-svc)
      --skip-build          Skip container image build and push steps
      --dry-run             Print the deployment plan without executing changes
  -h, --help                Show this help message and exit

${BOLD}Examples:${NC}
  $0 --project-id my-gcp-project --region us-central1
  $0 --project-id my-gcp-project --name-prefix acme
  $0 --project-id my-gcp-project --tfstate-bucket my-gcp-project-isa-tfstate
  $0 --project-id my-gcp-project --with-demo
  $0 --project-id my-gcp-project --dry-run
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
        -n|--name-prefix)
            NAME_PREFIX="$2"
            shift 2
            ;;
        -b|--tfstate-bucket)
            TFSTATE_BUCKET="$2"
            shift 2
            ;;
        --no-backend)
            NO_BACKEND=true
            shift
            ;;
        -t|--image-tag)
            IMAGE_TAG="$2"
            shift 2
            ;;
        --with-demo)
            WITH_DEMO=true
            shift
            ;;
        --skip-build)
            SKIP_BUILD=true
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

if [ -z "${PROJECT_ID}" ]; then
    echo "${RED}Error: GCP Project ID is required. Pass --project-id <id> or set PROJECT_ID.${NC}" >&2
    exit 1
fi

echo "${BLUE}${BOLD}===================================================================${NC}"
echo "${BLUE}${BOLD}        Incident SRE Agent — Master Deployment Orchestrator        ${NC}"
echo "${BLUE}${BOLD}===================================================================${NC}"
echo "Project ID:   ${BOLD}${PROJECT_ID}${NC}"
echo "Region:       ${BOLD}${REGION}${NC}"
echo "Name Prefix:  ${BOLD}${NAME_PREFIX}${NC}"
echo "TF State Bkt: ${BOLD}${TFSTATE_BUCKET:-<none>}${NC}"
echo "Image Tag:    ${BOLD}${IMAGE_TAG}${NC}"
echo "Deploy Demo:  ${BOLD}${WITH_DEMO}${NC}"
echo "Skip Build:   ${BOLD}${SKIP_BUILD}${NC}"
echo "Dry Run:      ${BOLD}${DRY_RUN}${NC}"
echo "${BLUE}-------------------------------------------------------------------${NC}"

run_step() {
    local step_title="$1"
    shift
    echo -e "\n${GREEN}${BOLD}>>> Step: ${step_title}${NC}"
    if [ "${DRY_RUN}" = true ]; then
        echo "${YELLOW}[DRY RUN] Would run: $*${NC}"
        return 0
    fi
    "$@"
}

init_tf_stack() {
    local stack_dir="$1"
    local component="$2"
    local component_slug="${component,,}"

    if [ "${NO_BACKEND}" = true ]; then
        run_step "Initialize ${component} Terraform (no backend)" \
            terraform -chdir="${stack_dir}" init -backend=false
    elif [ -f "${stack_dir}/backend.hcl" ]; then
        run_step "Initialize ${component} Terraform (existing backend.hcl)" \
            terraform -chdir="${stack_dir}" init -backend-config=backend.hcl
    elif [ -n "${TFSTATE_BUCKET}" ]; then
        if [ "${DRY_RUN}" = true ]; then
            echo "${YELLOW}[DRY RUN] Would generate ${stack_dir}/backend.hcl with bucket=${TFSTATE_BUCKET}, prefix=${NAME_PREFIX}/${component_slug}${NC}"
        else
            cat > "${stack_dir}/backend.hcl" <<EOF
bucket = "${TFSTATE_BUCKET}"
prefix = "${NAME_PREFIX}/${component_slug}"
EOF
        fi
        run_step "Initialize ${component} Terraform (GCS backend: ${TFSTATE_BUCKET})" \
            terraform -chdir="${stack_dir}" init -backend-config=backend.hcl
    else
        run_step "Initialize ${component} Terraform (backend=false)" \
            terraform -chdir="${stack_dir}" init -backend=false
    fi
}

# -----------------------------------------------------------------------------
# 1. Check Required Tools
# -----------------------------------------------------------------------------
echo -e "\n${BOLD}Checking prerequisites...${NC}"

command -v terraform >/dev/null 2>&1 || {
    echo "${RED}Error: 'terraform' is required but not installed or not in PATH.${NC}" >&2
    exit 1
}

if [ "${DRY_RUN}" = false ]; then
    command -v gcloud >/dev/null 2>&1 || {
        echo "${RED}Error: 'gcloud' is required but not installed or not in PATH.${NC}" >&2
        exit 1
    }
else
    if ! command -v gcloud >/dev/null 2>&1; then
        echo "${YELLOW}[DRY RUN] Warning: 'gcloud' not found in PATH.${NC}"
    fi
fi

if [ "${SKIP_BUILD}" = false ]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo "${YELLOW}Notice: 'docker' not found; builds will be submitted via Google Cloud Build.${NC}"
    fi
fi

echo "${GREEN}Prerequisites verified successfully.${NC}"

# -----------------------------------------------------------------------------
# 2. Agent Infrastructure Deployment
# -----------------------------------------------------------------------------
AGENT_TF_DIR="${ROOT_DIR}/agent/infra/terraform"

init_tf_stack "${AGENT_TF_DIR}" "Agent"

run_step "Deploy Agent Base Infrastructure (APIs, IAM, Artifact Registry, Tasks Queue)" \
    terraform -chdir="${AGENT_TF_DIR}" apply -auto-approve \
        -var="project_id=${PROJECT_ID}" \
        -var="region=${REGION}" \
        -var="name_prefix=${NAME_PREFIX}" \
        -var="image_tag=${IMAGE_TAG}" \
        -target=module.apis \
        -target=module.iam \
        -target=module.artifact_registry \
        -target=module.tasks_queue

# -----------------------------------------------------------------------------
# 3. Build & Push Agent Container Image
# -----------------------------------------------------------------------------
if [ "${SKIP_BUILD}" = false ]; then
    run_step "Build & Push SRE Agent Container Image" \
        env PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" NAME_PREFIX="${NAME_PREFIX}" IMAGE_TAG="${IMAGE_TAG}" \
        bash "${ROOT_DIR}/agent/scripts/build-push.sh"
fi

# -----------------------------------------------------------------------------
# 4. Deploy Agent Cloud Run Service
# -----------------------------------------------------------------------------
run_step "Deploy SRE Agent Cloud Run Service" \
    terraform -chdir="${AGENT_TF_DIR}" apply -auto-approve \
        -var="project_id=${PROJECT_ID}" \
        -var="region=${REGION}" \
        -var="name_prefix=${NAME_PREFIX}" \
        -var="image_tag=${IMAGE_TAG}"

# Extract Agent Outputs
if [ "${DRY_RUN}" = false ]; then
    AGENT_SERVICE_URL="$(terraform -chdir="${AGENT_TF_DIR}" output -raw agent_service_url)"
    AGENT_SA_EMAIL="$(terraform -chdir="${AGENT_TF_DIR}" output -raw agent_service_account_email)"
    TASKS_INVOKER_SA_EMAIL="$(terraform -chdir="${AGENT_TF_DIR}" output -raw tasks_invoker_service_account_email)"
    TASKS_QUEUE_NAME="$(terraform -chdir="${AGENT_TF_DIR}" output -raw tasks_queue_name)"
else
    AGENT_SERVICE_URL="https://${NAME_PREFIX}-sre-agent-mock-uc.a.run.app"
    AGENT_SA_EMAIL="${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com"
    TASKS_INVOKER_SA_EMAIL="${NAME_PREFIX}-tasks-invoker-sa@${PROJECT_ID}.iam.gserviceaccount.com"
    TASKS_QUEUE_NAME="${NAME_PREFIX}-investigations"
fi

echo -e "\n${BOLD}Agent Outputs Captured:${NC}"
echo "  Agent URL:               ${AGENT_SERVICE_URL}"
echo "  Agent Service Account:   ${AGENT_SA_EMAIL}"
echo "  Tasks Invoker Account:   ${TASKS_INVOKER_SA_EMAIL}"
echo "  Tasks Queue Name:        ${TASKS_QUEUE_NAME}"

# -----------------------------------------------------------------------------
# 5. Bridge Infrastructure Deployment
# -----------------------------------------------------------------------------
BRIDGE_TF_DIR="${ROOT_DIR}/bridge/infra/terraform"

init_tf_stack "${BRIDGE_TF_DIR}" "Bridge"

run_step "Deploy Bridge Base Infrastructure (APIs, IAM, Artifact Registry, Secrets)" \
    terraform -chdir="${BRIDGE_TF_DIR}" apply -auto-approve \
        -var="project_id=${PROJECT_ID}" \
        -var="region=${REGION}" \
        -var="name_prefix=${NAME_PREFIX}" \
        -var="image_tag=${IMAGE_TAG}" \
        -var="tasks_queue_name=${TASKS_QUEUE_NAME}" \
        -var="tasks_invoker_service_account_email=${TASKS_INVOKER_SA_EMAIL}" \
        -var="agent_service_url=${AGENT_SERVICE_URL}" \
        -var="agent_service_account_email=${AGENT_SA_EMAIL}" \
        -target=module.apis \
        -target=module.iam \
        -target=module.artifact_registry \
        -target=module.secrets

# -----------------------------------------------------------------------------
# 6. Populate Secret Manager Secrets
# -----------------------------------------------------------------------------
if [ "${DRY_RUN}" = false ]; then
    echo -e "\n${GREEN}${BOLD}>>> Step: Configure Bridge Secrets in Secret Manager${NC}"
    env PROJECT_ID="${PROJECT_ID}" NAME_PREFIX="${NAME_PREFIX}" bash "${ROOT_DIR}/bridge/scripts/set-secrets.sh"
else
    echo -e "\n${YELLOW}[DRY RUN] Would execute: ${ROOT_DIR}/bridge/scripts/set-secrets.sh${NC}"
fi

# -----------------------------------------------------------------------------
# 7. Build & Push Bridge Container Image
# -----------------------------------------------------------------------------
if [ "${SKIP_BUILD}" = false ]; then
    run_step "Build & Push Incident Bridge Container Image" \
        env PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" NAME_PREFIX="${NAME_PREFIX}" IMAGE_TAG="${IMAGE_TAG}" \
        bash "${ROOT_DIR}/bridge/scripts/build-push.sh"
fi

# -----------------------------------------------------------------------------
# 8. Deploy Bridge Cloud Run Service
# -----------------------------------------------------------------------------
run_step "Deploy Incident Bridge Cloud Run Service" \
    terraform -chdir="${BRIDGE_TF_DIR}" apply -auto-approve \
        -var="project_id=${PROJECT_ID}" \
        -var="region=${REGION}" \
        -var="name_prefix=${NAME_PREFIX}" \
        -var="image_tag=${IMAGE_TAG}" \
        -var="tasks_queue_name=${TASKS_QUEUE_NAME}" \
        -var="tasks_invoker_service_account_email=${TASKS_INVOKER_SA_EMAIL}" \
        -var="agent_service_url=${AGENT_SERVICE_URL}" \
        -var="agent_service_account_email=${AGENT_SA_EMAIL}"

# Extract Bridge Outputs
if [ "${DRY_RUN}" = false ]; then
    BRIDGE_SERVICE_URL="$(terraform -chdir="${BRIDGE_TF_DIR}" output -raw bridge_service_url)"
    BRIDGE_SA_EMAIL="$(terraform -chdir="${BRIDGE_TF_DIR}" output -raw bridge_service_account_email)"
else
    BRIDGE_SERVICE_URL="https://${NAME_PREFIX}-incident-bridge-mock-uc.a.run.app"
    BRIDGE_SA_EMAIL="${NAME_PREFIX}-bridge-sa@${PROJECT_ID}.iam.gserviceaccount.com"
fi

# -----------------------------------------------------------------------------
# 9. (Optional) Deploy Demo Infrastructure
# -----------------------------------------------------------------------------
SHOP_API_URL="N/A"
INVENTORY_SVC_URL="N/A"

if [ "${WITH_DEMO}" = true ]; then
    DEMO_TF_DIR="${ROOT_DIR}/demo/infra/terraform"

    SHOP_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-incident-demo/${NAME_PREFIX}-shop-api:${IMAGE_TAG}"
    INV_IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-incident-demo/${NAME_PREFIX}-inventory-svc:${IMAGE_TAG}"

    if [ "${SKIP_BUILD}" = false ]; then
        run_step "Create ${NAME_PREFIX}-incident-demo Artifact Registry (if needed)" \
            gcloud artifacts repositories create "${NAME_PREFIX}-incident-demo" \
                --repository-format=docker \
                --location="${REGION}" \
                --project="${PROJECT_ID}" \
                --description="Incident SRE Agent Demo Images" || true

        if command -v docker >/dev/null 2>&1; then
            run_step "Build & Push demo/shop-api" \
                bash -c "docker build -t '${SHOP_IMAGE}' '${ROOT_DIR}/demo/shop-api' && docker push '${SHOP_IMAGE}'"
            run_step "Build & Push demo/inventory-svc" \
                bash -c "docker build -t '${INV_IMAGE}' '${ROOT_DIR}/demo/inventory-svc' && docker push '${INV_IMAGE}'"
        elif command -v gcloud >/dev/null 2>&1; then
            run_step "Build & Push demo/shop-api via Cloud Build" \
                gcloud builds submit "${ROOT_DIR}/demo/shop-api" --tag="${SHOP_IMAGE}" --project="${PROJECT_ID}"
            run_step "Build & Push demo/inventory-svc via Cloud Build" \
                gcloud builds submit "${ROOT_DIR}/demo/inventory-svc" --tag="${INV_IMAGE}" --project="${PROJECT_ID}"
        fi
    fi

    init_tf_stack "${DEMO_TF_DIR}" "Demo"

    run_step "Deploy Demo Infrastructure" \
        terraform -chdir="${DEMO_TF_DIR}" apply -auto-approve \
            -var="project_id=${PROJECT_ID}" \
            -var="region=${REGION}" \
            -var="name_prefix=${NAME_PREFIX}" \
            -var="image_tag=${IMAGE_TAG}" \
            -var="shop_api_image=${SHOP_IMAGE}" \
            -var="inventory_svc_image=${INV_IMAGE}"

    if [ "${DRY_RUN}" = false ]; then
        SHOP_API_URL="$(terraform -chdir="${DEMO_TF_DIR}" output -raw shop_api_url)"
        INVENTORY_SVC_URL="$(terraform -chdir="${DEMO_TF_DIR}" output -raw inventory_svc_url)"
    else
        SHOP_API_URL="https://${NAME_PREFIX}-shop-api-mock-uc.a.run.app"
        INVENTORY_SVC_URL="https://${NAME_PREFIX}-inventory-svc-mock-uc.a.run.app"
    fi
fi

# -----------------------------------------------------------------------------
# 10. Deployment Summary Table & Next Steps
# -----------------------------------------------------------------------------
echo -e "\n${BLUE}${BOLD}===================================================================${NC}"
echo "${GREEN}${BOLD}                  DEPLOYMENT COMPLETED SUCCESSFULLY                 ${NC}"
echo "${BLUE}${BOLD}===================================================================${NC}"
cat <<EOF

${BOLD}Core Services:${NC}
  • Incident Bridge (Public):     ${BRIDGE_SERVICE_URL}
  • SRE Agent (Internal):         ${AGENT_SERVICE_URL}
  • Cloud Tasks Queue:            projects/${PROJECT_ID}/locations/${REGION}/queues/${TASKS_QUEUE_NAME}

${BOLD}Service Accounts:${NC}
  • Incident Bridge SA:           ${BRIDGE_SA_EMAIL}
  • SRE Agent SA:                 ${AGENT_SA_EMAIL}
  • Tasks Invoker SA:             ${TASKS_INVOKER_SA_EMAIL}

${BOLD}Webhook Ingestion Endpoints:${NC}
  • ServiceNow Webhook:           ${BRIDGE_SERVICE_URL}/webhook/servicenow
  • Jira Cloud Webhook:           ${BRIDGE_SERVICE_URL}/webhook/jira
  • Generic Webhook:              ${BRIDGE_SERVICE_URL}/webhook/generic
  • Bridge Health Check:          ${BRIDGE_SERVICE_URL}/health

EOF

if [ "${WITH_DEMO}" = true ]; then
cat <<EOF
${BOLD}Demo Environment:${NC}
  • Shop API (Public):            ${SHOP_API_URL}
  • Inventory Service (Internal):  ${INVENTORY_SVC_URL}

EOF
fi

cat <<EOF
${BOLD}Next Steps for Verification:${NC}
  1. Test Bridge Health:
     curl -i "${BRIDGE_SERVICE_URL}/health"

  2. Configure Ticketing System Webhooks:
     • ServiceNow: Follow instructions in bridge/integrations/servicenow/SETUP.md
     • Jira Cloud: Follow instructions in bridge/integrations/jira/SETUP.md

  3. (Optional) Run Demo Incident Simulation:
     python3 demo/scripts/create_incident.py \\
       --target generic \\
       --bridge-url "${BRIDGE_SERVICE_URL}" \\
       --scenario 1

${BLUE}${BOLD}===================================================================${NC}
EOF
