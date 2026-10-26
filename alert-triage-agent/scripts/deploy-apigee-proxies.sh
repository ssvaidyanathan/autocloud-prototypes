#!/usr/bin/env bash
set -euo pipefail

PROJECT_ID="${PROJECT_ID:-cloud-assist-fde-2}"
REGION="${REGION:-us-west1}"
ENV_NAME="${ENV_NAME:-prod}"
NAME_PREFIX="${NAME_PREFIX:-ata}"
SA_EMAIL="ata-apigee-sa@${PROJECT_ID}.iam.gserviceaccount.com"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== Deploying Apigee Proxies to ${PROJECT_ID} (env: ${ENV_NAME}) ==="

TOKEN=$(gcloud auth print-access-token)
BASE_URL="https://apigee.googleapis.com/v1/organizations/${PROJECT_ID}"

# Helper to create or update an Apigee TargetServer
create_or_update_targetserver() {
  local name="$1"
  local host="$2"
  echo "--> Ensuring TargetServer ${name} (${host}) in environment ${ENV_NAME}..."

  local http_code
  http_code=$(curl -s -o /dev/null -w "%{http_code}" -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: application/json" \
    "${BASE_URL}/environments/${ENV_NAME}/targetservers" \
    -d '{
      "name": "'"${name}"'",
      "host": "'"${host}"'",
      "port": 443,
      "isEnabled": true,
      "sSLInfo": { "enabled": true }
    }' || echo "000")

  if [ "${http_code}" -eq 409 ]; then
    echo "    TargetServer ${name} exists; updating configuration..."
    curl -s -X PUT \
      -H "Authorization: Bearer ${TOKEN}" \
      -H "Content-Type: application/json" \
      "${BASE_URL}/environments/${ENV_NAME}/targetservers/${name}" \
      -d '{
        "name": "'"${name}"'",
        "host": "'"${host}"'",
        "port": 443,
        "isEnabled": true,
        "sSLInfo": { "enabled": true }
      }' > /dev/null || true
  fi
}

echo "--> Provisioning TargetServers..."
ORDERS_HOST=$(gcloud run services describe "${NAME_PREFIX}-orders-api" --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null | sed -e 's|^https://||' -e 's|/.*$||' || echo "")
if [ -z "${ORDERS_HOST}" ]; then
  ORDERS_HOST="ata-orders-api-4klvlqnmpa-uw.a.run.app"
fi

INV_HOST=$(gcloud run services describe "${NAME_PREFIX}-inventory-api" --region="${REGION}" --project="${PROJECT_ID}" --format="value(status.url)" 2>/dev/null | sed -e 's|^https://||' -e 's|/.*$||' || echo "")
if [ -z "${INV_HOST}" ]; then
  INV_HOST="ata-inventory-api-4klvlqnmpa-uw.a.run.app"
fi

create_or_update_targetserver "ata-orders-target" "${ORDERS_HOST}"
create_or_update_targetserver "ata-inventory-target" "${INV_HOST}"

# Helper to deploy a proxy bundle
deploy_proxy() {
  local proxy_name="$1"
  local bundle_dir="${SCRIPT_DIR}/../demo/apigee/${proxy_name}"
  local zip_file="/tmp/${proxy_name}.zip"

  echo "--> Packaging ${proxy_name}..."
  rm -f "${zip_file}"
  python3 -c "import zipfile, os, sys
bundle_dir, zip_path = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
    for root, _, files in os.walk(bundle_dir):
        for f in files:
            full_path = os.path.join(root, f)
            rel_path = os.path.relpath(full_path, bundle_dir)
            zf.write(full_path, rel_path)
" "${bundle_dir}" "${zip_file}"

  echo "--> Importing ${proxy_name} revision..."
  IMPORT_RESP=$(curl -s -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H "Content-Type: multipart/form-data" \
    -F "file=@${zip_file}" \
    "${BASE_URL}/apis?name=${proxy_name}&action=import")

  REVISION=$(echo "${IMPORT_RESP}" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('revision', '1'))")
  echo "    Imported revision: ${REVISION}"

  echo "--> Deploying ${proxy_name} rev ${REVISION} to environment ${ENV_NAME}..."
  DEPLOY_RESP=$(curl -s -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    "${BASE_URL}/environments/${ENV_NAME}/apis/${proxy_name}/revisions/${REVISION}/deployments?override=true&serviceAccount=projects/-/serviceAccounts/${SA_EMAIL}")

  echo "    Deploy status: $(echo "${DEPLOY_RESP}" | python3 -c "import sys, json; d=json.load(sys.stdin); print(d.get('state', d.get('error', {}).get('message', 'submitted')))")"
}

deploy_proxy "ata-orders-v1"
deploy_proxy "ata-inventory-v1"

echo "--> Configuring API Product, Developer, and Developer App..."
# 1. API Product
curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${BASE_URL}/apiproducts" \
  -d '{
    "name": "ata-product",
    "displayName": "ATA Product",
    "approvalType": "auto",
    "environments": ["'"${ENV_NAME}"'"],
    "proxies": ["ata-orders-v1", "ata-inventory-v1"]
  }' > /dev/null || true

# 2. Developer
curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${BASE_URL}/developers" \
  -d '{
    "email": "ata-dev@example.com",
    "firstName": "ATA",
    "lastName": "Developer",
    "userName": "ata-dev"
  }' > /dev/null || true

# 3. Developer App
APP_RESP=$(curl -s -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  "${BASE_URL}/developers/ata-dev@example.com/apps" \
  -d '{
    "name": "ata-demo-app",
    "apiProducts": ["ata-product"]
  }')

API_KEY=$(echo "${APP_RESP}" | python3 -c "import sys, json; d=json.load(sys.stdin); creds=d.get('credentials', [{}]); print(creds[0].get('consumerKey', ''))" 2>/dev/null || echo "")

if [ -z "${API_KEY}" ]; then
  # Fetch existing app credentials
  APP_RESP=$(curl -s -H "Authorization: Bearer ${TOKEN}" "${BASE_URL}/developers/ata-dev@example.com/apps/ata-demo-app")
  API_KEY=$(echo "${APP_RESP}" | python3 -c "import sys, json; d=json.load(sys.stdin); creds=d.get('credentials', [{}]); print(creds[0].get('consumerKey', ''))")
fi

echo "=== Apigee Proxies Deployed Successfully ==="
echo "Host: 8.233.247.52.nip.io"
echo "Orders BasePath: /ata/orders/v1"
echo "Inventory BasePath: /ata/inventory/v1"
echo "API Key: ${API_KEY}"
(umask 077 && echo "${API_KEY}" > "${SCRIPT_DIR}/../.ata-api-key")
chmod 600 "${SCRIPT_DIR}/../.ata-api-key"
