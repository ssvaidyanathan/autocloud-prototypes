#!/usr/bin/env bash
# ==============================================================================
# register_tool.sh
#
# Registers or updates the FastMCP calculator service in Google Cloud Agent
# Registry (agentregistry.googleapis.com/v1) as an mcpServers resource.
#
# Usage:
#   ./scripts/register_tool.sh [options] [SERVICE_NAME] [TOOL_SERVICE_URL]
#
# Examples:
#   ./scripts/register_tool.sh calculator https://calculator-tool-service-uc.a.run.app
#   ./scripts/register_tool.sh --dry-run calculator https://calculator-tool-service-uc.a.run.app
#   TOOL_SERVICE_URL=https://... ./scripts/register_tool.sh
# ==============================================================================

set -euo pipefail

# Default configuration per project design
PROJECT_ID="${PROJECT_ID:-demo-project-id}"
REGION="${REGION:-us-central1}"
DISPLAY_NAME="${DISPLAY_NAME:-Precision Calculator Service}"
DESCRIPTION="${DESCRIPTION:-Provides verified arithmetic operations (add, multiply, power)}"
DRY_RUN=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [SERVICE_NAME] [TOOL_SERVICE_URL]

Registers or updates the FastMCP calculator service in Google Cloud Agent Registry.

Arguments:
  SERVICE_NAME       Service ID to register/update (default: calculator)
  TOOL_SERVICE_URL   Base URL of the tool service (e.g. https://calculator-tool-service-uc.a.run.app)

Options:
  --dry-run                    Print gcloud commands without executing them
  --project=ID, --project ID   Target GCP project ID (default: ${PROJECT_ID:-demo-project-id})
  --location=REGION, --location REGION
                               Target GCP region (default: ${REGION:-us-central1})
  --tool-url=URL, --tool-url URL
                               Base URL of the tool service
  --service-name=NAME, --service-name NAME
                               Service ID to register/update (default: calculator)
  --display-name=NAME, --display-name NAME
                               Human-readable service name
  --description=TEXT, --description TEXT
                               Service description
  -h, --help                   Display this help message

Environment Variables:
  PROJECT_ID         Target GCP project ID
  REGION             Target GCP region
  TOOL_SERVICE_URL   Base URL of the deployed tool service
  DISPLAY_NAME       Display name for the service
  DESCRIPTION        Description of the service
EOF
  exit 0
}

# Parse options and arguments
POSITIONAL=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN=true
      shift
      ;;
    --project)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --project requires an argument" >&2
        exit 1
      fi
      PROJECT_ID="$2"
      shift 2
      ;;
    --project=*)
      PROJECT_ID="${1#*=}"
      shift
      ;;
    --location)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --location requires an argument" >&2
        exit 1
      fi
      REGION="$2"
      shift 2
      ;;
    --location=*)
      REGION="${1#*=}"
      shift
      ;;
    --tool-url)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --tool-url requires an argument" >&2
        exit 1
      fi
      TOOL_SERVICE_URL="$2"
      shift 2
      ;;
    --tool-url=*)
      TOOL_SERVICE_URL="${1#*=}"
      shift
      ;;
    --service-name)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --service-name requires an argument" >&2
        exit 1
      fi
      SERVICE_NAME="$2"
      shift 2
      ;;
    --service-name=*)
      SERVICE_NAME="${1#*=}"
      shift
      ;;
    --display-name)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --display-name requires an argument" >&2
        exit 1
      fi
      DISPLAY_NAME="$2"
      shift 2
      ;;
    --display-name=*)
      DISPLAY_NAME="${1#*=}"
      shift
      ;;
    --description)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --description requires an argument" >&2
        exit 1
      fi
      DESCRIPTION="$2"
      shift 2
      ;;
    --description=*)
      DESCRIPTION="${1#*=}"
      shift
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "Error: Unknown option $1" >&2
      usage
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

SERVICE_NAME="${POSITIONAL[0]:-${SERVICE_NAME:-calculator}}"
TOOL_SERVICE_URL="${POSITIONAL[1]:-${TOOL_SERVICE_URL:-}}"

if [[ -z "${TOOL_SERVICE_URL}" ]]; then
  echo "Error: TOOL_SERVICE_URL must be specified via argument or environment variable." >&2
  exit 1
fi

# Strip trailing slash from URL if present
TOOL_SERVICE_URL="${TOOL_SERVICE_URL%/}"
INTERFACE_URL="${TOOL_SERVICE_URL}/mcp"

echo "=========================================================="
echo "Google Cloud Agent Registry - Tool Registration"
echo "=========================================================="
echo "Project ID:       ${PROJECT_ID}"
echo "Region:           ${REGION}"
echo "Service Name:     ${SERVICE_NAME}"
echo "Display Name:     ${DISPLAY_NAME}"
echo "Description:      ${DESCRIPTION}"
echo "Interface URL:    ${INTERFACE_URL}"
echo "Dry Run:          ${DRY_RUN}"
echo "=========================================================="

# Check if service already exists
EXISTS=false
if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[dry-run] Checking service existence skipped in dry-run mode."
else
  echo "Checking if service '${SERVICE_NAME}' already exists..."
  if gcloud agent-registry services describe "${SERVICE_NAME}" \
      --project="${PROJECT_ID}" \
      --location="${REGION}" \
      --format="value(name)" >/dev/null 2>&1; then
    EXISTS=true
    echo "Service '${SERVICE_NAME}' exists. Proceeding with UPDATE..."
  else
    echo "Service '${SERVICE_NAME}' does not exist. Proceeding with CREATE..."
  fi
fi

if [[ "${EXISTS}" == "true" ]]; then
  CMD=(
    gcloud agent-registry services update "${SERVICE_NAME}"
    --project="${PROJECT_ID}"
    --location="${REGION}"
    --display-name="${DISPLAY_NAME}"
    --description="${DESCRIPTION}"
    --interfaces="protocolBinding=jsonrpc,url=${INTERFACE_URL}"
  )
else
  CMD=(
    gcloud agent-registry services create "${SERVICE_NAME}"
    --project="${PROJECT_ID}"
    --location="${REGION}"
    --display-name="${DISPLAY_NAME}"
    --description="${DESCRIPTION}"
    --mcp-server-spec-type=no-spec
    --interfaces="protocolBinding=jsonrpc,url=${INTERFACE_URL}"
  )
fi

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[dry-run] Would execute create command:"
  echo "  gcloud agent-registry services create ${SERVICE_NAME} \\"
  echo "    --project=\"${PROJECT_ID}\" \\"
  echo "    --location=\"${REGION}\" \\"
  echo "    --display-name=\"${DISPLAY_NAME}\" \\"
  echo "    --description=\"${DESCRIPTION}\" \\"
  echo "    --mcp-server-spec-type=no-spec \\"
  echo "    --interfaces=\"protocolBinding=jsonrpc,url=${INTERFACE_URL}\""
  echo ""
  echo "[dry-run] Or update command (if service already exists):"
  echo "  gcloud agent-registry services update ${SERVICE_NAME} \\"
  echo "    --project=\"${PROJECT_ID}\" \\"
  echo "    --location=\"${REGION}\" \\"
  echo "    --display-name=\"${DISPLAY_NAME}\" \\"
  echo "    --description=\"${DESCRIPTION}\" \\"
  echo "    --interfaces=\"protocolBinding=jsonrpc,url=${INTERFACE_URL}\""
  exit 0
fi

echo "Executing command:"
echo "  ${CMD[*]}"
"${CMD[@]}"

echo "Successfully registered/updated '${SERVICE_NAME}' in Agent Registry."
