#!/usr/bin/env bash
# ==============================================================================
# Build and Deploy <prefix>-inventory-api to Cloud Run (initial / stage-1 deploy)
#
# Image (BUILD_IMAGE=auto|true|false, default auto):
#   auto   The stack has a pin (scripts/scenarios/inventory-pin.${NAME_PREFIX}.env) whose
#          digest is in the stack's Artifact Registry: deploy that pinned image. The pinned
#          digest is not there, or the stack has no pin yet (fresh stack): build first.
#   true   Always build.
#   false  Never build: deploy the pinned image; fails if there is no pin or the pinned
#          digest is not in the registry.
# Builds run on Cloud Build as the stack's build SA (${NAME_PREFIX}-build-sa, bucket
# gs://${PROJECT_ID}-${NAME_PREFIX}-build), tag PINNED_INVENTORY_TAG (default v1.1.0).
# The deployed image is printed as a machine-readable line:
#   IMAGE=<region>-docker.pkg.dev/<project>/<prefix>-docker/<prefix>-inventory-api@sha256:<hex>
# followed by the revision and the pin command when the deployed image is not the pin.
#
# `gcloud run deploy` routes 100% of traffic to the new revision. To move the demo baseline
# of a stack that is already pinned to new code, do NOT use this script: use
# scripts/repin-inventory.sh (no-traffic candidate, verify, then switch; DEPLOY.md,
# "Re-pinning the inventory baseline").
#
# Env and limits come from scripts/scenarios/common.sh, so this script, Terraform and the
# scenario restore agree: ATA_INVENTORY_* only (no DB_USER/MOCK_DB aliases), at most
# DEMO_MAX_INSTANCES instances (service level and revision level), min instances 0.
# Required: PROJECT_ID, REGION, NAME_PREFIX (no defaults).
# ==============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEMO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
REPO_ROOT="$(cd "${DEMO_DIR}/../.." && pwd)"

# PROJECT_ID, REGION, NAME_PREFIX, the pin (optional here: a fresh stack has none yet),
# DEMO_MAX_INSTANCES, INVENTORY_BASELINE_ENV_VARS, build_inventory_image,
# inventory_image_exists. Fails loudly on missing env, an invalid pin file or cap.
INVENTORY_PIN_OPTIONAL=true
# shellcheck source=scripts/scenarios/common.sh
source "${REPO_ROOT}/scripts/scenarios/common.sh"

SERVICE_NAME="${SVC_INVENTORY}"
PINNED_TAG="${PINNED_INVENTORY_TAG:-v1.1.0}"
IMAGE_BASE="${PINNED_INVENTORY_IMAGE_BASE}"
ENVIRONMENT="${ENVIRONMENT:-demo}"
OWNER="${OWNER:-sre-prototypes}"
BUILD_IMAGE="${BUILD_IMAGE:-auto}"
case "${BUILD_IMAGE}" in
    auto|true|false) ;;
    *) log_error "BUILD_IMAGE '${BUILD_IMAGE}' must be auto, true or false." >&2; exit 1 ;;
esac

echo "==================================================================="
echo "  Deploying ${SERVICE_NAME} to Cloud Run"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Stack: ${NAME_PREFIX}"
if [ "${INVENTORY_PIN_PRESENT}" = "true" ]; then
    echo "  Pin: ${PINNED_INVENTORY_REVISION} (${PINNED_INVENTORY_DIGEST}) from ${INVENTORY_PIN_FILE}"
else
    echo "  Pin: none (${INVENTORY_PIN_FILE} does not exist: fresh stack)"
fi
echo "  Max instances: ${DEMO_MAX_INSTANCES} (service and revision) | Min instances: 0"
echo "==================================================================="

# 1. Decide: deploy the pinned image, or build.
DO_BUILD=false
if [ -z "${PINNED_INVENTORY_DIGEST}" ]; then
    if [ "${BUILD_IMAGE}" = "false" ]; then
        log_error "BUILD_IMAGE=false, but stack '${NAME_PREFIX}' has no pinned image (${INVENTORY_PIN_FILE} does not exist). Run with BUILD_IMAGE=auto (or true) to build one." >&2
        exit 1
    fi
    log_info "No pinned image for stack '${NAME_PREFIX}': building one."
    DO_BUILD=true
elif [ "${BUILD_IMAGE}" = "true" ]; then
    log_info "BUILD_IMAGE=true: building."
    DO_BUILD=true
else
    rc=0
    inventory_image_exists "${PINNED_INVENTORY_DIGEST}" || rc=$?
    case "${rc}" in
        0)
            log_info "Pinned image ${IMAGE_BASE}@${PINNED_INVENTORY_DIGEST} is in Artifact Registry."
            ;;
        1)
            if [ "${BUILD_IMAGE}" = "false" ]; then
                log_error "BUILD_IMAGE=false, but the pinned image ${IMAGE_BASE}@${PINNED_INVENTORY_DIGEST} is not in this stack's Artifact Registry. Run with BUILD_IMAGE=auto (or true) to build." >&2
                exit 1
            fi
            log_warn "The pinned digest ${PINNED_INVENTORY_DIGEST} is not in ${IMAGE_BASE} (pin of another project, or deleted): building." >&2
            DO_BUILD=true
            ;;
        *)
            log_error "Could not check whether ${IMAGE_BASE}@${PINNED_INVENTORY_DIGEST} exists (gcloud error above); nothing deployed." >&2
            exit 1
            ;;
    esac
fi

DEPLOY_DIGEST="${PINNED_INVENTORY_DIGEST}"
if [ "${DO_BUILD}" = "true" ]; then
    DEPLOY_DIGEST="$(build_inventory_image "${PINNED_TAG}")" || exit 1
fi
DEPLOY_IMAGE="${IMAGE_BASE}@${DEPLOY_DIGEST}"

# 2. Deploy to Cloud Run by digest.
# --set-env-vars on purpose: the env is replaced by the COMPLETE inventory env,
# INVENTORY_BASELINE_ENV_VARS (common.sh: the five ATA_INVENTORY_* variables). The test
# test_applications_terraform_drift.py keeps that set equal to Terraform's inventory env.
# Replacing (not --update-env-vars) removes stale variables such as the legacy DB_USER /
# MOCK_DB aliases. A new inventory variable goes into INVENTORY_BASELINE_ENV_VARS and main.tf.
echo "Deploying Cloud Run service ${SERVICE_NAME} with ${DEPLOY_IMAGE}..."
gcloud run deploy "${SERVICE_NAME}" \
    --project="${PROJECT_ID}" \
    --region="${REGION}" \
    --image="${DEPLOY_IMAGE}" \
    --service-account="${INVENTORY_SA_EMAIL}" \
    --labels="app=alert-triage-agent,stack=${NAME_PREFIX},component=${SVC_INVENTORY},role=demo-workload,tier=service,env=${ENVIRONMENT},managed-by=script,owner=${OWNER}" \
    --set-env-vars="${INVENTORY_BASELINE_ENV_VARS}" \
    --max="${DEMO_MAX_INSTANCES}" \
    --max-instances="${DEMO_MAX_INSTANCES}" \
    --min-instances=0 \
    --no-allow-unauthenticated \
    --quiet

SERVICE_URL="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
REVISION="$(gcloud run services describe "${SERVICE_NAME}" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.latestReadyRevisionName)")"
echo "Successfully deployed ${SERVICE_NAME}: ${SERVICE_URL} (revision ${REVISION:-<unknown>})"
printf 'IMAGE=%s\n' "${DEPLOY_IMAGE}"

if [ "${INVENTORY_PIN_PRESENT}" != "true" ]; then
    cat <<EOF

NEXT (first pin of stack '${NAME_PREFIX}'; DEPLOY.md, "Re-pinning the inventory baseline"):
  scripts/repin-inventory.sh write-pin --initial --revision ${REVISION:-<revision>} --digest ${DEPLOY_DIGEST}
  git add scripts/scenarios/inventory-pin.${NAME_PREFIX}.env && git commit -m "chore(inventory): first pin of ${NAME_PREFIX}"
  scripts/repin-inventory.sh switch
EOF
elif [ "${DEPLOY_DIGEST}" != "${PINNED_INVENTORY_DIGEST}" ]; then
    cat <<EOF

NOTE: the deployed digest ${DEPLOY_DIGEST} is not the pin (${PINNED_INVENTORY_DIGEST}), and
${REVISION:-the new revision} now serves 100%. Scenarios and resets route traffic back to the pin
(${PINNED_INVENTORY_REVISION}). To make this image the baseline instead:
  scripts/repin-inventory.sh write-pin --revision ${REVISION:-<revision>} --digest ${DEPLOY_DIGEST}
  scripts/repin-inventory.sh switch
(Add --initial to write-pin if ${PINNED_INVENTORY_REVISION} is not a revision in ${PROJECT_ID}/${REGION}.)
EOF
fi
