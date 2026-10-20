#!/usr/bin/env bash
# ==============================================================================
# publish_skill.sh
#
# Packages a skill directory into a compliant ZIP bundle and publishes it
# to Google Cloud Agent Registry (agentregistry.googleapis.com/v1alpha) as
# an immutable revision snapshot.
#
# Usage:
#   ./scripts/publish_skill.sh [options] [SKILL_NAME] [REVISION_ID] [SOURCE_DIR]
#
# Examples:
#   ./scripts/publish_skill.sh calculator r1 demo/registry_packages/v1/calculator
#   ./scripts/publish_skill.sh --dry-run calculator r2 demo/registry_packages/v2/calculator
#   SKILL_NAME=calculator REVISION_ID=r1 SOURCE_DIR=... ./scripts/publish_skill.sh
# ==============================================================================

set -euo pipefail

# Default configuration per project design
PROJECT_ID="${PROJECT_ID:-cloud-assist-fde-7}"
REGION="${REGION:-us-central1}"
SKILL_NAME="${SKILL_NAME:-calculator}"
REVISION_ID="${REVISION_ID:-r1}"
SOURCE_DIR="${SOURCE_DIR:-}"
SET_DEFAULT=true
DRY_RUN=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [options] [SKILL_NAME] [REVISION_ID] [SOURCE_DIR]

Packages and publishes a skill revision to Google Cloud Agent Registry.

Arguments:
  SKILL_NAME       Name of the skill in Agent Registry (default: calculator)
  REVISION_ID      Revision identifier to create (e.g. r1, r2)
  SOURCE_DIR       Path to directory containing SKILL.md and optional references/

Options:
  --dry-run                    Print packaging and gcloud commands without executing
  --project=ID, --project ID   Target GCP project ID (default: ${PROJECT_ID})
  --location=REGION, --location REGION
                               Target GCP region (default: ${REGION})
  --skill-name=NAME, --skill-name NAME
                               Target skill identifier (default: ${SKILL_NAME})
  --revision-id=ID, --revision-id ID
                               Revision identifier (default: ${REVISION_ID})
  --source-dir=DIR, --source-dir DIR
                               Source directory to package
  --set-default                Update skill defaultRevision to this new revision (default: true)
  --no-set-default             Do not update skill defaultRevision
  -h, --help                   Display this help message

Environment Variables:
  PROJECT_ID       Target GCP project ID
  REGION           Target GCP region
  SKILL_NAME       Skill identifier
  REVISION_ID      Revision identifier
  SOURCE_DIR       Path to skill source directory
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
    --skill-name)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --skill-name requires an argument" >&2
        exit 1
      fi
      SKILL_NAME="$2"
      shift 2
      ;;
    --skill-name=*)
      SKILL_NAME="${1#*=}"
      shift
      ;;
    --revision-id)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --revision-id requires an argument" >&2
        exit 1
      fi
      REVISION_ID="$2"
      shift 2
      ;;
    --revision-id=*)
      REVISION_ID="${1#*=}"
      shift
      ;;
    --source-dir)
      if [[ $# -lt 2 || "$2" == -* ]]; then
        echo "Error: --source-dir requires an argument" >&2
        exit 1
      fi
      SOURCE_DIR="$2"
      shift 2
      ;;
    --source-dir=*)
      SOURCE_DIR="${1#*=}"
      shift
      ;;
    --set-default)
      SET_DEFAULT=true
      shift
      ;;
    --no-set-default)
      SET_DEFAULT=false
      shift
      ;;
    -h|--help)
      usage
      ;;
    -*)
      echo "Error: Unknown option '$1'" >&2
      usage
      ;;
    *)
      POSITIONAL+=("$1")
      shift
      ;;
  esac
done

if [[ ${#POSITIONAL[@]} -gt 0 ]]; then
  SKILL_NAME="${POSITIONAL[0]}"
fi
if [[ ${#POSITIONAL[@]} -gt 1 ]]; then
  REVISION_ID="${POSITIONAL[1]}"
fi
if [[ ${#POSITIONAL[@]} -gt 2 ]]; then
  SOURCE_DIR="${POSITIONAL[2]}"
fi

# Fallback default source dir if unspecified
if [[ -z "${SOURCE_DIR}" ]]; then
  if [[ -d "demo/registry_packages/v1/${SKILL_NAME}" && "${REVISION_ID}" == "r1" ]]; then
    SOURCE_DIR="demo/registry_packages/v1/${SKILL_NAME}"
  elif [[ -d "demo/registry_packages/v2/${SKILL_NAME}" && "${REVISION_ID}" == "r2" ]]; then
    SOURCE_DIR="demo/registry_packages/v2/${SKILL_NAME}"
  elif [[ -d "demo/registry_packages/${REVISION_ID}/${SKILL_NAME}" ]]; then
    SOURCE_DIR="demo/registry_packages/${REVISION_ID}/${SKILL_NAME}"
  else
    echo "Error: SOURCE_DIR must be specified via argument, flag, or environment variable." >&2
    exit 1
  fi
fi

# Resolve absolute path to SOURCE_DIR
if [[ ! -d "${SOURCE_DIR}" ]]; then
  echo "Error: Skill source directory '${SOURCE_DIR}' does not exist or is not a directory." >&2
  exit 1
fi

# ==============================================================================
# Security & Package Validation Guardrails
# ==============================================================================

# 1. Enforce prohibition of executable scripts/ directory
if [[ -d "${SOURCE_DIR}/scripts" ]]; then
  echo "Error: Security violation: skill directory '${SOURCE_DIR}' contains a forbidden 'scripts/' directory. Code execution inside skills is prohibited." >&2
  exit 1
fi

# 2. Check for required SKILL.md
if [[ ! -f "${SOURCE_DIR}/SKILL.md" ]]; then
  echo "Error: Required specification file '${SOURCE_DIR}/SKILL.md' not found." >&2
  exit 1
fi

echo "=========================================================="
echo "Google Cloud Agent Registry - Skill Revision Publication"
echo "=========================================================="
echo "Project ID:       ${PROJECT_ID}"
echo "Region:           ${REGION}"
echo "Skill Name:       ${SKILL_NAME}"
echo "Revision ID:      ${REVISION_ID}"
echo "Source Directory: ${SOURCE_DIR}"
echo "Set Default:      ${SET_DEFAULT}"
echo "Dry Run:          ${DRY_RUN}"
echo "=========================================================="

REVISION_RESOURCE="projects/${PROJECT_ID}/locations/${REGION}/skills/${SKILL_NAME}/revisions/${REVISION_ID}"

if [[ "${DRY_RUN}" == "true" ]]; then
  echo "[dry-run] Validation passed: zero forbidden 'scripts/' directories detected."
  echo "[dry-run] Would package '${SOURCE_DIR}' into ZIP archive in-memory/tempfile."
  echo ""
  echo "[dry-run] Would execute revision creation command:"
  echo "  gcloud alpha agent-registry skills revisions create ${REVISION_ID} \\"
  echo "    --skill=\"${SKILL_NAME}\" \\"
  echo "    --project=\"${PROJECT_ID}\" \\"
  echo "    --location=\"${REGION}\" \\"
  echo "    --payload=\"/tmp/skill_${SKILL_NAME}_${REVISION_ID}.zip\""
  echo ""
  if [[ "${SET_DEFAULT}" == "true" ]]; then
    echo "[dry-run] Would execute default revision promotion command:"
    echo "  gcloud alpha agent-registry skills update ${SKILL_NAME} \\"
    echo "    --project=\"${PROJECT_ID}\" \\"
    echo "    --location=\"${REGION}\" \\"
    echo "    --default-revision=\"${REVISION_RESOURCE}\""
  fi
  exit 0
fi

# Create temporary zip archive
TEMP_ZIP="$(mktemp -t "skill_${SKILL_NAME}_${REVISION_ID}_XXXXXX.zip")"
trap 'rm -f "${TEMP_ZIP}"' EXIT

echo "Packaging skill directory '${SOURCE_DIR}' into '${TEMP_ZIP}'..."
python3 -c "
import os, zipfile, sys

source_dir = sys.argv[1]
output_zip = sys.argv[2]

with zipfile.ZipFile(output_zip, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, dirs, files in os.walk(source_dir):
        # Security sanity check: never package scripts directory
        if 'scripts' in dirs:
            print('Security violation: found scripts directory during packaging', file=sys.stderr)
            sys.exit(1)
        for file in files:
            full_path = os.path.join(root, file)
            rel_path = os.path.relpath(full_path, source_dir)
            zf.write(full_path, rel_path)
            print(f'  Added: {rel_path}')
" "${SOURCE_DIR}" "${TEMP_ZIP}"

echo "Skill archive packaged successfully ($(wc -c < "${TEMP_ZIP}") bytes)."

echo "Publishing revision '${REVISION_ID}' to Agent Registry..."
gcloud alpha agent-registry skills revisions create "${REVISION_ID}" \
  --skill="${SKILL_NAME}" \
  --project="${PROJECT_ID}" \
  --location="${REGION}" \
  --payload="${TEMP_ZIP}"

if [[ "${SET_DEFAULT}" == "true" ]]; then
  echo "Promoting '${REVISION_ID}' to default revision for skill '${SKILL_NAME}'..."
  gcloud alpha agent-registry skills update "${SKILL_NAME}" \
    --project="${PROJECT_ID}" \
    --location="${REGION}" \
    --default-revision="${REVISION_RESOURCE}"
fi

echo "Successfully published and updated skill revision '${REVISION_ID}' in Agent Registry."
