#!/usr/bin/env bash
# ==============================================================================
# Build a service directory into the stack's Artifact Registry and print the image by digest.
#
# Usage: scripts/build-image.sh [--yes] <service-dir> <image-name>
#   e.g. scripts/build-image.sh demo/ata-orders-api "${NAME_PREFIX}-orders-api"
#
# Required: PROJECT_ID, REGION, NAME_PREFIX. Optional: IMAGE_TAG (default: the git commit,
# "-dirty-<UTC time>" appended for uncommitted changes; "t<UTC time>" outside git).
#
# Builds with Cloud Build as the stack's build SA (${NAME_PREFIX}-build-sa) with sources and
# logs in gs://${PROJECT_ID}-${NAME_PREFIX}-build, into
#   ${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/<image-name>:<tag>
# then reads the pushed digest from Artifact Registry. Progress goes to stderr; the only
# stdout line is machine-readable:
#   IMAGE=${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker/<image-name>@sha256:<hex>
# Exits non-zero on any failure (no IMAGE= line then).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/cloud-run-deploy-lib.sh
source "${SCRIPT_DIR}/cloud-run-deploy-lib.sh"

usage() { sed -n '2,19p' "$0"; }

POSITIONAL=()
for arg in "$@"; do
    case "${arg}" in
        --yes|-y) ;;  # accepted (deploy-stage1.sh passes it); this script never prompts
        -h|--help) usage; exit 0 ;;
        -*) echo "Unknown option: ${arg}" >&2; usage >&2; exit 1 ;;
        *) POSITIONAL=(${POSITIONAL[@]+"${POSITIONAL[@]}"} "${arg}") ;;
    esac
done
if [ "${#POSITIONAL[@]}" -ne 2 ]; then
    usage >&2
    exit 1
fi
SERVICE_DIR="${POSITIONAL[0]}"
IMAGE_NAME="${POSITIONAL[1]}"

require_vars PROJECT_ID REGION || exit 1
require_name_prefix || exit 1
if [ ! -d "${SERVICE_DIR}" ]; then
    echo "ERROR: service directory '${SERVICE_DIR}' not found." >&2
    exit 1
fi
SERVICE_DIR="$(cd "${SERVICE_DIR}" && pwd)"

echo "Build ${SERVICE_DIR} -> $(artifact_repo)/${IMAGE_NAME} (project ${PROJECT_ID}, build SA ${NAME_PREFIX}-build-sa)" >&2
confirm_or_abort || exit 1

IMAGE="$(build_image "${SERVICE_DIR}" "${IMAGE_NAME}")"
printf 'IMAGE=%s\n' "${IMAGE}"
