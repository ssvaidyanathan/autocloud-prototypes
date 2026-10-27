# Alert Triage Agent: Deployment Guide

This guide is the single procedure for deploying a complete **Alert Triage Agent (ATA)** stack into a Google Cloud project from scratch, and for removing it again. It works for any resource name prefix (`NAME_PREFIX`), including a second stack next to an existing one in the same project and Apigee organization.

Every command is meant to be copied and pasted as it is, in bash 3.2 or later (the macOS default `/bin/bash`) or on Linux. Each step states its purpose, the commands, the expected output, how to verify it, and what to do if it fails. Nothing outside this guide is needed.

Markers of the form `TODO(<lane>)` mark steps whose final script or flag is still landing on another branch. Search for `TODO(` before following the guide.

---

## Contents

- [1. Architecture](#1-architecture)
- [2. Before you start](#2-before-you-start)
  - [2.1 Operator prerequisites](#21-operator-prerequisites)
  - [2.2 Inputs](#22-inputs)
  - [2.3 Second stack in an existing project and Apigee organization](#23-second-stack-in-an-existing-project-and-apigee-organization)
  - [2.4 Step overview](#24-step-overview)
- [3. Deployment steps](#3-deployment-steps)
  - [Step 0: Project checks and authentication](#step-0-project-checks-and-authentication)
  - [Step 1: Apigee organization, environment and hostname](#step-1-apigee-organization-environment-and-hostname)
  - [Step 2: Terraform state bootstrap](#step-2-terraform-state-bootstrap)
  - [Step 3: Foundation](#step-3-foundation)
  - [Step 4: ServiceNow secrets](#step-4-servicenow-secrets)
  - [Step 5: Demo images](#step-5-demo-images)
  - [Step 6: Database and schema](#step-6-database-and-schema)
  - [Step 7: Demo applications and the inventory pin](#step-7-demo-applications-and-the-inventory-pin)
  - [Step 8: Triage tier](#step-8-triage-tier)
  - [Step 9: Apigee proxies](#step-9-apigee-proxies)
  - [Step 10: Observability](#step-10-observability)
  - [Step 11: Verification](#step-11-verification)
  - [Step 12: Reset](#step-12-reset)
- [4. Operations reference](#4-operations-reference)
- [5. Teardown](#5-teardown)
- [6. Verify that nothing is left](#6-verify-that-nothing-is-left)

---

## 1. Architecture

Every resource name below starts with the stack's `NAME_PREFIX` (shown as `<prefix>`; the default is `ata`).

```
                                      +------------------------+
                                      |   Client / Consumer    |
                                      +-----------+------------+
                                                  | HTTPS (x-api-key)
                                                  v
                                      +------------------------+
                                      |  Apigee X: APIGEE_HOST |
                                      |  /<prefix>/orders/v1   |
                                      |  /<prefix>/inventory/v1|
                                      +-----------+------------+
                                                  | Google ID token (<prefix>-apigee-sa)
                     +----------------------------+----------------------------+
                     |                                                         |
                     v                                                         v
        +---------------------------+                             +-----------------------------+
        | Cloud Run: <prefix>-      |                             | Cloud Run: <prefix>-        |
        |            orders-api     |                             |            inventory-api    |
        +------------+--------------+                             +-------------+---------------+
                     | Google ID token                                          | IAM DB auth (pg8000)
                     +--------------------------------------------------------->+
                                                                                |
                                                                                v
                                                                  +----------------------------+
                                                                  | Cloud SQL PostgreSQL 15    |
                                                                  | <prefix>-inventory-db      |
                                                                  +----------------------------+
```

```
     [Cloud Monitoring alert policies <prefix>-*]
                 | Webhook channel <prefix>-bridge-webhook (HTTP Basic Auth)
                 v
     +--------------------------+         +--------------------------+
     | Cloud Run: <prefix>-     | ------> | Cloud Tasks              |
     |            bridge        |         | <prefix>-triage-queue    |
     +-----------+--------------+         | (max_concurrent: 1)      |
                 ^   |                    +------------+-------------+
                 |   | Instructions                    |
                 |   | (POST /instructions/ticket/*)   v
                 |   |                    +--------------------------+       +----------------------+
                 |   +------------------- | Cloud Run: <prefix>-     | ----> | Cloud Run: <prefix>- |
                 |                        |            agent         |  MCP  |            tools     |
                 v                        +------------+-------------+       +----------------------+
     +--------------------------+                      | Timers        ^
     | ServiceNow REST API      |                      v               | Wake
     | (Table and Attachment)   |         +--------------------------+ |
     +--------------------------+         | Cloud Tasks              |-+
                                          | <prefix>-timer-queue     |
                                          +--------------------------+
```

The agent calls the tools service (`<prefix>-tools`) over MCP with an ID token to read the change timeline (Cloud Run revisions and Apigee deployments). Both run as `<prefix>-agent-sa`.

> [!IMPORTANT]
> **Labels.** Every resource carries the standard labels (`app=alert-triage-agent`, `component`, `role`, `tier`, `env`, `managed-by`, `owner`). The Terraform roots also set `stack=<prefix>`. For resources without GCP labels (Apigee proxies, Cloud Tasks queues), the metadata is in the proxy bundle `<Description>`. See [CONFIGURATION.md §7](CONFIGURATION.md#7-labels--metadata).

---

## 2. Before you start

### 2.1 Operator prerequisites

| Requirement | Version or detail | Check |
| :--- | :--- | :--- |
| bash | 3.2 or later. The scripts are kept 3.2-compatible (`tests/test_bash32_compat.py`), so the macOS `/bin/bash` works. | `bash --version` |
| Google Cloud SDK | A recent release (480.0.0 or later). Commands used: `gcloud storage`, `gcloud run deploy --add-volume`, `gcloud builds submit`, `gcloud artifacts`, `gcloud sql connect`. No alpha or beta commands are needed (notification channels are read with the Monitoring REST API). | `gcloud version` |
| Terraform | **1.9.0 or later** (all five roots require `>= 1.9.0`) | `terraform version` |
| python3 | Any supported Python 3. The operator scripts use the standard library only (the service packages need 3.12+ only for local development). | `python3 --version` |
| curl | Any recent version | `curl --version` |
| psql | The PostgreSQL client, used by `gcloud sql connect` in the schema step ([Step 6](#step-6-database-and-schema)). On macOS: `brew install libpq` and add its `bin` to `PATH`. | `psql --version` |
| Docker | **Not needed.** Every image is built with Cloud Build. | n/a |
| jq | **Not needed.** | n/a |
| Deployer IAM | `roles/owner` on the project. Without Owner: `roles/editor`, `roles/resourcemanager.projectIamAdmin`, `roles/run.admin`, `roles/iam.serviceAccountUser` on the runtime SAs (or list yourself in the foundation variable `deployer_members`), and `roles/secretmanager.admin`. | [Step 0](#step-0-project-checks-and-authentication) |
| Apigee IAM | `roles/apigee.admin` on the Apigee organization's project (`APIGEE_ORG`): it creates TargetServers, proxies, the API product, developer and app. Provisioning a new organization needs Owner on that project. | [Step 1](#step-1-apigee-organization-environment-and-hostname) |
| ServiceNow | An instance URL and an integration user with the `itil` role (incident and attachment tables). A free Personal Developer Instance works. | Log in to the instance. |
| macOS only | If Python reports `CERTIFICATE_VERIFY_FAILED`: `export SSL_CERT_FILE=/etc/ssl/cert.pem` | n/a |

### 2.2 Inputs

Every step reads the same exported variables. Use **one shell per stack**, and keep the values in a file outside the repository so that you can load them again in a new shell, and for teardown.

1. Clone the repository. Use a **separate clone for every stack** (see [§2.3](#23-second-stack-in-an-existing-project-and-apigee-organization)):

   ```bash
   git clone https://github.com/ssvaidyanathan/autocloud-prototypes.git autocloud-prototypes-<prefix>
   cd autocloud-prototypes-<prefix>/alert-triage-agent
   ```

2. Write the inputs file. Replace every `<...>` value:

   ```bash
   cat > "${HOME}/ata-stack.env" <<'EOF'
   # --- Target ---
   export PROJECT_ID="<project-id>"
   export REGION="<region>"                    # e.g. us-west1
   export NAME_PREFIX="<prefix>"               # 2-10 chars, [a-z][a-z0-9]*; e.g. ata
   export ENVIRONMENT="demo"                   # env label
   export OWNER="sre-prototypes"               # owner label
   # --- ServiceNow (configuration, not a secret) ---
   export SERVICENOW_INSTANCE_URL="https://<instance>.service-now.com"
   # --- Apigee ---
   export APIGEE_ORG="<apigee-org>"            # usually the project ID that hosts Apigee
   export APIGEE_ENV="<apigee-env>"            # e.g. prod
   export APIGEE_HOST="<envgroup-hostname>"    # e.g. 203.0.113.10.nip.io
   # --- Optional ---
   # export VERIFIER_SA="<sa-or-email>,<...>"  # extra callers of the bridge /instructions/* endpoints
   EOF
   ```

   Rename the file per stack if you run several, for example `${HOME}/ata-stack-atat.env`.

3. Load it, and derive the remaining values. **Run this block in every new shell before any step**, from the `alert-triage-agent/` directory of the stack's clone:

   ```bash
   . "${HOME}/ata-stack.env"
   export ATA_DIR="$(pwd)"
   export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
   export AR_REPO="${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker"
   # Terraform reads TF_VAR_<name> as the value of variable <name>. Roots that do not declare
   # a variable ignore it.
   export TF_VAR_project_id="${PROJECT_ID}"
   export TF_VAR_region="${REGION}"
   export TF_VAR_name_prefix="${NAME_PREFIX}"
   export TF_VAR_environment="${ENVIRONMENT}"
   export TF_VAR_owner="${OWNER}"
   gcloud config set project "${PROJECT_ID}"
   test -f "${ATA_DIR}/DEPLOY.md" && echo "OK: ${NAME_PREFIX} in ${PROJECT_ID}/${REGION} (project number ${PROJECT_NUMBER})"
   ```

   **Expected:** `OK: <prefix> in <project>/<region> (project number <digits>)`. If `PROJECT_NUMBER` is empty, you have no access to the project or the ID is wrong.

| Input | Used by |
| :--- | :--- |
| `PROJECT_ID`, `REGION`, `NAME_PREFIX` | Every script and Terraform root. They have **no defaults**: the scripts stop if they are missing. |
| `ENVIRONMENT`, `OWNER` | Labels (defaults `demo`, `sre-prototypes`). |
| `SERVICENOW_INSTANCE_URL` | `bridge/scripts/deploy.sh` ([Step 8](#step-8-triage-tier)). |
| `APIGEE_ORG`, `APIGEE_ENV`, `APIGEE_HOST` | `scripts/deploy-apigee-proxies.sh`, `scripts/teardown-apigee.sh`, the tools service, the scenarios. |
| `VERIFIER_SA` | `bridge/scripts/deploy.sh`, only when exported ([§4.6](#46-read-only-ticket-verification-endpoint)). |
| `PROJECT_NUMBER` | Derived. Builds the deterministic Cloud Run URLs `https://<service>-<PROJECT_NUMBER>.<REGION>.run.app`. |

### 2.3 Second stack in an existing project and Apigee organization

A second stack, for example `NAME_PREFIX=atat` next to a live `ata`, can share the project and the Apigee organization. Every resource name, Terraform state prefix, Apigee proxy, base path, TargetServer, product, developer and app derives from `NAME_PREFIX`, so the stacks do not collide. Apigee matches base paths per path segment, so `/ata/...` never captures `/atat/...`.

Rules:

- **Reuse** the organization, environment and hostname: set `APIGEE_ORG`, `APIGEE_ENV` and `APIGEE_HOST` to the existing values. Skip the provisioning part of [Step 1](#step-1-apigee-organization-environment-and-hostname). Run only its checks.
- **Use a new prefix.** It must not be in use: Step 1 and Step 2 check this.
- **Use a separate clone per stack.** Each Terraform root has one `backend.hcl` and one `.terraform/` directory per checkout. `.terraform/` remembers the last backend it was initialised with, and `scripts/scenarios/inventory-pin.env` holds one stack's pin.
- **Never share a checkout's `.terraform/` between prefixes.** Do not copy `.terraform/` or `backend.hcl` from another clone. Always run `terraform init -reconfigure -backend-config=backend.hcl`, as every step here does.
- **Use a separate shell with its own inputs file.** `TF_VAR_name_prefix` and `NAME_PREFIX` are read by every step.
- Both stacks may use the same ServiceNow instance. Their tickets then share a queue and differ only in the group IDs and the `<prefix>-*` names in the text.
- Do not commit or push `scripts/scenarios/inventory-pin.env` from a second stack's clone. It holds that stack's pin ([Step 7](#step-7-demo-applications-and-the-inventory-pin)).

### 2.4 Step overview

| Step | What | Takes about |
| :--- | :--- | :--- |
| 0 | Project checks and authentication | 5 min |
| 1 | Apigee organization, environment and hostname: check, or provision | 5 min to check; 30-90 min to provision. Start it first and continue with Steps 2-8 meanwhile. |
| 2 | Terraform state bucket and `backend.hcl` for the five roots | 1 min |
| 3 | Foundation: APIs, SAs, IAM, bucket, queues, registry, secrets, webhook channel | 3-5 min |
| 4 | ServiceNow credentials into Secret Manager (before the bridge starts) | 1 min |
| 5 | Build the inventory and orders images | 3-5 min |
| 6 | Cloud SQL instance, schema and GRANT | 10-15 min |
| 7 | Demo applications (Terraform), then the first inventory pin | 5 min |
| 8 | Triage tier: tools, agent and bridge, then the `triage-iam` root | 10 min |
| 9 | Apigee proxies, product, developer, app and API key | 3 min |
| 10 | Observability: alert policies | 1 min |
| 11 | Verification: infrastructure, bridge, preflight, S1 and S2 | 40-60 min |
| 12 | Reset | 5-15 min |

---

## 3. Deployment steps

All commands start from `${ATA_DIR}` (the `alert-triage-agent/` directory of the stack's clone), with the inputs loaded ([§2.2](#22-inputs)).

### Step 0: Project checks and authentication

**Purpose:** confirm that the tools, the account, billing and the organization policies allow the deployment, before anything is created.

```bash
cd "${ATA_DIR}"
bash --version | head -1; gcloud version | head -1; terraform version | head -1
python3 --version; curl --version | head -1; psql --version

gcloud auth login
gcloud auth application-default login

gcloud billing projects describe "${PROJECT_ID}" --format='value(billingEnabled)'
gcloud projects get-iam-policy "${PROJECT_ID}" --flatten='bindings[].members' \
  --filter="bindings.members:$(gcloud config get-value account 2>/dev/null)" --format='value(bindings.role)'

for c in iam.allowedPolicyMemberDomains sql.restrictPublicIp iam.automaticIamGrantsForDefaultServiceAccounts run.allowedIngress; do
  echo "== ${c}"
  gcloud resource-manager org-policies describe "constraints/${c}" --project="${PROJECT_ID}" --effective 2>&1 | grep -E 'booleanPolicy|enforced|allValues|allowedValues|deniedValues|constraint' || echo "(not set)"
done
```

**Expected:**
- Terraform 1.9 or later, and the other tools present.
- `billingEnabled` is `True`.
- Your roles include `roles/owner` (or the set in [§2.1](#21-operator-prerequisites)).
- The organization policies:

| Constraint | Must be | Why |
| :--- | :--- | :--- |
| `iam.allowedPolicyMemberDomains` | not set, or allowing `allUsers` | The bridge is deployed with `--allow-unauthenticated`, so that the Cloud Monitoring webhook can reach it. The webhook authenticates with HTTP Basic Auth. |
| `sql.restrictPublicIp` | not enforced | The database root creates a public IP instance. There is no private IP option in this repository. |
| `run.allowedIngress` | not set, or allowing `all` | All services are deployed with ingress `all`. Access is controlled by Cloud Run IAM. |
| `iam.automaticIamGrantsForDefaultServiceAccounts` | either | If enforced, the default Cloud Build service account has no roles: do the grants in [Step 5](#step-5-demo-images) "If it fails". |

**If it fails:**
- A missing tool: install it (§2.1).
- Billing `False`: link a billing account (`gcloud billing projects link "${PROJECT_ID}" --billing-account=<id>`).
- A blocking policy: ask the organization admin for a project-level exception. The repository has no workaround for `sql.restrictPublicIp` or `iam.allowedPolicyMemberDomains`.

---

### Step 1: Apigee organization, environment and hostname

**Purpose:** the proxies in [Step 9](#step-9-apigee-proxies) need an Apigee organization `APIGEE_ORG` with an environment `APIGEE_ENV`, attached to an instance and to an environment group whose hostname is `APIGEE_HOST`. Nothing in this repository creates them.

#### 1a. Existing organization (also the second-stack case)

```bash
TOKEN="$(gcloud auth print-access-token)"
API="https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"
curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}" | grep -E '"(name|runtimeType|state)"'
curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/environments/${APIGEE_ENV}" | grep -E '"(name|state)"'
curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/envgroups" | grep -F "\"${APIGEE_HOST}\"" && echo "OK: ${APIGEE_HOST} is an envgroup hostname"
curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/apis" | grep -E "\"${NAME_PREFIX}-" || echo "OK: no ${NAME_PREFIX}-* proxies in ${APIGEE_ORG}"
curl -sS -o /dev/null -w 'APIGEE_HOST answers: HTTP %{http_code}\n' -k "https://${APIGEE_HOST}/"
```

**Expected:**
- The organization with `"state": "ACTIVE"`, and the environment.
- `OK: <APIGEE_HOST> is an envgroup hostname`.
- `OK: no <prefix>-* proxies`.
- An HTTP status from `APIGEE_HOST`: any code, usually 404, shows that the hostname routes to Apigee.

Check that the environment is attached to the envgroup and the instance:

```bash
for g in $(curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/envgroups" | python3 -c 'import json,sys; print(" ".join(g["name"] for g in json.load(sys.stdin).get("environmentGroups", [])))'); do
  curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/envgroups/${g}/attachments" | grep -q "\"${APIGEE_ENV}\"" && echo "envgroup ${g}: ${APIGEE_ENV} attached"
done
for i in $(curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/instances" | python3 -c 'import json,sys; print(" ".join(i["name"] for i in json.load(sys.stdin).get("instances", [])))'); do
  curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/instances/${i}/attachments" | grep -q "\"${APIGEE_ENV}\"" && echo "instance ${i}: ${APIGEE_ENV} attached"
done
```

**Expected:** at least one `envgroup ...: <env> attached` line and one `instance ...: <env> attached` line.

**If it fails:**
- 403 errors: you lack `roles/apigee.admin` (or at least read access) on the organization.
- A `<prefix>-*` proxy exists: that prefix is in use. Choose another `NAME_PREFIX`, or tear that stack down first.
- `APIGEE_HOST` does not answer: the envgroup hostname is wrong, or no load balancer serves it.

#### 1b. New organization

Provision Apigee in the project before Step 9. This takes 30-90 minutes, so start it now and continue with Step 2 meanwhile.

1. In the Cloud console, open **Apigee** in project `${APIGEE_ORG}` and follow the provisioning wizard. An evaluation organization is free for 60 days. Choose:
   - the runtime region, normally `${REGION}`;
   - an environment named `${APIGEE_ENV}`;
   - an environment group with the external load balancer. The wizard offers an external HTTPS load balancer with a Google-managed IP and a `<IP>.nip.io` hostname. That hostname is your `APIGEE_HOST`.
2. Wait until the wizard reports the instance, the environment and the environment group as ready, then run the checks in 1a.
3. Write `APIGEE_HOST` into the inputs file and load it again.

The wizard creates the network peering, the instance, the environment, the environment group and the load balancer. The `apigee.googleapis.com` API is also enabled by the foundation root in Step 3. Note the load balancer's running cost (tens of USD per month). The teardown ([§5](#5-teardown)) does not delete the organization.

---

### Step 2: Terraform state bootstrap

**Purpose:** enable the APIs Terraform needs before it can run, create the state bucket `gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate`, and write `backend.hcl` (prefix `${NAME_PREFIX}/<root>`) into all five roots: foundation, database, applications, observability and triage-iam.

```bash
cd "${ATA_DIR}"
./scripts/bootstrap-tfstate.sh
```

The script reads `PROJECT_ID`, `REGION`, `NAME_PREFIX`, `ENVIRONMENT` and `OWNER` from the environment (flags `--project-id`, `--region`, `--name-prefix`, `--env`, `--owner` also work). `--dry-run` prints what it would do.

**Expected:** `Created .../backend.hcl (prefix: <prefix>/<root>)` for each of the five roots, then `SUCCESS: Terraform state bucket gs://<project>-<prefix>-tfstate configured.`

**Verify:**

```bash
cat infra/terraform/*/backend.hcl
gcloud storage buckets describe "gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate" --format='value(name,versioning_enabled)'
```

Each `backend.hcl` names bucket `<project>-<prefix>-tfstate` and prefix `<prefix>/<root>`. The bucket has versioning `True`.

**If it fails:**
- `required input(s) not set`: load the inputs (§2.2).
- `NAME_PREFIX ... is invalid`: use 2-10 lowercase letters or digits, starting with a letter.
- `gcloud services enable` is denied: you need `serviceusage.services.enable` (Owner or Editor).
- Bucket creation fails with a name conflict: bucket names are global. Pass `--bucket <name>` and export `TFSTATE_BUCKET=<name>` for the rest of the session (`scripts/deploy-demo-infra.sh` checks it).

---

### Step 3: Foundation

**Purpose:** create everything the stack's services need before they run:
- the project APIs;
- six service accounts: `<prefix>-bridge-sa`, `<prefix>-agent-sa`, `<prefix>-inventory-sa`, `<prefix>-orders-sa`, `<prefix>-apigee-sa` and `<prefix>-build-sa`, with their IAM;
- the incidents bucket, the build bucket and the Artifact Registry repository `<prefix>-docker`;
- the triage and timer queues;
- the webhook token and the ServiceNow placeholder secrets;
- the Cloud Monitoring webhook channel, at the bridge's deterministic URL.

```bash
cd "${ATA_DIR}/infra/terraform/foundation"
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
cd "${ATA_DIR}"
```

If you deploy without Owner, add your principal first: `export TF_VAR_deployer_members='["user:you@example.com"]'`. It grants `roles/iam.serviceAccountUser` on the Apigee SA and the build SA.

**Expected:** on a fresh stack the plan only adds resources (`0 to change, 0 to destroy`). The apply ends with `Apply complete!`, and the outputs include `bridge_webhook_url`, `project_number` and `build_sa_id`.

**Verify:**

```bash
terraform -chdir=infra/terraform/foundation output -raw bridge_webhook_url; echo
echo "expected: https://${NAME_PREFIX}-bridge-${PROJECT_NUMBER}.${REGION}.run.app/webhook/monitoring"
gcloud iam service-accounts list --project="${PROJECT_ID}" --filter="email~^${NAME_PREFIX}-" --format='value(email)'
gcloud tasks queues list --location="${REGION}" --project="${PROJECT_ID}" --filter="name~/${NAME_PREFIX}-" --format='value(name)'
gcloud secrets list --project="${PROJECT_ID}" --filter="name~/${NAME_PREFIX}-" --format='value(name)'
```

- The two URLs are equal.
- Six SAs: `<prefix>-{agent,apigee,bridge,build,inventory,orders}-sa`. There is no tools SA: the tools service runs as `<prefix>-agent-sa`.
- Two queues: `<prefix>-triage-queue` and `<prefix>-timer-queue`.
- Three secrets: `<prefix>-webhook-token`, `<prefix>-servicenow-username` and `<prefix>-servicenow-password`.

**If it fails:**
- `API ... has not been used in project ... or it is disabled`: the APIs enabled by this same apply take a minute or two to propagate. Wait, then run `terraform plan -out=tfplan && terraform apply tfplan` again.
- `Error acquiring the state lock`: another run holds the lock. Wait for it. Use `terraform force-unlock <id>` only if you are sure no other run is active.
- A `409 already exists` on a `<prefix>-*` resource: the prefix is in use, or an earlier stack was not fully removed. Run the checks in [§6](#6-verify-that-nothing-is-left).

To change only a queue later, see [§4.1](#41-applying-queue-only-changes).

---

### Step 4: ServiceNow secrets

**Purpose:** replace the `placeholder` versions that the foundation created with the real ServiceNow username and password. Do this **before** the bridge deploy in [Step 8](#step-8-triage-tier). The bridge mounts the secrets as `:latest` environment variables, which are resolved when an instance starts, so a bridge started earlier keeps `placeholder` until its next revision.

```bash
cd "${ATA_DIR}"
./scripts/set-secrets.sh
```

It prompts for the username, then the password, without echoing the password.

**Expected:** `Updated secret <prefix>-servicenow-username with a new version.`, the same for the password, then `Secrets configuration completed.`

**Verify:**

```bash
for s in servicenow-username servicenow-password; do
  gcloud secrets versions list "${NAME_PREFIX}-${s}" --project="${PROJECT_ID}" --filter='state=ENABLED' --format='value(name)' | wc -l
done
```

Each secret has 2 enabled versions: the placeholder and yours. Do not print the values.

**If it fails:**
- The script printed `Creating secret placeholder ...`: Step 3 did not complete, or `NAME_PREFIX` or `PROJECT_ID` differs from Step 3. Check your inputs.
- `Skipped ... (empty value provided)`: run the script again and enter a value.

> [!NOTE]
> The ServiceNow instance URL is configuration, not a secret. `bridge/scripts/deploy.sh` reads it from `SERVICENOW_INSTANCE_URL`. The webhook token `<prefix>-webhook-token` is generated by Terraform.

---

### Step 5: Demo images

**Purpose:** build the demo inventory and orders images into the stack's repository `${AR_REPO}`, and record their digests. The applications root ([Step 7](#step-7-demo-applications-and-the-inventory-pin)) deploys exactly these digests. The agent, bridge and tools images are built by their own deploy scripts in Step 8.

> TODO(scripts-lane): replace the two `gcloud builds submit` commands with the orders/inventory build script once it lands, keeping the exported `TF_VAR_inventory_image` and `TF_VAR_orders_image` and the `INVENTORY_DIGEST` value. The script should build with `--service-account "$(terraform -chdir=infra/terraform/foundation output -raw build_sa_id)"`.

```bash
cd "${ATA_DIR}"
TAG="initial-$(date -u +%Y%m%d%H%M%S)"
INVENTORY_DIGEST="$(gcloud builds submit demo/ata-inventory-api --project="${PROJECT_ID}" \
  --tag="${AR_REPO}/${NAME_PREFIX}-inventory-api:${TAG}" --format='value(results.images[0].digest)')"
ORDERS_DIGEST="$(gcloud builds submit demo/ata-orders-api --project="${PROJECT_ID}" \
  --tag="${AR_REPO}/${NAME_PREFIX}-orders-api:${TAG}" --format='value(results.images[0].digest)')"
echo "inventory ${INVENTORY_DIGEST}"; echo "orders    ${ORDERS_DIGEST}"
cat >> "${HOME}/ata-stack.env" <<EOF
export INVENTORY_DIGEST="${INVENTORY_DIGEST}"
export TF_VAR_inventory_image="${AR_REPO}/${NAME_PREFIX}-inventory-api@${INVENTORY_DIGEST}"
export TF_VAR_orders_image="${AR_REPO}/${NAME_PREFIX}-orders-api@${ORDERS_DIGEST}"
EOF
. "${HOME}/ata-stack.env"
```

The digests are appended to the inputs file, so a new shell and the teardown have them too.

**Expected:** each build streams its log and ends with `SUCCESS`, and each `echo` shows `sha256:<64 hex>`.

**Verify:**

```bash
gcloud artifacts docker images list "${AR_REPO}" --include-tags --format='value(package,version,tags)'
```

It lists `<prefix>-inventory-api` and `<prefix>-orders-api` with the digests and the tag.

**If it fails:** a build that fails on source upload, image push or log writing (`PERMISSION_DENIED`, `does not have storage.objects.get access`, `artifactregistry.repositories.uploadArtifacts`) means the default Cloud Build service account lacks roles. That happens in projects where `iam.automaticIamGrantsForDefaultServiceAccounts` is enforced. Grant them, then build again:

```bash
BUILD_SA="$(gcloud builds get-default-service-account --project="${PROJECT_ID}" --format='value(serviceAccountEmail)' | sed 's|.*/||')"
for r in roles/artifactregistry.writer roles/storage.objectViewer roles/logging.logWriter; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" --member="serviceAccount:${BUILD_SA}" --role="${r}" --condition=None >/dev/null
done
```

The foundation root already created a dedicated build SA (`<prefix>-build-sa`) with exactly these permissions, and a staging bucket `<project>-<prefix>-build`. The deploy scripts do not use them yet (TODO(scripts-lane)).

---

### Step 6: Database and schema

**Purpose:** create the Cloud SQL PostgreSQL 15 instance `<prefix>-inventory-db`, with the `inventory` database and the IAM database user `<prefix>-inventory-sa@<project>.iam`. Then create and seed the `inventory_items` table and grant that user `SELECT, UPDATE` on it. Without the GRANT, every `/items` request returns 500.

```bash
cd "${ATA_DIR}/infra/terraform/database"
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
cd "${ATA_DIR}"
```

**Expected:** `Apply complete! Resources: 3 added` (instance, database, IAM user). The instance takes 5-15 minutes.

Then apply the schema as the `postgres` user. Terraform sets no `postgres` password, so set a one-time one first. `gcloud sql connect` allowlists your public IP for a few minutes and runs `psql`:

```bash
cd "${ATA_DIR}"
export PGPASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
gcloud sql users set-password postgres --instance="${NAME_PREFIX}-inventory-db" \
  --project="${PROJECT_ID}" --password="${PGPASSWORD}"
{ printf "\\\\set iam_user '%s'\n" "${NAME_PREFIX}-inventory-sa@${PROJECT_ID}.iam"; cat demo/db/schema.sql; } \
  | gcloud sql connect "${NAME_PREFIX}-inventory-db" --user=postgres --database=inventory --project="${PROJECT_ID}"
unset PGPASSWORD
```

`schema.sql` stops at the first error (`ON_ERROR_STOP`) and fails if `iam_user` is not set, so the GRANT cannot be skipped silently.

**Expected:** psql prints `CREATE TABLE`, `INSERT 0 5` and `GRANT`.

**Verify:**

```bash
gcloud sql instances describe "${NAME_PREFIX}-inventory-db" --project="${PROJECT_ID}" --format='value(state,settings.tier)'
gcloud sql users list --instance="${NAME_PREFIX}-inventory-db" --project="${PROJECT_ID}" --format='value(name,type)'
```

`RUNNABLE db-f1-micro`, and the user `<prefix>-inventory-sa@<project>.iam` of type `CLOUD_IAM_SERVICE_ACCOUNT`. The application path is checked in Step 7 (`/items` through the pin verification).

**If it fails:**
- `psql: command not found`: install the client (§2.1).
- `gcloud sql connect` times out: your network has no public IPv4 egress, or blocks port 5432. Run the block from Cloud Shell instead (it has `psql`): clone the repository there and load the inputs.
- `ERROR: syntax error at or near ":"` at the GRANT: `iam_user` was not set. Check the `printf` line.
- `relation "inventory_items" already exists`: harmless. The script uses `CREATE TABLE IF NOT EXISTS` and upserts the seed rows. Run it again.

---

### Step 7: Demo applications and the inventory pin

**Purpose:** deploy `<prefix>-inventory-api` and `<prefix>-orders-api` on Cloud Run from the images of Step 5, with their invoker IAM (orders to inventory, Apigee SA to both). Then establish the first **inventory pin**. Scenarios, reset and preflight restore and check inventory traffic against one pinned revision, recorded in `scripts/scenarios/inventory-pin.env`.

```bash
cd "${ATA_DIR}/infra/terraform/applications"
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
cd "${ATA_DIR}"
```

The image variables come from `TF_VAR_inventory_image` and `TF_VAR_orders_image` (Step 5). If they are missing, Terraform prompts for `inventory_image`: stop, and load the inputs file again.

**Expected:** `Apply complete! Resources: 5 added` (two Cloud Run services and three invoker bindings).

Then pin the inventory baseline. On a fresh stack no pinned revision exists yet, so use `--initial`:

```bash
cd "${ATA_DIR}"
./scripts/repin-inventory.sh prepare --initial --image-digest "${INVENTORY_DIGEST}"
```

`prepare` deploys a candidate revision with no traffic, checks it (digest, `ATA_INVENTORY_*` env, max instances, Ready), and sends GET requests through a temporary tag URL: `/health`, `/items` (which proves the Cloud SQL IAM login and the GRANT) and the admin probes. It removes the tag before it exits. It ends with `Candidate verified: <prefix>-inventory-api-<nnnnn>-<xxx> (sha256:...)` and prints the next command. Run it, replacing `<candidate>` with the revision from that line:

```bash
./scripts/repin-inventory.sh write-pin --initial --revision <candidate> --digest "${INVENTORY_DIGEST}"
./scripts/repin-inventory.sh switch
./scripts/set-demo-instance-caps.sh --apply
```

- `write-pin` writes the pin file.
- `switch` moves 100% of the traffic to the pinned revision, and verifies serving, `/health`, `/items` and the admin probes.
- `set-demo-instance-caps.sh --apply` sets the service-level instance limit (default 1) on both demo services ([§4.2](#42-instance-caps)).

**Verify:**

```bash
./scripts/repin-inventory.sh status
cat scripts/scenarios/inventory-pin.env | grep '^PIN_'
```

`status` exits 0 and reports traffic 100% on the pin, with no baseline gaps. The pin file names a `<prefix>-inventory-api-...` revision and `INVENTORY_DIGEST`.

**If it fails:**
- `--initial: a pin exists`: the pin file in this clone names a revision that exists in this project. You are probably in the wrong clone or project.
- `/items` fails in `prepare`: the schema or the GRANT is missing (Step 6), or the instance is not `RUNNABLE` yet.
- The applications apply fails with `service account ... does not exist`: Step 3 did not complete for this prefix.

> [!NOTE]
> The pin file is per stack and per clone. In the clone of a second stack, keep the file local: do not commit or push it.

---

### Step 8: Triage tier

**Purpose:** build and deploy the tools service, the agent and the bridge, then apply the `triage-iam` root. That root holds the service-level `run.invoker` bindings between them (bridge to agent, agent to agent for timers, agent to bridge, agent to tools). Each deploy script resolves the other services' URLs: an exported value wins, then the service's `status.url`, then the deterministic URL `https://<service>-${PROJECT_NUMBER}.${REGION}.run.app` for a service that is not deployed yet. The deploy order therefore does not matter.

```bash
cd "${ATA_DIR}/tools"  && ./scripts/deploy.sh
cd "${ATA_DIR}/agent"  && ./scripts/deploy.sh
cd "${ATA_DIR}/bridge" && ./scripts/deploy.sh
cd "${ATA_DIR}"
```

> TODO(tools-lane): `agent/scripts/deploy.sh` must set `TOOLS_URL` (the agent refuses to start without it), and `tools/scripts/deploy.sh` must set `APIGEE_ENV` (the tools service needs it for the change timeline). Confirm both are wired before running this step.

- The scripts take `PROJECT_ID`, `REGION`, `NAME_PREFIX`, `ENVIRONMENT`, `OWNER` and `PROJECT_NUMBER` from the inputs.
- The tools script also takes `APIGEE_ORG`.
- The bridge script also takes `SERVICENOW_INSTANCE_URL`, and `VERIFIER_SA` when it is exported.
- Each script prints `Successfully deployed <prefix>-<service>: https://...`.
- On a first deploy, the bridge script also updates `BRIDGE_URL` to the service's real `status.url`.

The full list of variables the scripts set is in [§4.5](#45-environment-variables-set-by-the-deploy-scripts).

Then apply the invoker bindings. This root fails with "service not found" if any of the three services is missing:

```bash
cd "${ATA_DIR}/infra/terraform/triage-iam"
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
cd "${ATA_DIR}"
```

**Expected:** `Apply complete! Resources: 4 added`.

**Verify:**

```bash
for s in tools agent bridge; do
  gcloud run services describe "${NAME_PREFIX}-${s}" --region="${REGION}" --project="${PROJECT_ID}" \
    --format='value(metadata.name,status.url,spec.template.spec.serviceAccountName)'
done
BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
curl -sS "${BRIDGE_URL}/health"; echo
AGENT_URL="$(gcloud run services describe "${NAME_PREFIX}-agent" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
curl -sS -H "Authorization: Bearer $(gcloud auth print-identity-token)" "${AGENT_URL}/health"; echo
```

- The three services are listed with URLs. The agent and tools run as `<prefix>-agent-sa`, and the bridge as `<prefix>-bridge-sa`.
- Both `/health` calls return a JSON status. The bridge is public; the agent needs your identity token.

**If it fails:**
- `PROJECT_ID and REGION are required`: load the inputs.
- `SERVICENOW_INSTANCE_URL is required`: export it (it is in the inputs file).
- A revision fails to start, with `RuntimeError` naming `BRIDGE_URL`, `PROJECT_NUMBER` or `TOOLS_URL` in the logs: the setting was not passed. Check the TODO above, and redeploy that service.
- A build fails with a permission error: see Step 5 "If it fails".
- `triage-iam` fails with `Error 404 ... service not found`: deploy the missing service, then apply again.

---

### Step 9: Apigee proxies

**Purpose:** deploy the stack's Apigee resources: TargetServers `<prefix>-orders-target` and `<prefix>-inventory-target`, pointing at the stack's own Cloud Run URLs, and proxies `<prefix>-orders-v1` (`/<prefix>/orders/v1`) and `<prefix>-inventory-v1` (`/<prefix>/inventory/v1`), running as `<prefix>-apigee-sa`. It also creates the API product `<prefix>-product`, the developer `<prefix>-dev@example.com` and the app `<prefix>-demo-app`, and writes the consumer key.

```bash
cd "${ATA_DIR}"
./scripts/deploy-apigee-proxies.sh
```

The script requires `PROJECT_ID`, `REGION`, `NAME_PREFIX`, `APIGEE_ENV` and `APIGEE_HOST`. `APIGEE_ORG` defaults to `PROJECT_ID`, and the script says so when it uses the default. It prints the exact list of resources before it changes anything. Every management API call is checked, and any non-2xx response stops it with the response body. It touches only `<prefix>-*` names.

**Expected:** each proxy reaches `READY`. The script writes:
- `${ATA_DIR}/.${NAME_PREFIX}-api-key`: the app's consumer key (mode 600);
- `${ATA_DIR}/.${NAME_PREFIX}-apigee.env`: `APIGEE_HOST`, `APIGEE_ORG` and `APIGEE_ENV`, in a sourceable form.

Both files are local and ignored by Git.

**Verify:**

```bash
curl -sS -o /dev/null -w 'no key: %{http_code}\n' -k "https://${APIGEE_HOST}/${NAME_PREFIX}/orders/v1/orders/test"
curl -sS -o /dev/null -w 'with key: %{http_code}\n' -k -H "x-api-key: $(cat ".${NAME_PREFIX}-api-key")" "https://${APIGEE_HOST}/${NAME_PREFIX}/inventory/v1/items"
```

`no key: 401` and `with key: 200`.

**If it fails:**
- `gcloud run services describe <prefix>-orders-api failed` or `has no status.url`: Step 7 did not complete.
- 403 from `apigee.googleapis.com`: you lack Apigee admin on `APIGEE_ORG` (§2.1).
- The deployment does not become `READY`, or fails on the service account: check that `<prefix>-apigee-sa` exists (Step 3) and that you may act as it (Owner, or `deployer_members`).
- Undo the step with `./scripts/teardown-apigee.sh --apply` ([§5](#5-teardown)).

---

### Step 10: Observability

**Purpose:** create the eight Cloud Monitoring alert policies `<prefix>-*` ([§4.4](#44-alert-policies)). They notify the foundation's webhook channel `<prefix>-bridge-webhook`, which the root looks up by name. Apply this root last, once the services exist, so that cold starts during the deployment do not page.

```bash
cd "${ATA_DIR}/infra/terraform/observability"
terraform init -reconfigure -backend-config=backend.hcl
terraform plan -out=tfplan
terraform apply tfplan
cd "${ATA_DIR}"
```

**Expected:** `Apply complete! Resources: 8 added`.

**Verify:**

```bash
gcloud monitoring policies list --project="${PROJECT_ID}" --filter="displayName:${NAME_PREFIX}-" \
  --format='value(displayName,enabled,notificationChannels)'
curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/notificationChannels" \
  | python3 -c 'import json,os,sys; [print(c["name"], c.get("labels", {}).get("url")) for c in json.load(sys.stdin).get("notificationChannels", []) if c.get("displayName") == os.environ["NAME_PREFIX"] + "-bridge-webhook"]'
```

There are eight `<prefix>-*` policies, all enabled, and each notifies the one channel listed by the second command. The channel's URL is the stack's own bridge.

`displayName:` is a substring match, so with the prefix `ata`, `atat-*` policies are listed as well. Check the names.

**If it fails:** `no matching notification channel` (or a lookup error on `<prefix>-bridge-webhook`) means the foundation was not applied for this prefix.

---

### Step 11: Verification

**Purpose:** prove the whole path end to end. Check the demo services and Apigee, then the bridge webhook, the preflight, and finally two real scenarios that must each produce one incident group and one ServiceNow ticket.

> TODO(scripts-lane): the verification and scenario scripts read the API key from `.ata-api-key` and use hardcoded `/ata/...` base paths and `ata-*` names. Until the scripts lane lands the prefix variables, run them only for `NAME_PREFIX=ata`. Confirm the final variable names (`APIGEE_HOST` and the key file) and remove this note.

Load the Apigee values and the key into the environment that the scripts read:

```bash
cd "${ATA_DIR}"
set -a; . "./.${NAME_PREFIX}-apigee.env"; set +a
export ATA_API_KEY="$(cat "./.${NAME_PREFIX}-api-key")"
```

**11.1 Demo services and Apigee**

```bash
./scripts/verify-demo-infra.sh
```

Expected: the direct and Apigee checks report the expected status codes, with no failure listed. If a check fails, the script names the request and the status code. Fix the step that owns it: 5xx from inventory points to Steps 6-7, 401 or 404 through Apigee points to Step 9.

**11.2 Bridge webhook**

```bash
./scripts/verify-stage1.sh
```

> TODO(scripts-lane): `verify-stage1.sh` must accept HTTP **202** from `/webhook/monitoring` (the bridge's answer to an accepted alert), and must not create a real incident through the agent's `/triage`. Until it does, it fails on a healthy bridge. Record the final flags here.

Expected: the health checks pass, and the webhook returns `202 {"status":"accepted",...}`.

**11.3 Preflight**

```bash
./scripts/scenarios/preflight.sh
```

Expected: every check passes:
- the demo services and their admin endpoints;
- the Apigee proxies and SpikeArrest;
- the alert policies;
- the Cloud SQL instance;
- the inventory pin and the instance caps.

A failure names the item to fix.

**11.4 Scenarios S1 and S2**

```bash
./scripts/run-all-scenarios.sh --scenario s1
./scripts/run-all-scenarios.sh --scenario s2
```

Each run takes about 15-25 minutes. That covers the traffic, the alerts opening, the settle timer and the investigation, followed by the reset. [DEMO.md](DEMO.md) has the timings and the expected result of each scenario.

**Expected:** each scenario's summary line reports PASS. Then check the result and the ticket:

```bash
./scripts/scenarios/check-result.sh --scenario s1 --bucket "${PROJECT_ID}-${NAME_PREFIX}-incidents"
./scripts/scenarios/check-result.sh --scenario s2 --bucket "${PROJECT_ID}-${NAME_PREFIX}-incidents"
```

The checker prints, for each incident group, the origin, the ticket number (`INC...`), the alert count and the blast radius, and compares them with the expected values. Open each ticket number in the ServiceNow instance (`${SERVICENOW_INSTANCE_URL}`) and check three things:
- the short description names a `<prefix>-*` component;
- the work notes and the HTML attachment are present;
- there is one ticket per scenario.

There is no separate automated test-suite count to compare against. The deployment is verified when 11.1-11.4 pass.

**If it fails:**
- No ticket, and the group stays open: check the agent logs for timer task errors (`gcloud run services logs read "${NAME_PREFIX}-agent" --region="${REGION}" --project="${PROJECT_ID}" --limit=100`). A `PERMISSION_DENIED` on Cloud Tasks or on `actAs` points to Step 3. A 403 on the agent points to `triage-iam`.
- A ticket that says `change timeline unavailable`: the agent could not call the tools service. Check `TOOLS_URL` on the agent and the `tools_invoker_agent` binding (Step 8).
- The bridge logs show ServiceNow 401: the secrets still hold `placeholder`, or the credentials are wrong. Run Step 4 again, then redeploy the bridge (`cd "${ATA_DIR}/bridge" && ./scripts/deploy.sh`) so that a new revision reads them.

---

### Step 12: Reset

**Purpose:** return the demo to its baseline after the scenarios: faults cleared, traffic on the pinned inventory revision, stock restored, open alerts closed, and terminal incident groups archived.

```bash
cd "${ATA_DIR}"
./scripts/reset-scenarios.sh
```

**Expected:** it ends with a success line after the alert, incident-group and quiet-period waits (5-15 minutes; a reset after S4 takes longer). The reset uses ADC (`gcloud auth application-default login`) for Cloud Monitoring.

**If it fails:** it names the check that failed. See [README.md §14](README.md#14-testing-guide--post-deploy-smoke-tests) for the reset options. On macOS, `CERTIFICATE_VERIFY_FAILED` needs `export SSL_CERT_FILE=/etc/ssl/cert.pem`.

---

## 4. Operations reference

### 4.1 Applying queue-only changes

When you change only a queue (its `retry_config`, or the `triage_queue_*` or `timer_queue_*` variables), target that queue resource:

```bash
cd "${ATA_DIR}/infra/terraform/foundation"
terraform init -reconfigure -backend-config=backend.hcl
# Triage queue:
terraform plan -target=google_cloud_tasks_queue.triage_queue -out=tfplan
# Timer queue:
terraform plan -target=google_cloud_tasks_queue.timer_queue -out=tfplan
terraform apply tfplan
```

> [!WARNING]
> Do not assume that an untargeted `terraform apply` of the foundation root changes only the queue. Before any untargeted apply, run a full `terraform plan` and review every change it lists. In particular, check that `random_password.webhook_token` is not replaced, because a replacement rotates the webhook token.

After the apply, confirm the live settings with `gcloud tasks queues describe "${NAME_PREFIX}-triage-queue" --location="${REGION}" --project="${PROJECT_ID}"` (or `${NAME_PREFIX}-timer-queue`). The queue settings are in [CONFIGURATION.md §4](CONFIGURATION.md#4-cloud-tasks-queue-configuration).

### 4.2 Instance caps

Both demo services run with at most `demo_max_instance_count` instances (default 1) and min instances 0. Fault state and leaked DB connections live in one instance's memory, and `/admin/fault/reset` clears only the instance it reaches. The inventory API reads only `ATA_INVENTORY_*` variables (`ATA_INVENTORY_INSTANCE_CONNECTION_NAME`, `ATA_INVENTORY_DB_USER`, `ATA_INVENTORY_DB_NAME`, `ATA_INVENTORY_DB_IAM_AUTH`, `ATA_INVENTORY_MOCK_DB`).

`scripts/set-demo-instance-caps.sh` makes no changes unless you pass `--apply`. With it, the script sets:
- the **service-level** limit (`gcloud run services update --max`) on `<prefix>-inventory-api` and `<prefix>-orders-api`;
- the orders revision-level limits.

The service-level limit covers every revision, including a pinned revision created before the cap, and needs no new revision. Terraform does not set it. `ATA_DEMO_MAX_INSTANCES` (default 1) sets the limit; keep it equal to `demo_max_instance_count`. `scripts/scenarios/preflight.sh` fails if either service lacks the limit.

### 4.3 Re-pinning the inventory baseline and pruning revisions

The pin lives only in `scripts/scenarios/inventory-pin.env`: the revision, its image digest, and the previous pin (the rollback target). Do not edit the file by hand. Use `scripts/repin-inventory.sh`:

```bash
./scripts/repin-inventory.sh status                       # read-only: pin, traffic, baseline gaps, caps
./scripts/repin-inventory.sh prepare --image-tag v1.2.0   # build; deploy a candidate with NO traffic; verify; untag
#   (or: prepare --image-digest sha256:<hex> for an image already in ${AR_REPO})
./scripts/repin-inventory.sh write-pin --revision <candidate> --digest sha256:<hex>
git add scripts/scenarios/inventory-pin.env && git commit -m "chore(inventory): pin <candidate>"   # main stack only
./scripts/repin-inventory.sh switch                       # 100% to the pin; verify serving, /health, /items, admin probes
./scripts/reset-scenarios.sh                              # restock and full baseline check
# Rollback: ./scripts/repin-inventory.sh rollback  (traffic and pin file back to the previous pin)
```

`prepare` verifies the candidate before it ever serves traffic. On a fresh stack, use `--initial` ([Step 7](#step-7-demo-applications-and-the-inventory-pin)).

Every S5 run and every baseline restore creates an inventory revision. `scripts/prune-revisions.sh` makes no changes unless you pass `--apply`. It lists, and with `--apply` deletes, the inventory and orders revisions that have 0% traffic and no tag and are not the latest. It never deletes the pinned revision, the previous pin or any `--keep <revision>`. `--apply` runs only while inventory serves the pin.

### 4.4 Alert policies

Created by the observability root ([Step 10](#step-10-observability)):

1. `<prefix>-orders-5xx`: Cloud Run `<prefix>-orders-api` 5xx rate > 5% over 60s.
2. `<prefix>-orders-latency`: Cloud Run `<prefix>-orders-api` p99 latency > 2000 ms. The p99 is computed over 60s windows, with all revisions and response codes merged per service, and must hold for the `latency_duration_secs` retest window (default 240s). A single cold-start burst therefore does not open an alert.
3. `<prefix>-inventory-5xx`: Cloud Run `<prefix>-inventory-api` 5xx rate > 5% over 60s.
4. `<prefix>-inventory-latency`: as 2, for `<prefix>-inventory-api`.
5. `<prefix>-apigee-orders-5xx`: Apigee proxy `<prefix>-orders-v1` HTTP 500 count > 1.
6. `<prefix>-apigee-orders-429`: Apigee proxy `<prefix>-orders-v1` HTTP 429 (SpikeArrest) count > 1.
7. `<prefix>-cloudsql-connections-critical`: Cloud SQL `<prefix>-inventory-db` connection count > 20 (`cloudsql_connections_threshold`, about 80% of db-f1-micro's 25).
8. `<prefix>-cloudsql-cpu-high`: Cloud SQL `<prefix>-inventory-db` CPU utilization > 85%.

Every policy closes an open incident whose condition has received no data for `alert_auto_close_secs`. That Terraform variable defaults to 1800 s and must be at least 1800, Cloud Monitoring's 30-minute minimum.

### 4.5 Environment variables set by the deploy scripts

The deploy scripts use `gcloud run deploy --update-env-vars`: they set the variables listed below and keep every other variable already on the service. (`--set-env-vars` would replace the whole list, and drop, for example, `VERIFIER_SA` on a redeploy without it exported.) The bridge's secrets use `--update-secrets` for the same reason. Each image is pushed to `${AR_REPO}`.

URLs are never hardcoded. An exported `AGENT_URL` or `BRIDGE_URL` wins. Otherwise the script reads the service's `status.url`, and for a service that is not deployed yet it uses the deterministic URL `https://<service>-<project number>.<REGION>.run.app` and says so. If the services cannot be listed, the deploy stops.

| Service | Variable | Value |
| :--- | :--- | :--- |
| `<prefix>-agent` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. |
| | `INCIDENTS_BUCKET` | `${PROJECT_ID}-${NAME_PREFIX}-incidents` |
| | `INCIDENT_STORE_PATH` | `/knowledge/incident-manager` (the GCS volume mount) |
| | `BRIDGE_URL` | The bridge URL: the agent's ID-token audience for the bridge. |
| | `AGENT_URL` | The agent's own URL (the timer task target). |
| | `TOOLS_URL` | The tools service URL (MCP calls, ID-token audience). TODO(tools-lane): confirm the script sets it. |
| | `LOCATION` | `${REGION}` (the timer queue region) |
| | `TIMER_QUEUE_NAME` | `${NAME_PREFIX}-timer-queue` |
| | `SERVICE_ACCOUNT_EMAIL` | `${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com` (the OIDC identity of the timer tasks) |
| | `AUTO_RESOLVE_ON_RECOVERY` | Only when exported. |
| `<prefix>-bridge` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. |
| | `PROJECT_NUMBER` | Environment, else `gcloud projects describe`. Required. |
| | `INCIDENTS_BUCKET` | `${PROJECT_ID}-${NAME_PREFIX}-incidents` |
| | `TASKS_QUEUE` | `${NAME_PREFIX}-triage-queue` |
| | `AGENT_URL` | The agent URL (the `/triage` target of the dispatched tasks). |
| | `BRIDGE_URL` | The bridge's own `status.url`. Required. On a first deploy, the script deploys with the deterministic URL, then sets `status.url` with `gcloud run services update` if it differs. |
| | `SERVICENOW_INSTANCE_URL` | Environment, else `servicenow_instance_url` in `infra/terraform/foundation/terraform.tfvars`, else the value on the service. Required. |
| | `VERIFIER_SA` | Only when exported. The list is passed with gcloud's `^\|^` delimiter, so commas in it are kept. |
| | `WEBHOOK_TOKEN`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` | Secret Manager `${NAME_PREFIX}-webhook-token`, `${NAME_PREFIX}-servicenow-username`, `${NAME_PREFIX}-servicenow-password` (`:latest`). |
| `<prefix>-tools` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. Runs as `${NAME_PREFIX}-agent-sa`. |
| | `APIGEE_ORG` | `${APIGEE_ORG}`, default `${PROJECT_ID}` |
| | `APIGEE_ENV` | `${APIGEE_ENV}`. TODO(tools-lane): confirm the script sets it. |

The other deploy-script inputs:

| Input | Effect |
| :--- | :--- |
| `ENVIRONMENT`, `OWNER` | Labels (defaults `demo`, `sre-prototypes`). |
| `PROJECT_ID` (or `GCP_PROJECT`), `REGION` (or `GCP_REGION`) | **Required**. There are no defaults: the scripts exit before any gcloud call without them. |
| `PROJECT_NUMBER` | Looked up with `gcloud projects describe` if unset. The bridge deploy exits if the lookup fails. |
| `BRIDGE_URL`, `AGENT_URL` | Override the URLs that the scripts look up. |

`<prefix>-inventory-api` and `<prefix>-orders-api` are declared in the applications root, which lists their variables. Their settings are in [CONFIGURATION.md §1.4](CONFIGURATION.md#14-demo-applications).

### 4.6 Read-only ticket verification endpoint

The bridge exposes `GET /instructions/ticket/view/{group_id or sys_id}`, which returns ticket metadata without exposing ServiceNow credentials. The caller needs a Google-signed ID token that meets two conditions:
- its **audience** is the bridge URL (its `status.url` or its deterministic URL);
- its email is `<prefix>-agent-sa`, `<prefix>-bridge-sa`, or an entry in the bridge's `VERIFIER_SA`.

A plain `gcloud auth print-identity-token` user token has gcloud's own audience and is rejected with 403. Use a service account listed in `VERIFIER_SA` that you may impersonate (`roles/iam.serviceAccountTokenCreator` on it):

```bash
BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token --impersonate-service-account="<verifier-sa-email>" --audiences="${BRIDGE_URL}" --include-email)"
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BRIDGE_URL}/instructions/ticket/view/<group_id_or_sys_id>"
```

Returned fields: `number`, `state` (`1` New, `2` In Progress, `6` Resolved, `7` Closed), `short_description`, `caller`, `work_notes` and `attachments` (names and sizes). Credentials are never returned. See [CONFIGURATION.md §3.1](CONFIGURATION.md#31-read-only-ticket-verification-endpoint-get-instructionsticketviewgroup_id-or-sys_id).

---

## 5. Teardown

This removes one stack, identified by `NAME_PREFIX`, and nothing else. Run it in the stack's own clone and shell, with the inputs file loaded (§2.2), including `TF_VAR_inventory_image` and `TF_VAR_orders_image`: the applications root needs them even to destroy. The order matters:
- the Apigee proxies run as the Apigee SA, so they go first;
- the invoker bindings and the services go before the foundation;
- the incidents bucket must be empty before the foundation destroy (`force_destroy = false`);
- the state bucket goes last, because it holds every root's state.

Before you start, confirm the target:

```bash
cd "${ATA_DIR}"
echo "Tearing down NAME_PREFIX=${NAME_PREFIX} in ${PROJECT_ID}/${REGION}, Apigee ${APIGEE_ORG}/${APIGEE_ENV}"
grep -h '^prefix' infra/terraform/*/backend.hcl
```

Every `prefix` line must start with `"<prefix>/`. If the prefix is wrong, stop: you are in the wrong clone or shell.

**T1. Apigee resources.** Undeploy and delete the `<prefix>-*` proxies, then the app, developer, product and TargetServers. The script makes no changes by default: it shows the plan, and with `--apply` it asks you to type the prefix:

```bash
./scripts/teardown-apigee.sh
./scripts/teardown-apigee.sh --apply
```

Expected: it ends with a verification that no `<prefix>-*` Apigee resource is left (exit 0). It touches no other name. The Apigee organization, environment and envgroup are left in place.

**T2. Observability** (alert policies):

```bash
cd "${ATA_DIR}/infra/terraform/observability"
terraform init -reconfigure -backend-config=backend.hcl
terraform destroy
```

**T3. Triage invoker bindings, then the triage services** (created by the deploy scripts, not by Terraform):

```bash
cd "${ATA_DIR}/infra/terraform/triage-iam"
terraform init -reconfigure -backend-config=backend.hcl
terraform destroy
for s in bridge agent tools; do
  gcloud run services delete "${NAME_PREFIX}-${s}" --region="${REGION}" --project="${PROJECT_ID}" --quiet
done
```

**T4. Demo applications** (inventory and orders services, including every revision the re-pin and the scenarios created):

```bash
cd "${ATA_DIR}/infra/terraform/applications"
terraform init -reconfigure -backend-config=backend.hcl
terraform destroy
```

**T5. Database:**

```bash
cd "${ATA_DIR}/infra/terraform/database"
terraform init -reconfigure -backend-config=backend.hcl
terraform destroy
```

If the destroy fails at `google_sql_user.inventory_iam_user` with `role ... cannot be dropped because some objects depend on it` (the GRANT of Step 6), remove the user from the state and destroy again. Deleting the instance removes the user with it:

```bash
terraform state rm google_sql_user.inventory_iam_user
terraform destroy
```

A deleted Cloud SQL instance name cannot be reused for about a week.

**T6. Empty the incidents bucket,** which the foundation creates with `force_destroy = false`:

```bash
gcloud storage rm --recursive --all-versions "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/**" || echo "(already empty)"
```

The bucket has object versioning, and a bucket can only be deleted when no version is left, so `--all-versions` matters. Check with `gcloud storage ls --all-versions "gs://${PROJECT_ID}-${NAME_PREFIX}-incidents/**"`: the command must list nothing.

**T7. Foundation.** This removes the SAs, queues, secrets, buckets, the webhook channel, and the Artifact Registry repository `<prefix>-docker` with all its images. The project APIs are left enabled (`disable_on_destroy = false`), because other workloads may use them:

```bash
cd "${ATA_DIR}/infra/terraform/foundation"
terraform init -reconfigure -backend-config=backend.hcl
terraform destroy
```

If the destroy fails on the incidents bucket with `the bucket you tried to delete is not empty`, repeat T6 and run the destroy again. If it fails on the Artifact Registry repository, delete the repository with its images, then destroy again:

```bash
gcloud artifacts repositories delete "${NAME_PREFIX}-docker" --location="${REGION}" --project="${PROJECT_ID}" --quiet
terraform destroy
```

**T8. State bucket, last:**

```bash
gcloud storage rm --recursive "gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate"
```

This deletes every object version and the bucket. After it, the stack's Terraform state is gone: run it only once T1-T7 have succeeded.

**T9. Local files** in the clone: `.${NAME_PREFIX}-api-key`, `.${NAME_PREFIX}-apigee.env`, `infra/terraform/*/backend.hcl`, `infra/terraform/*/.terraform/` and `tfplan` files. Delete the clone, or:

```bash
cd "${ATA_DIR}"
rm -f ".${NAME_PREFIX}-api-key" ".${NAME_PREFIX}-apigee.env" infra/terraform/*/backend.hcl infra/terraform/*/tfplan
rm -rf infra/terraform/*/.terraform
```

What teardown leaves behind:
- the enabled APIs;
- the Cloud Build source archives in the project's shared `gs://<project>_cloudbuild` bucket;
- the log entries;
- if you granted roles to the default Cloud Build SA in Step 5, those grants.

None of them belongs to the stack by name.

---

## 6. Verify that nothing is left

Run these in the stack's shell. Each command must print nothing (the Apigee lines print `OK`). `~^<prefix>-` matches names that **start with** the prefix. With the prefix `ata`, that excludes `atat-*`.

```bash
P="${NAME_PREFIX}"
gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" --format='value(metadata.name)' --filter="metadata.name~^${P}-"
gcloud sql instances list --project="${PROJECT_ID}" --format='value(name)' --filter="name~^${P}-"
gcloud tasks queues list --project="${PROJECT_ID}" --location="${REGION}" --format='value(name)' --filter="name~/queues/${P}-"
gcloud secrets list --project="${PROJECT_ID}" --format='value(name)' --filter="name~/secrets/${P}-"
gcloud storage buckets list --project="${PROJECT_ID}" --format='value(name)' --filter="name~^${PROJECT_ID}-${P}-"
gcloud iam service-accounts list --project="${PROJECT_ID}" --format='value(email)' --filter="email~^${P}-"
gcloud artifacts repositories list --project="${PROJECT_ID}" --location="${REGION}" --format='value(name)' --filter="name~/repositories/${P}-"
gcloud monitoring policies list --project="${PROJECT_ID}" --format='value(displayName)' --filter="displayName~^${P}-"
TOKEN="$(gcloud auth print-access-token)"
curl -sS -H "Authorization: Bearer ${TOKEN}" "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/notificationChannels" \
  | grep -E "\"displayName\": \"${P}-"

API="https://apigee.googleapis.com/v1/organizations/${APIGEE_ORG}"
for path in apis apiproducts developers "environments/${APIGEE_ENV}/targetservers" "environments/${APIGEE_ENV}/deployments"; do
  curl -sS -H "Authorization: Bearer ${TOKEN}" "${API}/${path}" | grep -E "\"${P}-" || echo "OK: no ${P}-* in ${path}"
done
```

The last command checks the proxies, API products, developers (`<prefix>-dev@example.com`), TargetServers and deployments. If anything is listed, delete it with the matching teardown step, then run the checks again.
