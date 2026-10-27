#!/usr/bin/env bash
# ==============================================================================
# Verify Stage 1 Vertical Slice Tracer Bullet
# ==============================================================================

set -euo pipefail

PROJECT_ID="${PROJECT_ID:-${GCP_PROJECT:-cloud-assist-fde-2}}"
REGION="${REGION:-${GCP_REGION:-us-west1}}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
BUCKET_NAME="${PROJECT_ID}-${NAME_PREFIX}-incidents"

echo "==================================================================="
echo "  Verifying Stage 1 Vertical Slice Tracer Bullet"
echo "  Project: ${PROJECT_ID} | Region: ${REGION} | Bucket: gs://${BUCKET_NAME}"
echo "==================================================================="

# Discover Cloud Run service URLs
echo "Resolving service URLs..."
INVENTORY_URL="$(gcloud run services describe "${NAME_PREFIX}-inventory-api" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
AGENT_URL="$(gcloud run services describe "${NAME_PREFIX}-agent" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"
BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --project="${PROJECT_ID}" --region="${REGION}" --format="value(status.url)")"

echo "  Inventory API: ${INVENTORY_URL}"
echo "  Agent:         ${AGENT_URL}"
echo "  Bridge:        ${BRIDGE_URL}"

# Acquire GCP Identity Token for Cloud Run IAM invocation
ID_TOKEN="$(gcloud auth print-identity-token 2>/dev/null || true)"
AUTH_HEADER=()
if [ -n "${ID_TOKEN}" ]; then
    AUTH_HEADER=(-H "Authorization: Bearer ${ID_TOKEN}")
fi

# 1. Verify /health endpoints
echo ""
echo "--- 1. Testing /health endpoints ---"
for svc in "Inventory API:${INVENTORY_URL}" "Agent:${AGENT_URL}" "Bridge:${BRIDGE_URL}"; do
    name="${svc%%:*}"
    url="${svc#*:}"
    echo -n "Checking ${name} (${url}/health)... "
    resp="$(curl -s -w "\n%{http_code}" ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} "${url}/health")"
    body="$(echo "${resp}" | head -n 1)"
    code="$(echo "${resp}" | tail -n 1)"
    if [ "${code}" -eq 200 ]; then
        echo "OK (HTTP 200 - ${body})"
    else
        echo "FAILED (HTTP ${code} - ${body})"
        exit 1
    fi
done

# 2. Get webhook token (from Secret Manager or use placeholder)
echo ""
echo "--- 2. Fetching Webhook Authentication Token ---"
WEBHOOK_TOKEN="$(gcloud secrets versions access latest --secret="${NAME_PREFIX}-webhook-token" --project="${PROJECT_ID}" 2>/dev/null || echo "placeholder")"
echo "Webhook token acquired (length: ${#WEBHOOK_TOKEN})"

# 3. Send mock Cloud Monitoring webhook to ata-bridge
echo ""
echo "--- 3. Sending Mock Cloud Monitoring Alert Webhook ---"
TEST_TIMESTAMP="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
TEST_ALERT_PAYLOAD=$(cat <<EOF
{
  "version": "1.2",
  "incident": {
    "incident_id": "0.stage1_test_$(date +%s)",
    "resource_name": "ata-inventory-api",
    "resource_type": "cloud_run_revision",
    "resource": {
      "type": "cloud_run_revision",
      "labels": {
        "service_name": "ata-inventory-api",
        "location": "${REGION}",
        "project_id": "${PROJECT_ID}"
      }
    },
    "state": "open",
    "started_at": "${TEST_TIMESTAMP}",
    "policy_name": "ata-inventory-5xx",
    "condition_name": "Cloud Run 5xx rate high",
    "summary": "Stage 1 verification test alert"
  }
}
EOF
)

WEBHOOK_RESP="$(curl -s -w "\n%{http_code}" -X POST \
    ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
    -H "Content-Type: application/json" \
    "${BRIDGE_URL}/webhook/monitoring?token=${WEBHOOK_TOKEN}" \
    -d "${TEST_ALERT_PAYLOAD}")"

W_BODY="$(echo "${WEBHOOK_RESP}" | head -n 1)"
W_CODE="$(echo "${WEBHOOK_RESP}" | tail -n 1)"

echo "Bridge Response: HTTP ${W_CODE} - ${W_BODY}"
if [ "${W_CODE}" -ne 200 ]; then
    echo "ERROR: Webhook returned non-200 status!"
    exit 1
fi

# 4. Trigger triage on ata-agent directly to verify pre-analysis lock creation & findings
echo ""
echo "--- 4. Executing Agent Triage ---"
ALERT_EVENT_PAYLOAD=$(cat <<EOF
{
  "incident_id": "0.stage1_test_$(date +%s)",
  "policy_name": "ata-inventory-5xx",
  "condition_name": "Cloud Run 5xx rate high",
  "resource_name": "ata-inventory-api",
  "resource_type": "cloud_run_revision",
  "resource_labels": {
    "service_name": "ata-inventory-api"
  },
  "state": "open",
  "started_at": "${TEST_TIMESTAMP}",
  "summary": "Stage 1 verification test alert"
}
EOF
)

AGENT_RESP="$(curl -s -w "\n%{http_code}" -X POST \
    ${AUTH_HEADER[@]+"${AUTH_HEADER[@]}"} \
    -H "Content-Type: application/json" \
    "${AGENT_URL}/triage" \
    -d "${ALERT_EVENT_PAYLOAD}")"

A_BODY="$(echo "${AGENT_RESP}" | head -n 1)"
A_CODE="$(echo "${AGENT_RESP}" | tail -n 1)"

echo "Agent Response: HTTP ${A_CODE} - ${A_BODY}"
if [ "${A_CODE}" -ne 200 ]; then
    echo "ERROR: Agent triage returned non-200 status!"
    exit 1
fi

# 5. Verify incident .md file in GCS bucket gs://${BUCKET_NAME}/
echo ""
echo "--- 5. Verifying GCS Incident File in gs://${BUCKET_NAME}/ ---"
sleep 2
GCS_FILES="$(gcloud storage ls "gs://${BUCKET_NAME}/**.md" --project="${PROJECT_ID}" 2>/dev/null || true)"
echo "Incident files found in GCS:"
echo "${GCS_FILES}"

if [ -z "${GCS_FILES}" ]; then
    echo "WARNING: Direct GCS bucket listing empty; checking root listing..."
    GCS_FILES="$(gcloud storage ls "gs://${BUCKET_NAME}/" --project="${PROJECT_ID}")"
    echo "${GCS_FILES}"
fi

FIRST_FILE="$(echo "${GCS_FILES}" | grep -E '\.md$' | head -n 1 || true)"
if [ -n "${FIRST_FILE}" ]; then
    echo ""
    echo "Inspecting incident file content: ${FIRST_FILE}"
    FILE_CONTENT="$(gcloud storage cat "${FIRST_FILE}" --project="${PROJECT_ID}")"
    echo "${FILE_CONTENT}"

    if echo "${FILE_CONTENT}" | grep -q "status: mitigation-in-progress"; then
        echo "SUCCESS: Found 'status: mitigation-in-progress' in frontmatter."
    else
        echo "ERROR: Frontmatter missing 'status: mitigation-in-progress'!"
        exit 1
    fi

    if echo "${FILE_CONTENT}" | grep -q "type: incident"; then
        echo "SUCCESS: Found 'type: incident' in frontmatter."
    else
        echo "ERROR: Frontmatter missing 'type: incident'!"
        exit 1
    fi
else
    echo "ERROR: No .md files found in gs://${BUCKET_NAME}/"
    exit 1
fi

echo ""
echo "==================================================================="
echo "  Stage 1 Verification Succeeded 100%"
echo "==================================================================="
