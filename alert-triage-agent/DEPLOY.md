# Alert Triage Agent: Deployment Guide

This guide is the single procedure for deploying a complete **Alert Triage Agent (ATA)** stack into a Google Cloud project from scratch, and for removing it again. It works for any resource name prefix (`NAME_PREFIX`), including a second stack next to an existing one in the same project and Apigee organization.

The deployment is one script, `scripts/deploy-stage1.sh`, made of 14 phases. You can run them all at once ([§3.1](#31-one-shot-run)), or one at a time with `--only <phase>` ([§3.2](#32-step-by-step)), checking each phase before the next. This guide follows the step-by-step path: each step states the phase's purpose, the command, the expected output, how to verify it, and what to do if it fails. The two paths run the same code.

Every command is meant to be copied and pasted as it is, in bash 3.2 or later (the macOS default `/bin/bash`) or on Linux. Nothing outside this guide is needed.

---

## Contents

- [1. Architecture](#1-architecture)
- [2. Before you start](#2-before-you-start)
  - [2.1 Operator prerequisites](#21-operator-prerequisites)
  - [2.2 Inputs](#22-inputs)
  - [2.3 Second stack in an existing project and Apigee organization](#23-second-stack-in-an-existing-project-and-apigee-organization)
- [3. Deployment](#3-deployment)
  - [3.1 One-shot run](#31-one-shot-run)
  - [3.2 Step by step](#32-step-by-step)
  - [Step 0: Project checks and authentication](#step-0-project-checks-and-authentication)
  - [Step 1: Apigee organization, environment and hostname](#step-1-apigee-organization-environment-and-hostname)
  - [Step 2: preflight](#step-2-preflight)
  - [Step 3: bootstrap](#step-3-bootstrap)
  - [Step 4: foundation](#step-4-foundation)
  - [Step 5: secrets](#step-5-secrets)
  - [Step 6: images](#step-6-images)
  - [Step 7: database](#step-7-database)
  - [Step 8: schema](#step-8-schema)
  - [Step 9: applications](#step-9-applications)
  - [Step 10: inventory-pin](#step-10-inventory-pin)
  - [Step 11: triage](#step-11-triage)
  - [Step 12: triage-iam](#step-12-triage-iam)
  - [Step 13: apigee](#step-13-apigee)
  - [Step 14: observability](#step-14-observability)
  - [Step 15: verify](#step-15-verify)
  - [Step 16: Scenarios S1 and S2](#step-16-scenarios-s1-and-s2)
  - [Step 17: Reset](#step-17-reset)
- [4. Operations reference](#4-operations-reference)
- [5. Teardown](#5-teardown)
- [6. Verify that nothing is left](#6-verify-that-nothing-is-left)

---

## 1. Architecture

Every resource name below starts with the stack's `NAME_PREFIX` (shown as `<prefix>`; there is no default, the live stack uses `ata`). Terraform needs `-var name_prefix=<prefix>` (or `TF_VAR_name_prefix`) on every plan/apply/destroy; `deploy-stage1.sh` and `teardown-stack.sh` pass it.

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
| Google Cloud SDK | A recent release (480.0.0 or later). No alpha or beta commands are needed. | `gcloud version` |
| Terraform | **1.9.0 or later** (all five roots require `>= 1.9.0`; the `preflight` phase checks it) | `terraform version` |
| python3 | Any supported Python 3. The operator scripts use the standard library only. | `python3 --version` |
| curl | Any recent version | `curl --version` |
| psql | The PostgreSQL client, for the `schema` phase. macOS: `brew install libpq` and add its `bin` to `PATH`. Debian/Ubuntu: `apt install postgresql-client`. | `psql --version` |
| cloud-sql-proxy | **v2**, for the `schema` phase: `gcloud components install cloud-sql-proxy`, or the binary from the [Cloud SQL Auth Proxy page](https://cloud.google.com/sql/docs/postgres/sql-proxy). | `cloud-sql-proxy --version` |
| Docker | **Not needed.** Every image is built with Cloud Build. | n/a |
| jq | **Not needed.** | n/a |
| Credentials | `gcloud auth login` **and** `gcloud auth application-default login` (the schema proxy, the reset and the preflight use ADC). | [Step 0](#step-0-project-checks-and-authentication) |
| Deployer IAM | `roles/owner` on the project, or **all** the roles in [README.md: Prerequisites: deployer IAM roles](README.md#prerequisites-deployer-iam-roles): `roles/editor`, `roles/resourcemanager.projectIamAdmin`, `roles/iam.roleAdmin` (the foundation creates the custom role `<prefix>ApigeeDeployReader`), `roles/run.admin`, `roles/iam.serviceAccountAdmin`, `roles/cloudtasks.admin`, `roles/artifactregistry.admin`, `roles/secretmanager.admin` and `roles/cloudsql.admin`. Editor alone cannot set IAM policies on service accounts, queues, secrets or repositories, or create custom roles, which the foundation needs. The README has a one-command grant. Also list yourself in the foundation variable `deployer_members` ([Step 4](#step-4-foundation)) for `roles/iam.serviceAccountUser` on the build and Apigee SAs. | [Step 0](#step-0-project-checks-and-authentication) |
| Apigee IAM | `roles/apigee.admin` on the Apigee organization's project (`APIGEE_ORG`): it creates TargetServers, proxies, the API product, developer and app. Provisioning a new organization needs Owner on that project. | [Step 1](#step-1-apigee-organization-environment-and-hostname) |
| ServiceNow | An instance URL (`https://...`) and an integration user with the `itil` role (incident and attachment tables). A free Personal Developer Instance works. | Log in to the instance. |
| macOS only | If Python reports `CERTIFICATE_VERIFY_FAILED`: `export SSL_CERT_FILE=/etc/ssl/cert.pem` | n/a |

### 2.2 Inputs

Every phase reads the same exported variables, and there are **no defaults** for the target: `deploy-stage1.sh` stops if one is missing. Use **one shell per stack**, and keep the values in a file outside the repository so that you can load them again in a new shell, and for teardown.

1. Clone the repository. Use a **separate clone for every stack** ([§2.3](#23-second-stack-in-an-existing-project-and-apigee-organization)). Replace `<prefix>`:

   ```bash
   git clone https://github.com/ssvaidyanathan/autocloud-prototypes.git autocloud-prototypes-<prefix>
   cd autocloud-prototypes-<prefix>/alert-triage-agent
   ```

2. Write the inputs file. Replace every `<...>` value before you paste:

   ```bash
   cat > "${HOME}/ata-stack.env" <<'EOF'
   # --- Target (required) ---
   export PROJECT_ID="<project-id>"
   export REGION="<region>"                    # e.g. us-west1
   export NAME_PREFIX="<prefix>"               # 2-10 chars, [a-z][a-z0-9]*; e.g. ata
   export ENVIRONMENT="demo"                   # env label
   export OWNER="sre-prototypes"               # owner label
   # --- ServiceNow (required; the URL is configuration, not a secret) ---
   export SERVICENOW_INSTANCE_URL="https://<instance>.service-now.com"
   export SERVICENOW_USERNAME="<integration-user>"   # the password is prompted for, never stored here
   # --- Apigee (APIGEE_ENV and APIGEE_HOST required; APIGEE_ORG defaults to PROJECT_ID) ---
   export APIGEE_ORG="<apigee-org>"
   export APIGEE_ENV="<apigee-env>"            # e.g. prod
   export APIGEE_HOST="<envgroup-hostname>"    # e.g. 203.0.113.10.nip.io
   # --- Optional ---
   # export VERIFIER_SA="<email>,<email>"      # extra callers of the bridge /instructions/* (emails only)
   EOF
   ```

   Rename the file per stack if you run several, for example `${HOME}/ata-stack-atat.env`.

3. Load it. **Run this block in every new shell before any step**, from the `alert-triage-agent/` directory of the stack's clone:

   ```bash
   . "${HOME}/ata-stack.env"
   export ATA_DIR="$(pwd)"
   export PROJECT_NUMBER="$(gcloud projects describe "${PROJECT_ID}" --format='value(projectNumber)')"
   gcloud config set project "${PROJECT_ID}"
   test -f "${ATA_DIR}/scripts/deploy-stage1.sh" && echo "OK: ${NAME_PREFIX} in ${PROJECT_ID}/${REGION} (project number ${PROJECT_NUMBER})"
   ```

   **Expected:** `OK: <prefix> in <project>/<region> (project number <digits>)`. If `PROJECT_NUMBER` is empty, you have no access to the project or the ID is wrong.

| Input | Used by |
| :--- | :--- |
| `PROJECT_ID`, `REGION`, `NAME_PREFIX`, `ENVIRONMENT`, `OWNER` | Every phase. `ENVIRONMENT` and `OWNER` are labels. |
| `SERVICENOW_INSTANCE_URL` | `triage` (bridge). Must start with `https://`. |
| `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` | `secrets`. Each one that is not exported is prompted for; the password is read without echo. Values are never printed. |
| `APIGEE_ORG`, `APIGEE_ENV`, `APIGEE_HOST` | `triage` (tools), `apigee`, `verify`, the scenarios and the teardown. |
| `VERIFIER_SA` | `triage` (bridge), only when exported ([§4.6](#46-read-only-ticket-verification-endpoint)). `VERIFIER_SA=none` removes it from the service. |
| Optional | `AUTO_RESOLVE_ON_RECOVERY`, `MODEL_NAME`, `GEMINI_LOCATION`, `CHANGE_ONSET_WINDOW_MINUTES`, `CHANGE_HEALTHY_PERIOD_MINUTES` (agent, passed only when exported); `IMAGE_TAG` (image tag; default: the git commit); `TFSTATE_BUCKET` (custom state bucket name); `PGPASSWORD` (postgres password for `schema`; default: a new random one); `SCHEMA_PROXY_PORT` (default 54329); `ATA_STACK_DIR` (directory of the stack-local files `.<prefix>-api-key`, `.<prefix>-apigee.env` and `.deploy-state/`; default `alert-triage-agent/`); `DEPLOY_STATE_DIR` (default `${ATA_STACK_DIR}/.deploy-state/`). |
| Extra Terraform variables | For example `deployer_members`: put them in a `terraform.tfvars` in that root, which Terraform loads automatically. Or export `TF_VAR_<name>`, which every root reads. |
| `TF_VAR_deletion_protection` | Optional, `applications` only. Default `true`. Set `false` only for a test or throwaway stack ([§2.3](#23-second-stack-in-an-existing-project-and-apigee-organization)). |

### 2.3 Second stack in an existing project and Apigee organization

A second stack, for example `NAME_PREFIX=atat` next to a live `ata`, can share the project and the Apigee organization. Every resource name, Terraform state prefix, Apigee proxy, base path, TargetServer, product, developer and app derives from `NAME_PREFIX`, so the stacks do not collide. Apigee matches base paths per path segment, so `/ata/...` never captures `/atat/...`.

Rules:

- **Reuse** the organization, environment and hostname: set `APIGEE_ORG`, `APIGEE_ENV` and `APIGEE_HOST` to the existing values. Skip the provisioning part of [Step 1](#step-1-apigee-organization-environment-and-hostname). Run only its checks.
- **Use a new prefix.** It must not be in use: Step 1 checks Apigee, and [§6](#6-verify-that-nothing-is-left) lists anything in the project that already starts with the prefix.
- **Use a separate clone per stack.** Each Terraform root has one `backend.hcl` and one `.terraform/` directory per checkout. `.deploy-state/` and the `.<prefix>-*` key files also live in the clone.
- **Never share a checkout's `.terraform/` between prefixes.** Do not copy `.terraform/` or `backend.hcl` from another clone. Every Terraform phase runs `terraform init -reconfigure -backend-config=backend.hcl` and refuses a `backend.hcl` that belongs to another prefix or bucket.
- **Use a separate shell with its own inputs file.**
- Each stack has its own pin file, `scripts/scenarios/inventory-pin.<prefix>.env` ([Step 10](#step-10-inventory-pin)). Commit a stack's pin file only if that stack is meant to be tracked in the repository.
- **Review each Terraform phase's plan first.** The phases apply with `-auto-approve`. In a shared project, plan the root yourself and check that every named resource starts with `<prefix>-` (buckets: `<project>-<prefix>-`) and that every IAM member is a `<prefix>-*-sa` (or you, from `deployer_members`). The `google_project_service` entries only adopt APIs that are already enabled (`disable_on_destroy = false`). Run the phase only when the plan is clean:

  ```bash
  R=foundation   # or database, applications, triage-iam, observability
  (cd "infra/terraform/${R}" && terraform init -input=false -reconfigure -backend-config=backend.hcl >/dev/null && \
    terraform plan -input=false -var project_id="${PROJECT_ID}" -var region="${REGION}" -var name_prefix="${NAME_PREFIX}" \
      $(case "${R}" in foundation|database|applications) echo "-var environment=${ENVIRONMENT} -var owner=${OWNER}";; esac) \
    | grep -E '^  # |^Plan:')
  ```

  `applications` also needs the image variables that the phase passes. Use the phase's own output for that root.
- Both stacks may use the same ServiceNow instance. Their tickets then share a queue and differ only in the group IDs and the `<prefix>-*` names in the text.
- **Deletion protection.** The applications root's variable `deletion_protection` (default `true`) protects `<prefix>-inventory-api` and `<prefix>-orders-api` from `terraform destroy`. Keep the default for a stack that stays. A test or throwaway stack can set it to `false`: put `export TF_VAR_deletion_protection=false` in its inputs file, or `deletion_protection = false` in `infra/terraform/applications/terraform.tfvars`. `deploy-stage1.sh` does not pass the variable, so either one takes effect. The teardown does not need it: it sets `false` itself before the destroy ([§5](#5-teardown)).

---

## 3. Deployment

All commands start from `${ATA_DIR}`, with the inputs loaded ([§2.2](#22-inputs)). Do [Step 0](#step-0-project-checks-and-authentication) and [Step 1](#step-1-apigee-organization-environment-and-hostname) first on either path: they are manual checks that no phase can do for you.

### 3.1 One-shot run

```bash
cd "${ATA_DIR}"
./scripts/deploy-stage1.sh
```

The script prints the target and the phase list, asks once for confirmation (`--yes` skips it; without a terminal it requires `--yes`), then runs all 14 phases in order. After each phase it prints `--- Check (<phase>) ---` with read-only commands for that phase, then `=== Phase <phase>: done ===`. The `secrets` phase prompts for the ServiceNow password unless `SERVICENOW_PASSWORD` is exported. A full run takes about 45-60 minutes. The Cloud SQL instance alone takes 5-15.

If a phase fails, the script stops there. Fix the cause using that phase's "If it fails" in [§3.2](#32-step-by-step), then resume:

```bash
./scripts/deploy-stage1.sh --from <phase>
```

`--to <phase>` stops after a phase, and `--list-phases` prints the phases in order. After the run, continue with [Step 16](#step-16-scenarios-s1-and-s2).

### 3.2 Step by step

Each step below runs one phase:

```bash
./scripts/deploy-stage1.sh --only <phase>
```

Each run prints the inputs, asks for confirmation (add `--yes` to skip it), runs the phase and prints its check commands. The phases must run in the order below: a later phase reads what an earlier one created. For example, the image refs from `images` are kept in `.deploy-state/${PROJECT_ID}-${NAME_PREFIX}-images.env` for `applications`, `inventory-pin` and `triage`.

| Step | Phase | Takes about |
| :--- | :--- | :--- |
| 0 | (manual) project checks and authentication | 5 min |
| 1 | (manual) Apigee organization, environment and hostname | 5 min to check; 30-90 min to provision. Start it first. |
| 2 | `preflight` | seconds |
| 3 | `bootstrap` | 1 min |
| 4 | `foundation` | 3-5 min |
| 5 | `secrets` | 1 min |
| 6 | `images` | 5-10 min |
| 7 | `database` | 5-15 min |
| 8 | `schema` | 1 min |
| 9 | `applications` | 2 min |
| 10 | `inventory-pin` | 3 min |
| 11 | `triage` | 5 min |
| 12 | `triage-iam` | 1 min |
| 13 | `apigee` | 3 min |
| 14 | `observability` | 1 min |
| 15 | `verify` | 2 min |
| 16 | (manual) scenarios S1 and S2 | 40-50 min |
| 17 | (manual) reset | 5-15 min |

---

### Step 0: Project checks and authentication

**Purpose:** confirm that the account, billing and the organization policies allow the deployment, before anything is created.

```bash
cd "${ATA_DIR}"
gcloud auth login
gcloud auth application-default login

gcloud billing projects describe "${PROJECT_ID}" --billing-project="${PROJECT_ID}" --format='value(billingEnabled)'
gcloud projects get-iam-policy "${PROJECT_ID}" --flatten='bindings[].members' \
  --filter="bindings.members:$(gcloud config get-value account 2>/dev/null)" --format='value(bindings.role)'

for c in iam.allowedPolicyMemberDomains sql.restrictPublicIp iam.automaticIamGrantsForDefaultServiceAccounts run.allowedIngress; do
  echo "== ${c}"
  gcloud resource-manager org-policies describe "constraints/${c}" --project="${PROJECT_ID}" --effective 2>&1 | grep -E 'booleanPolicy|enforced|allValues|allowedValues|deniedValues|constraint' || echo "(not set)"
done
```

**Expected:**
- `billingEnabled` is `True`.
- Your roles include `roles/owner` (or the set in [§2.1](#21-operator-prerequisites)).
- The organization policies:

| Constraint | Must be | Why |
| :--- | :--- | :--- |
| `iam.allowedPolicyMemberDomains` | not set, or allowing `allUsers` | The bridge is deployed with `--allow-unauthenticated`, so that the Cloud Monitoring webhook can reach it. The webhook authenticates with HTTP Basic Auth. |
| `sql.restrictPublicIp` | not enforced | The database root creates a public IP instance. There is no private IP option in this repository. |
| `run.allowedIngress` | not set, or allowing `all` | All services are deployed with ingress `all`. Access is controlled by Cloud Run IAM. |
| `iam.automaticIamGrantsForDefaultServiceAccounts` | either | Builds run as the stack's own build SA (`<prefix>-build-sa`), not the default Cloud Build SA. |

**If it fails:**
- The billing command fails with `Cloud Billing API has not been used in project <number>`: gcloud bills the call to your account's own project. Keep `--billing-project="${PROJECT_ID}"` as above (the target project has the API enabled), or enable `cloudbilling.googleapis.com` there.
- Billing `False`: link a billing account (`gcloud billing projects link "${PROJECT_ID}" --billing-account=<id>`).
- A blocking policy: ask the organization admin for a project-level exception. The repository has no workaround for `sql.restrictPublicIp` or `iam.allowedPolicyMemberDomains`.

---

### Step 1: Apigee organization, environment and hostname

**Purpose:** the `apigee` phase ([Step 13](#step-13-apigee)) needs an Apigee organization `APIGEE_ORG` with an environment `APIGEE_ENV`, attached to an instance and to an environment group whose hostname is `APIGEE_HOST`. Nothing in this repository creates them.

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

Provision Apigee in the project before Step 13. This takes 30-90 minutes, so start it now and continue with Step 2 meanwhile.

1. In the Cloud console, open **Apigee** in project `${APIGEE_ORG}` and follow the provisioning wizard. An evaluation organization is free for 60 days. Choose:
   - the runtime region, normally `${REGION}`;
   - an environment named `${APIGEE_ENV}`;
   - an environment group with the external load balancer. The wizard offers an external HTTPS load balancer with a Google-managed IP and a `<IP>.nip.io` hostname. That hostname is your `APIGEE_HOST`.
2. Wait until the wizard reports the instance, the environment and the environment group as ready, then run the checks in 1a.
3. Write `APIGEE_HOST` into the inputs file and load it again.

The wizard creates the network peering, the instance, the environment, the environment group and the load balancer. Note the load balancer's running cost (tens of USD per month). The teardown ([§5](#5-teardown)) does not delete the organization.

---

### Step 2: preflight

**Purpose:** check that the tools are on `PATH` (gcloud, terraform >= 1.9, curl, python3, and psql and cloud-sql-proxy whenever `schema` is among the selected phases), that a gcloud account is active, that ADC works, and that the project is readable. Read-only.

```bash
./scripts/deploy-stage1.sh --only preflight
```

**Expected:** `project <project> (number <digits>) is readable; tools and credentials are in place.`

**If it fails:** the message names the missing tool or credential. `--only preflight` checks psql and cloud-sql-proxy only when `schema` is selected, so run `psql --version; cloud-sql-proxy --version` yourself.

---

### Step 3: bootstrap

**Purpose:** `scripts/bootstrap-tfstate.sh` enables the APIs Terraform needs before it can run (serviceusage, cloudresourcemanager, storage, iam), creates the state bucket `gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate` (versioned, uniform access, public access prevention), and writes `backend.hcl` (prefix `${NAME_PREFIX}/<root>`) into all five roots: foundation, database, applications, observability and triage-iam.

```bash
./scripts/deploy-stage1.sh --only bootstrap
```

**Expected:** `Created .../backend.hcl (prefix: <prefix>/<root>)` for each of the five roots, then `SUCCESS: Terraform state bucket gs://<project>-<prefix>-tfstate configured.`

**Verify:**

```bash
grep -h '' infra/terraform/*/backend.hcl
gcloud storage buckets describe "gs://${PROJECT_ID}-${NAME_PREFIX}-tfstate" --format='value(name,versioning_enabled)'
```

Each `backend.hcl` names bucket `<project>-<prefix>-tfstate` and prefix `<prefix>/<root>`. The bucket has versioning `True`.

**If it fails:**
- `NAME_PREFIX ... is invalid`: use 2-10 lowercase letters or digits, starting with a letter.
- `gcloud services enable` is denied: you need `serviceusage.services.enable` (Owner or Editor).
- Bucket creation fails with a name conflict: bucket names are global. Export `TFSTATE_BUCKET=<name>` (add it to the inputs file) and run the phase again.

---

### Step 4: foundation

**Purpose:** Terraform root `infra/terraform/foundation` creates everything the services need before they run:
- the project APIs;
- six service accounts: `<prefix>-bridge-sa`, `<prefix>-agent-sa`, `<prefix>-inventory-sa`, `<prefix>-orders-sa`, `<prefix>-apigee-sa` and `<prefix>-build-sa`, with their IAM ([CONFIGURATION.md §3](CONFIGURATION.md#3-iam-roles--least-privilege-matrix));
- the incidents bucket, the build bucket and the Artifact Registry repository `<prefix>-docker`;
- the triage and timer queues;
- the webhook token and the ServiceNow placeholder secrets;
- the Cloud Monitoring webhook channel, at the bridge's deterministic URL.

If you deploy without Owner, first add your principal in `infra/terraform/foundation/terraform.tfvars`: `deployer_members = ["user:you@example.com"]`.

```bash
./scripts/deploy-stage1.sh --only foundation
```

**Expected:** `Apply complete!` with only additions on a fresh stack, and outputs including `bridge_webhook_url`, `project_number` and `build_sa_id`.

**Verify:**

```bash
terraform -chdir=infra/terraform/foundation output -raw bridge_webhook_url; echo
echo "expected: https://${NAME_PREFIX}-bridge-${PROJECT_NUMBER}.${REGION}.run.app/webhook/monitoring"
gcloud iam service-accounts list --project="${PROJECT_ID}" --filter="email~^${NAME_PREFIX}-" --format='value(email)'
gcloud tasks queues list --location="${REGION}" --project="${PROJECT_ID}" --filter="name~/queues/${NAME_PREFIX}-" --format='value(name)'
gcloud secrets list --project="${PROJECT_ID}" --filter="name~/secrets/${NAME_PREFIX}-" --format='value(name)'
```

- The two URLs are equal.
- Six SAs: `<prefix>-{agent,apigee,bridge,build,inventory,orders}-sa`. There is no tools SA: the tools service runs as `<prefix>-agent-sa`.
- Two queues: `<prefix>-triage-queue` and `<prefix>-timer-queue`.
- Three secrets: `<prefix>-webhook-token`, `<prefix>-servicenow-username` and `<prefix>-servicenow-password`.

**If it fails:**
- `API ... has not been used in project ... or it is disabled`: the APIs enabled by this same apply take a minute or two to propagate. Wait, then run the phase again.
- `Permission 'iam.serviceAccounts.setIamPolicy' denied`, or `lacks IAM permission "cloudtasks.queues.getIamPolicy"` (or the same for secrets or the repository): you are not Owner and lack one of the admin roles in [§2.1](#21-operator-prerequisites) ([README.md: Prerequisites: deployer IAM roles](README.md#prerequisites-deployer-iam-roles)). The resources are created, only their IAM bindings are missing. Get the roles, then run the phase again.
- `Error acquiring the state lock`: another run holds the lock. Wait for it. Use `terraform force-unlock <id>` only if you are sure no other run is active.
- `backend.hcl ... is not for NAME_PREFIX`: you are in another stack's clone or shell, or bootstrap ran with other inputs. Check the inputs and rerun `--only bootstrap`.
- A `409 already exists` on a `<prefix>-*` resource: the prefix is in use, or an earlier stack was not fully removed. Run the checks in [§6](#6-verify-that-nothing-is-left).

---

### Step 5: secrets

**Purpose:** `scripts/set-secrets.sh --yes --skip-if-set` replaces the `placeholder` versions that the foundation created with the real ServiceNow username and password. It must run **before** the bridge starts ([Step 11](#step-11-triage)): the bridge mounts the secrets as `:latest` environment variables, which are resolved when an instance starts. `--skip-if-set` leaves a secret alone when its latest version is already real.

```bash
./scripts/deploy-stage1.sh --only secrets
```

It takes `SERVICENOW_USERNAME` from the inputs and prompts for the password (no echo), unless `SERVICENOW_PASSWORD` is exported. An empty answer is an error.

**Expected:** each secret gets a new version, or is reported as already set. No value is printed.

**Verify:**

```bash
for s in servicenow-username servicenow-password; do
  gcloud secrets versions list "${NAME_PREFIX}-${s}" --project="${PROJECT_ID}" --filter='state=ENABLED' --format='value(name)' | wc -l
done
```

Each secret has 2 enabled versions: the placeholder and yours.

**If it fails:**
- `apply foundation first`: the secrets do not exist for this `NAME_PREFIX` and `PROJECT_ID`. Check your inputs, or run Step 4.
- `re-run with --yes` or a missing prompt: the phase needs a terminal for the password, or an exported `SERVICENOW_PASSWORD`.
- To replace credentials that are already set, run `./scripts/set-secrets.sh` yourself (without `--skip-if-set`), then redeploy the bridge (`./scripts/deploy-stage1.sh --only triage`).

---

### Step 6: images

**Purpose:** `scripts/build-image.sh` builds the five images with Cloud Build, as `<prefix>-build-sa`, with source and logs in `gs://<project>-<prefix>-build`: `<prefix>-inventory-api`, `<prefix>-orders-api`, `<prefix>-agent`, `<prefix>-bridge` and `<prefix>-tools`. They go into `${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker`. The tag is `IMAGE_TAG`, else the git commit. The phase records each image by digest in `.deploy-state/${PROJECT_ID}-${NAME_PREFIX}-images.env`. Every later phase deploys exactly these digests.

```bash
./scripts/deploy-stage1.sh --only images
```

**Expected:** five builds, each ending with a line `<VAR>_IMAGE=<repo>/<name>@sha256:<64 hex>`, then `image refs written to .../.deploy-state/<project>-<prefix>-images.env`.

**Verify:**

```bash
cat ".deploy-state/${PROJECT_ID}-${NAME_PREFIX}-images.env"
gcloud artifacts docker images list "${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker" --include-tags --format='value(package,version,tags)'
```

Five `*_IMAGE=...@sha256:...` lines, and the same five packages in the repository.

**If it fails:**
- `PERMISSION_DENIED` acting as `<prefix>-build-sa`: you are not Owner and not in `deployer_members` (Step 4).
- A build error: the Cloud Build log is in `gs://<project>-<prefix>-build/logs`, and the script prints the build ID.
- `bad digest`: the image was pushed but its digest could not be read. Run the phase again.

To rebuild a single image, run `./scripts/build-image.sh <service-dir> <image-name>`. It prints exactly one line, `IMAGE=<repo>/<image-name>@sha256:<hex>`. Then export the matching `*_IMAGE` variable, which wins over the images file.

---

### Step 7: database

**Purpose:** Terraform root `infra/terraform/database` creates the Cloud SQL PostgreSQL 15 instance `<prefix>-inventory-db` (db-f1-micro, IAM database authentication), the `inventory` database and the IAM database user `<prefix>-inventory-sa@<project>.iam`.

```bash
./scripts/deploy-stage1.sh --only database
```

**Expected:** `Apply complete! Resources: 3 added`. The instance takes 5-15 minutes.

**Verify:**

```bash
gcloud sql instances describe "${NAME_PREFIX}-inventory-db" --project="${PROJECT_ID}" --format='value(state,settings.tier)'
```

`RUNNABLE db-f1-micro`.

**If it fails:** `instance name ... already used` or `cannot be reused`: Cloud SQL reserves a deleted instance's name for about a week. Use another `NAME_PREFIX`, or wait.

---

### Step 8: schema

**Purpose:** create and seed the `inventory_items` table, and grant the IAM database user `SELECT, UPDATE` on it. Without the GRANT, every `/items` request returns 500.

The phase works in three steps:
1. It sets a new random password for the built-in `postgres` user (not printed), unless `PGPASSWORD` is exported.
2. It starts `cloud-sql-proxy` v2 on `127.0.0.1:${SCHEMA_PROXY_PORT:-54329}`, with your ADC, through the instance's public IP. No authorized network is needed.
3. It runs `psql -v ON_ERROR_STOP=1 -v iam_user=<prefix>-inventory-sa@<project>.iam -f demo/db/schema.sql`.

```bash
./scripts/deploy-stage1.sh --only schema
```

**Expected:** psql prints `CREATE TABLE`, `INSERT 0 5` and `GRANT`, then `schema applied; <prefix>-inventory-sa@<project>.iam has SELECT, UPDATE on inventory_items.`

**Verify:**

```bash
gcloud sql users list --instance="${NAME_PREFIX}-inventory-db" --project="${PROJECT_ID}" --format='value(name,type)'
```

It lists the user `<prefix>-inventory-sa@<project>.iam` of type `CLOUD_IAM_SERVICE_ACCOUNT`. The application path (`/items` with the GRANT) is checked in Step 10.

**If it fails:**
- `psql not on PATH` or `cloud-sql-proxy (v2) not on PATH`: install them ([§2.1](#21-operator-prerequisites)).
- `cloud-sql-proxy exited before it was ready`: the log above it shows why. Usually ADC is missing (`gcloud auth application-default login`), or the port is in use (export another `SCHEMA_PROXY_PORT`).
- `relation "inventory_items" already exists`: harmless. The schema uses `CREATE TABLE IF NOT EXISTS` and upserts the seed rows, so you can run the phase again.

---

### Step 9: applications

**Purpose:** Terraform root `infra/terraform/applications` deploys `<prefix>-inventory-api` and `<prefix>-orders-api` on Cloud Run from the digests built in Step 6, with their invoker IAM (orders to inventory, Apigee SA to both). Both run with at most `demo_max_instance_count` instances (default 1). Both have `deletion_protection = true` unless `TF_VAR_deletion_protection=false` is set ([§2.3](#23-second-stack-in-an-existing-project-and-apigee-organization)).

```bash
./scripts/deploy-stage1.sh --only applications
```

**Expected:** `Apply complete! Resources: 5 added` (two Cloud Run services and three invoker bindings).

After the apply, the phase runs `scripts/set-demo-instance-caps.sh --apply`. Google provider 6.x resets the service-level max instance count of a service it updates to 100, and Terraform cannot set that limit. The script restores it, makes no change when the caps are in place, and creates no inventory revision. It runs after every applications apply, including on a stack that is already pinned.

**Verify:**

```bash
gcloud run services list --project="${PROJECT_ID}" --region="${REGION}" --filter="metadata.name~^${NAME_PREFIX}-" --format='value(metadata.name,status.url)'
```

It lists `<prefix>-inventory-api` and `<prefix>-orders-api` with URLs.

**The inventory image once the stack has a pin.** The phase decides whether the stack's pin serves here by checking that the pinned revision (`PIN_INVENTORY_REVISION` of `scripts/scenarios/inventory-pin.<prefix>.env`) exists in `${PROJECT_ID}/${REGION}`, not by the file existing. The file is per prefix and committed, and a teardown keeps it. With no pin file (a new prefix), the phase passes the image built in Step 6, and Step 10 pins it. With a pin file whose revision is not in this project (a fresh project, or a redeploy after a teardown, including `ata`), the phase also passes the built image and prints `WARNING: the inventory pin file ... is not a revision in ...`; Step 10 (`prepare --initial`, which accepts exactly this case) then pins the new build and rewrites the file. Commit it. Once the pinned revision exists, the phase always passes the pinned image (`<repo>/<prefix>-inventory-api@<PIN_INVENTORY_DIGEST>`) and prints `inventory_image: the pinned image ...`. If `INVENTORY_IMAGE` (exported, or in the images file) names another image, for example a rebuild from Step 6 or an images file from before a re-pin, the phase stops before Terraform runs with `INVENTORY_IMAGE disagrees with the inventory pin` and both values. Change the inventory image only with `scripts/repin-inventory.sh` ([§4.3](#43-re-pinning-the-inventory-baseline-and-pruning-revisions)). The applications root ignores the pinned revision's name, so while a pin serves, Terraform cannot apply any change to the inventory template: not the image, the env, `inventory_mock_db` or `demo_max_instance_count`. The plan shows the change, and the apply fails with `revision ... with different configuration`. Make such a change through a re-pin, whose candidate gets the new image and `ATA_DEMO_MAX_INSTANCES`. Then keep the Terraform variables equal to what the pin runs.

**If it fails:**
- `Run the images phase (--only images) or export them`: the images file is missing, for example because the images were built in another clone. Run Step 6.
- `INVENTORY_IMAGE disagrees with the inventory pin`: nothing was applied. To keep the current pin, unset `INVENTORY_IMAGE`, and set the `INVENTORY_IMAGE=` line of `.deploy-state/${PROJECT_ID}-${NAME_PREFIX}-images.env` to the pinned image the message prints, or delete that line. To serve the new image, re-pin ([§4.3](#43-re-pinning-the-inventory-baseline-and-pruning-revisions)).
- `cannot read the inventory pin ..., or cannot tell whether it serves`: nothing was applied. Either the pin file is invalid (the error above names the line), or `gcloud run revisions describe` failed with an error other than not-found (permissions, network). Fix the cause and re-run the phase.
- `set-demo-instance-caps.sh --apply failed`: Terraform has applied, and the service-level caps may now be 100. Fix the cause, then run `scripts/set-demo-instance-caps.sh --apply` (its dry run without `--apply` shows what it would change).
- `revision ... with different configuration` on `<prefix>-inventory-api`: a Terraform change to the inventory template while a pin serves (see above). Re-pin instead, or revert the variable.
- `service account ... does not exist`: Step 4 did not complete for this prefix.

---

### Step 10: inventory-pin

**Purpose:** establish the first **inventory pin**. Scenarios, reset and preflight restore and check inventory traffic against one pinned revision. The phase runs `scripts/repin-inventory.sh` three times:
1. `prepare --initial --image-digest <inventory digest>` deploys a candidate revision with no traffic and checks it: the digest, the `ATA_INVENTORY_*` env, the instance limits and Ready. It then sends GET requests through a temporary tag: `/health`, `/items` (which proves the IAM login and the GRANT of Step 8) and the admin probes.
2. `write-pin --initial` writes the stack's pin file.
3. `switch` moves 100% of the traffic to the pinned revision and verifies it.

```bash
./scripts/deploy-stage1.sh --only inventory-pin
```

**Expected:** `Candidate verified: <prefix>-inventory-api-<nnnnn>-<xxx> (sha256:...)`, then the pin written and the switch verified, and finally `pinned <revision> (<digest>)`.

**Re-runs.** If the stack is already pinned, meaning the pinned revision exists in this project and serves 100% of the traffic, the phase prints `already pinned: <revision>; skipping` and changes nothing, so a full re-run of `deploy-stage1.sh` completes. The applications phase has already restored the instance caps. A pin file whose revision is not in this project, from another deployment of the prefix, is pinned again as above.

**Verify:**

```bash
./scripts/repin-inventory.sh status
```

It exits 0 and reports traffic 100% on the pin, with no baseline gaps.

The pin file is `scripts/scenarios/inventory-pin.<prefix>.env`; the end-of-phase message names it. Check it with `grep '^PIN_' scripts/scenarios/inventory-pin.${NAME_PREFIX}.env`. Commit it only if the stack is tracked in the repository ([§2.3](#23-second-stack-in-an-existing-project-and-apigee-organization)); a temporary stack's pin file stays local.

The phase then caps the demo services at the service level (the default is 1 instance; see [§4.2](#42-instance-caps)) by running `./scripts/set-demo-instance-caps.sh --apply`. If only that step fails, the pin is in place: fix the cause, re-run that command alone, and resume with `--from triage`.

**If it fails:**
- `is pinned to <revision> ..., but ... traffic is '...', not <revision>=100`: the pin exists but does not serve all traffic, so nothing changed. Restore it with `./scripts/reset-scenarios.sh` (or `s5_bad_deploy.sh --revert`), or finish an interrupted re-pin with `./scripts/repin-inventory.sh switch`. `./scripts/repin-inventory.sh status` shows the state.
- `is already pinned ..., but INVENTORY_IMAGE is not the pinned image`: nothing changed. Re-pin to serve the new image ([§4.3](#43-re-pinning-the-inventory-baseline-and-pruning-revisions)), or set the images file's `INVENTORY_IMAGE` line to the pinned image.
- `--initial: a pin exists`: this stack's pin file names a revision that exists in this project, so the stack is already pinned. Re-pin without `--initial` ([§4.3](#43-re-pinning-the-inventory-baseline-and-pruning-revisions)), or check `NAME_PREFIX` and `PROJECT_ID`.
- `/items` fails in `prepare`: the schema or the GRANT is missing (Step 8), or the instance is not `RUNNABLE` yet.
- `could not read the candidate revision`: `prepare` succeeded but its output changed. Run `./scripts/repin-inventory.sh write-pin --initial --revision <candidate> --digest <digest>`, then `./scripts/repin-inventory.sh switch`, using the values from the `Candidate verified` line.

---

### Step 11: triage

**Purpose:** deploy the triage tier from the digests built in Step 6, in this order: `tools/scripts/deploy.sh`, `bridge/scripts/deploy.sh`, `agent/scripts/deploy.sh`. Each script resolves the other services' URLs: an exported value wins, then the service's `status.url`, then the deterministic URL `https://<service>-${PROJECT_NUMBER}.${REGION}.run.app`. The variables they set are listed in [§4.5](#45-environment-variables-set-by-the-deploy-scripts):
- **agent:** `TOOLS_URL` (the agent does not start without it), `BRIDGE_URL`, and its timer queue and timer SA;
- **tools:** `APIGEE_ORG`, `APIGEE_ENV`, `EXPECTED_AUDIENCE` (the agent's `TOOLS_URL`), and `ENFORCE_AUTH` / `VERIFY_OIDC` set to `true`;
- **bridge:** `SERVICENOW_INSTANCE_URL`, `BRIDGE_URL`, and `VERIFIER_SA` when it is exported.

```bash
./scripts/deploy-stage1.sh --only triage
```

**Expected:** `Successfully deployed <prefix>-tools: https://...`, then the same for the bridge and the agent. On a first deploy the bridge and tools scripts also update `BRIDGE_URL` and `EXPECTED_AUDIENCE` to the service's real `status.url`.

**Verify:**

```bash
for s in tools agent bridge; do
  gcloud run services describe "${NAME_PREFIX}-${s}" --region="${REGION}" --project="${PROJECT_ID}" \
    --format='value(metadata.name,status.url,spec.template.spec.serviceAccountName)'
done
BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
curl -sS "${BRIDGE_URL}/health"; echo
```

- The three services are listed with URLs. The agent and tools run as `<prefix>-agent-sa`, and the bridge as `<prefix>-bridge-sa`.
- The bridge `/health` returns a JSON status.

The agent-to-bridge and agent-to-tools calls need Step 12. Step 15 checks the full configuration.

**If it fails:**
- `SERVICENOW_INSTANCE_URL is required` or `APIGEE_ENV` missing: load the inputs.
- A revision fails to start, and the logs (`gcloud run services logs read <service> --region="${REGION}" --limit=50`) show a `RuntimeError` or a validation error naming `BRIDGE_URL`, `PROJECT_NUMBER`, `TOOLS_URL` or `VERIFIER_SA`: fix the input (`VERIFIER_SA` takes emails only), then run the phase again. Cloud Run keeps the previous revision serving.
- `image ... is not in this stack's repository`: an exported `*_IMAGE` points at another stack. Unset it.

---

### Step 12: triage-iam

**Purpose:** Terraform root `infra/terraform/triage-iam` holds the service-level `roles/run.invoker` bindings between the triage services:
- bridge SA to agent (triage tasks);
- agent SA to agent (timer wakes);
- agent SA to bridge (ticket instructions);
- agent SA to tools (MCP).

It must run after Step 11, because the services must exist.

```bash
./scripts/deploy-stage1.sh --only triage-iam
```

**Expected:** `Apply complete! Resources: 4 added`.

**Verify:**

```bash
for s in agent bridge tools; do
  echo "== ${NAME_PREFIX}-${s}"
  gcloud run services get-iam-policy "${NAME_PREFIX}-${s}" --region="${REGION}" --project="${PROJECT_ID}" \
    --flatten='bindings[].members' --filter='bindings.role:roles/run.invoker' --format='value(bindings.members)'
done
```

- agent: `<prefix>-bridge-sa` and `<prefix>-agent-sa`.
- bridge: `<prefix>-agent-sa` and `allUsers` (the webhook; set by the bridge deploy).
- tools: `<prefix>-agent-sa`.

**If it fails:** `Error 404 ... service not found`: one of the three services is missing. Run Step 11 again.

---

### Step 13: apigee

**Purpose:** `scripts/deploy-apigee-proxies.sh` deploys the stack's Apigee resources, touching only `<prefix>-*` names:
- TargetServers `<prefix>-orders-target` and `<prefix>-inventory-target`, pointing at the stack's own Cloud Run URLs;
- proxies `<prefix>-orders-v1` (`/<prefix>/orders/v1`) and `<prefix>-inventory-v1` (`/<prefix>/inventory/v1`), running as `<prefix>-apigee-sa`;
- the API product `<prefix>-product`, the developer `<prefix>-dev@example.com` and the app `<prefix>-demo-app`.

The script prints the exact list before it changes anything, and stops on any non-2xx management API response.

It is safe to re-run, and `scripts/reset-scenarios.sh` runs it on every reset. A proxy is imported as a new revision and deployed only when the rendered bundle differs from the revision deployed in `APIGEE_ENV`. The deployed revision's bundle (`GET .../revisions/<n>?format=bundle`) is compared file by file; in the root `apiproxy/<proxy>.xml`, the metadata Apigee regenerates on export is ignored. An unchanged proxy is logged (`<proxy>: deployed revision N matches the rendered bundle; not redeployed.`), so it adds no revision and no deployment to the agent's change window. If the check cannot decide (the read fails, the proxy is not deployed, it has several deployments, or a different service account, or it is not `READY`), the script prints a `WARNING` and imports and deploys anyway. TargetServers, the product and the developer are read first and not written when they already hold the wanted values.

```bash
./scripts/deploy-stage1.sh --only apigee
```

**Expected:** each proxy reaches `READY`. The script writes two local files in `ATA_STACK_DIR` (default `alert-triage-agent/`), both ignored by Git:
- `.<prefix>-api-key`: the app's consumer key (mode 600);
- `.<prefix>-apigee.env`: `APIGEE_HOST`, `APIGEE_ORG` and `APIGEE_ENV`.

**Verify:**

```bash
curl -sS -o /dev/null -w 'no key: %{http_code}\n' -k "https://${APIGEE_HOST}/${NAME_PREFIX}/orders/v1/orders/test"
curl -sS -o /dev/null -w 'with key: %{http_code}\n' -k -H "x-api-key: $(cat ".${NAME_PREFIX}-api-key")" "https://${APIGEE_HOST}/${NAME_PREFIX}/inventory/v1/items"
```

`no key: 401` and `with key: 200`.

**If it fails:**
- `gcloud run services describe <prefix>-orders-api failed` or `has no status.url`: Step 9 did not complete.
- 403 from `apigee.googleapis.com`: you lack Apigee admin on `APIGEE_ORG` ([§2.1](#21-operator-prerequisites)).
- The deployment does not become `READY`, or fails on the service account: check that `<prefix>-apigee-sa` exists (Step 4) and that you may act as it (Owner, or `deployer_members`).
- To remove only this step's resources: `./scripts/teardown-apigee.sh --apply`.

---

### Step 14: observability

**Purpose:** Terraform root `infra/terraform/observability` creates the eight Cloud Monitoring alert policies `<prefix>-*` ([§4.4](#44-alert-policies)). They notify the foundation's webhook channel `<prefix>-bridge-webhook`, which the root looks up by name. The phase runs after the services exist, so that cold starts during the deployment do not page.

```bash
./scripts/deploy-stage1.sh --only observability
```

**Expected:** `Apply complete! Resources: 8 added`.

**Verify:**

```bash
gcloud monitoring policies list --project="${PROJECT_ID}" --filter="displayName~^${NAME_PREFIX}-" \
  --format='value(displayName,enabled,notificationChannels)'
curl -sS -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  "https://monitoring.googleapis.com/v3/projects/${PROJECT_ID}/notificationChannels" \
  | python3 -c 'import json,os,sys; [print(c["name"], c.get("labels", {}).get("url")) for c in json.load(sys.stdin).get("notificationChannels", []) if c.get("displayName") == os.environ["NAME_PREFIX"] + "-bridge-webhook"]'
```

There are eight `<prefix>-*` policies, all enabled, each notifying the one channel printed by the second command. That channel's URL is the stack's own bridge.

**If it fails:** an error looking up `<prefix>-bridge-webhook` means the foundation was not applied for this prefix.

---

### Step 15: verify

**Purpose:** two read-only checks of the whole stack.
- `scripts/verify-stage1.sh`:
  - `/health` of the five services;
  - the bridge webhook rejects a wrong token (401) and answers an out-of-scope probe sent with the real token with `200 ignored`, so nothing is enqueued;
  - the agent's `BRIDGE_URL`, `TOOLS_URL` and `SERVICE_ACCOUNT_EMAIL` point at this stack;
  - the newest incident document, if any, has the current schema.
- `scripts/verify-demo-infra.sh`: the demo services directly and through Apigee at `/<prefix>/...` (the key and `APIGEE_HOST` come from the `.<prefix>-*` files of Step 13).

Neither creates an incident or a ticket.

```bash
./scripts/deploy-stage1.sh --only verify
```

**Expected:** both scripts finish without a failure. `verify-demo-infra.sh` lists any request slower than 2000 ms (a cold start). If it does, run Step 17 with `--alert-window-from-start` before the first scenario.

**If it fails:** each failed check names the request and the status. For 5xx from inventory, go back to Steps 8-10. For 401 or 404 through Apigee, Step 13. For a 403 between triage services, Step 12. For a foreign `BRIDGE_URL` or `TOOLS_URL` on the agent, Step 11.

Optional end-to-end check of the ticket path, without a scenario:

```bash
./scripts/verify-stage1.sh --with-ticket
```

It sends one `<prefix>-inventory-5xx` alert (expects **202**), waits for its incident document, checks it, then sends the matching `closed` alert. It **creates a real ServiceNow ticket** after the settle timer. Close the ticket in ServiceNow, then run Step 17.

---

### Step 16: Scenarios S1 and S2

**Purpose:** prove the triage itself. Each scenario must produce one incident group and one ServiceNow ticket with the expected origin.

> The scenario scripts take the prefix from `NAME_PREFIX` (required, with `PROJECT_ID` and `REGION`), read the Apigee values from `.<prefix>-apigee.env` (parsed, not sourced) and the key from `.<prefix>-api-key`.

```bash
./scripts/scenarios/preflight.sh
./scripts/run-all-scenarios.sh --scenario s1
./scripts/run-all-scenarios.sh --scenario s2
```

Each run takes about 15-25 minutes. That covers the traffic, the alerts opening, the settle timer and the investigation, followed by the reset. [DEMO.md](DEMO.md) has the timings and the expected result of each scenario.

**Expected:** the preflight passes:
- the demo services and their admin endpoints;
- Apigee and SpikeArrest;
- the alert policies;
- Cloud SQL;
- the pin and the caps.

Each scenario's summary reports PASS. Then check the results and the tickets:

```bash
./scripts/scenarios/check-result.sh --scenario s1 --bucket "${PROJECT_ID}-${NAME_PREFIX}-incidents"
./scripts/scenarios/check-result.sh --scenario s2 --bucket "${PROJECT_ID}-${NAME_PREFIX}-incidents"
```

The checker prints, for each incident group, the origin, the ticket number (`INC...`), the alert count and the blast radius, and compares them with the expected values. Open each ticket in the ServiceNow instance (`${SERVICENOW_INSTANCE_URL}`) and check three things:
- the short description names a `<prefix>-*` component;
- the work notes and the HTML attachment are present;
- there is one ticket per scenario.

There is no separate automated test-suite count to compare against. The deployment is verified when Steps 15 and 16 pass.

**If it fails:**
- No ticket, and the group stays open: check the agent logs (`gcloud run services logs read "${NAME_PREFIX}-agent" --region="${REGION}" --project="${PROJECT_ID}" --limit=100`). A `PERMISSION_DENIED` on Cloud Tasks or `actAs` points to Step 4, and a 403 on the agent points to Step 12.
- A ticket that says `change timeline unavailable: <reason>` (Change-Induced "Not assessed"): the change timeline from the tools service failed. The reason names the cause:
  - `returned HTTP 403` from `<prefix>-tools`: the agent-to-tools binding is missing (Step 12), or `EXPECTED_AUDIENCE` on the tools service is not the agent's `TOOLS_URL` (run Step 11 again);
  - `unreachable` or `timed out`: check `TOOLS_URL` on the agent and that `<prefix>-tools` is serving;
  - `Apigee deployment history source failed (APIGEE_ENV not set in ata-tools)`: run Step 11 again with `APIGEE_ENV` exported;
  - `Apigee deployments list returned HTTP 403`: `<prefix>-agent-sa` lacks the custom role `<prefix>ApigeeDeployReader` (`apigee.deployments.list`, `apigee.proxyrevisions.get`) in the `APIGEE_ORG` project. The foundation creates and grants it in `PROJECT_ID` only. Do not grant `roles/apigee.readOnlyAdmin` instead: it also reads every developer app key in the org.
- The bridge logs show ServiceNow 401: the secrets still hold `placeholder`, or the credentials are wrong. Run `./scripts/set-secrets.sh`, then `./scripts/deploy-stage1.sh --only triage`, so that a new bridge revision reads them.

---

### Step 17: Reset

**Purpose:** return the demo to its baseline after the scenarios: faults cleared, traffic on the pinned inventory revision, stock restored, open alerts closed, and terminal incident groups archived.

```bash
./scripts/reset-scenarios.sh
```

**Expected:** it ends with a success line after the alert, incident-group and quiet-period waits (5-15 minutes; a reset after S4 takes longer). It uses ADC for Cloud Monitoring.

**If it fails:** it names the check that failed. See [README.md §14](README.md#14-testing-guide--post-deploy-smoke-tests) and [DEMO.md](DEMO.md) for the reset options. On macOS, `CERTIFICATE_VERIFY_FAILED` needs `export SSL_CERT_FILE=/etc/ssl/cert.pem`.

---

## 4. Operations reference

### 4.1 Applying queue-only changes

When you change only a queue (its `retry_config`, or the `triage_queue_*` or `timer_queue_*` variables), target that queue resource:

```bash
cd "${ATA_DIR}/infra/terraform/foundation"
terraform init -reconfigure -backend-config=backend.hcl
TFV="-var project_id=${PROJECT_ID} -var region=${REGION} -var name_prefix=${NAME_PREFIX} -var environment=${ENVIRONMENT} -var owner=${OWNER}"
# Triage queue:
terraform plan ${TFV} -target=google_cloud_tasks_queue.triage_queue -out=tfplan
# Timer queue:
terraform plan ${TFV} -target=google_cloud_tasks_queue.timer_queue -out=tfplan
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

Each stack's pin lives only in `scripts/scenarios/inventory-pin.<prefix>.env`: the revision, its image digest, and the previous pin (the rollback target). Do not edit the file by hand. Use `scripts/repin-inventory.sh`:

```bash
./scripts/repin-inventory.sh status                       # read-only: pin, traffic, baseline gaps, caps
./scripts/repin-inventory.sh prepare --image-tag v1.2.0   # build; deploy a candidate with NO traffic; verify; untag
#   (or: prepare --image-digest sha256:<hex> for an image already in ${REGION}-docker.pkg.dev/${PROJECT_ID}/${NAME_PREFIX}-docker)
./scripts/repin-inventory.sh write-pin --revision <candidate> --digest sha256:<hex>
git add "scripts/scenarios/inventory-pin.${NAME_PREFIX}.env" && git commit -m "chore(inventory): pin <candidate>"   # tracked stacks only
./scripts/repin-inventory.sh switch                       # 100% to the pin; verify serving, /health, /items, admin probes
./scripts/reset-scenarios.sh                              # restock and full baseline check
# Rollback: ./scripts/repin-inventory.sh rollback  (traffic and pin file back to the previous pin)
```

`prepare` verifies the candidate before it ever serves traffic. On a fresh stack, use `--initial` ([Step 10](#step-10-inventory-pin)).

Every S5 run and every baseline restore creates an inventory revision. `scripts/prune-revisions.sh` makes no changes unless you pass `--apply`. It lists, and with `--apply` deletes, the inventory and orders revisions that have 0% traffic and no tag and are not the latest. It never deletes the pinned revision, the previous pin or any `--keep <revision>`. `--apply` runs only while inventory serves the pin.

### 4.4 Alert policies

Created by the observability root ([Step 14](#step-14-observability)):

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

`tools/`, `bridge/` and `agent/scripts/deploy.sh` (run by the `triage` phase, or alone with `--yes`) deploy with `gcloud run deploy --update-env-vars`. They set the variables listed below and keep every other variable already on the service. (`--set-env-vars` would replace the whole list.) The bridge's secrets use `--update-secrets` for the same reason.

Each script deploys the image given in `TOOLS_IMAGE` / `BRIDGE_IMAGE` / `AGENT_IMAGE`, which `deploy-stage1.sh` takes from the images file. The image must be in this stack's repository, or the deploy is refused. Without one, the script builds the image itself with the build SA. All three label the service with `app`, `stack=<prefix>`, `component`, `role`, `tier`, `env`, `managed-by=script` and `owner`.

URLs are never hardcoded. An exported `AGENT_URL` or `BRIDGE_URL` wins. Otherwise the script reads the service's `status.url`, and for a service that is not deployed yet it uses the deterministic URL `https://<service>-<project number>.<REGION>.run.app` and says so. If the services cannot be listed, the deploy stops. Every URL, exported or derived, must be this stack's service (`https://<prefix>-<service>-...`); a URL left in the shell from another stack's deploy stops the deploy. For a custom domain, export `ALLOW_CUSTOM_SERVICE_URLS=true` as well (the script says it accepted it).

| Service | Variable | Value |
| :--- | :--- | :--- |
| `<prefix>-agent` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. |
| | `INCIDENTS_BUCKET` | `${PROJECT_ID}-${NAME_PREFIX}-incidents` |
| | `INCIDENT_STORE_PATH` | `/knowledge/incident-manager` (the GCS volume mount) |
| | `BRIDGE_URL` | The bridge URL: the agent's ID-token audience for the bridge. |
| | `AGENT_URL` | The agent's own URL (the timer task target). |
| | `TOOLS_URL` | The tools service's `status.url`, else `https://${NAME_PREFIX}-tools-${PROJECT_NUMBER}.${REGION}.run.app`. Bare `https://host`, no path. Required: the agent does not start without it. |
| | `LOCATION` | `${REGION}` (the timer queue region) |
| | `TIMER_QUEUE_NAME` | `${NAME_PREFIX}-timer-queue` |
| | `SERVICE_ACCOUNT_EMAIL` | `${NAME_PREFIX}-agent-sa@${PROJECT_ID}.iam.gserviceaccount.com` (the OIDC identity of the timer tasks) |
| | `IS_LOCAL` | Always `false`. |
| | `AUTO_RESOLVE_ON_RECOVERY`, `MODEL_NAME`, `GEMINI_LOCATION`, `TOOLS_TIMEOUT_SECONDS`, `TOOLS_RETRY_ATTEMPTS`, `TOOLS_RETRY_BASE_SECONDS`, `CHANGE_ONSET_WINDOW_MINUTES`, `CHANGE_HEALTHY_PERIOD_MINUTES` | Only when exported ([CONFIGURATION.md §1.2](CONFIGURATION.md#12-multi-agent-adk-runtime-ata-agent)). |
| `<prefix>-bridge` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. |
| | `PROJECT_NUMBER` | Environment, else `gcloud projects describe`. Required. |
| | `INCIDENTS_BUCKET` | `${PROJECT_ID}-${NAME_PREFIX}-incidents` |
| | `TASKS_QUEUE` | `${NAME_PREFIX}-triage-queue` |
| | `AGENT_URL` | The agent URL (the `/triage` target of the dispatched tasks). |
| | `BRIDGE_URL` | The bridge's own `status.url`. Required. On a first deploy, the script deploys with the deterministic URL, then sets `status.url` with `gcloud run services update`. An exported `BRIDGE_URL` is never replaced. |
| | `SERVICENOW_INSTANCE_URL` | From the environment (logged), else the value already on the service (logged). Required: the deploy fails if neither exists. |
| | `VERIFIER_SA` | Only when exported. The list is passed with gcloud's `^\|^` delimiter, so commas in it are kept. `VERIFIER_SA=none` removes it from the service (`--remove-env-vars=VERIFIER_SA`). Entries must be email addresses; the bridge lowercases them and refuses to start on any other entry. |
| | `IS_LOCAL` | Always `false`. |
| | `WEBHOOK_TOKEN`, `SERVICENOW_USERNAME`, `SERVICENOW_PASSWORD` | Secret Manager `${NAME_PREFIX}-webhook-token`, `${NAME_PREFIX}-servicenow-username`, `${NAME_PREFIX}-servicenow-password` (`:latest`). |
| `<prefix>-tools` | `PROJECT_ID`, `NAME_PREFIX`, `REGION` | From the environment. Runs as `${NAME_PREFIX}-agent-sa`. |
| | `PROJECT_NUMBER` | As for the bridge. It makes the deterministic tools URL an accepted audience. |
| | `APIGEE_ORG` | `${APIGEE_ORG}`, default `${PROJECT_ID}` (echoed). |
| | `APIGEE_ENV` | `${APIGEE_ENV}`. Required. |
| | `EXPECTED_AUDIENCE` | The service's own URL, which is exactly what the agent resolves as `TOOLS_URL`. On a first deploy it starts as the deterministic URL and is updated to `status.url`. |
| | `ENFORCE_AUTH`, `VERIFY_OIDC` | `true`: the tools service also verifies the caller's ID token. Cloud Run IAM is the primary gate. |
| | `IS_LOCAL` | Always `false`. |
| | `ALLOWED_SERVICE_ACCOUNTS` | Only when exported. The app's default is `${NAME_PREFIX}-agent-sa`. |

The other deploy-script inputs:

| Input | Effect |
| :--- | :--- |
| `PROJECT_ID` (or `GCP_PROJECT`), `REGION` (or `GCP_REGION`), `NAME_PREFIX` | **Required**. There are no defaults: the scripts exit before any gcloud call without them. |
| `ENVIRONMENT`, `OWNER` | Labels (defaults `demo`, `sre-prototypes`, echoed). |
| `--yes` (or `ASSUME_YES=true`) | Skips the confirmation prompt. Without a terminal, the scripts require it. |

`<prefix>-inventory-api` and `<prefix>-orders-api` are declared in the applications root, which lists their variables. Their settings are in [CONFIGURATION.md §1.4](CONFIGURATION.md#14-demo-applications).

### 4.6 Read-only ticket verification endpoint

The bridge exposes `GET /instructions/ticket/view/{group_id or sys_id}`, which returns ticket metadata without exposing ServiceNow credentials. The caller needs a Google-signed ID token that meets two conditions:
- its **audience** is the bridge URL (its `status.url` or its deterministic URL);
- its email is `<prefix>-agent-sa`, `<prefix>-bridge-sa`, or an entry in the bridge's `VERIFIER_SA`.

A plain `gcloud auth print-identity-token` user token has gcloud's own audience and is rejected with 403. Use a service account listed in `VERIFIER_SA` that you may impersonate (`roles/iam.serviceAccountTokenCreator` on it). `VERIFIER_SA` entries must be email addresses; the bridge lowercases them:

```bash
BRIDGE_URL="$(gcloud run services describe "${NAME_PREFIX}-bridge" --region="${REGION}" --project="${PROJECT_ID}" --format='value(status.url)')"
TOKEN="$(gcloud auth print-identity-token --impersonate-service-account="<verifier-sa-email>" --audiences="${BRIDGE_URL}" --include-email)"
curl -sS -H "Authorization: Bearer ${TOKEN}" "${BRIDGE_URL}/instructions/ticket/view/<group_id_or_sys_id>"
```

Returned fields: `number`, `state` (`1` New, `2` In Progress, `6` Resolved, `7` Closed), `short_description`, `caller`, `work_notes` and `attachments` (names and sizes). Credentials are never returned. See [CONFIGURATION.md §3.1](CONFIGURATION.md#31-read-only-ticket-verification-endpoint-get-instructionsticketviewgroup_id-or-sys_id).

---


## 5. Teardown

`scripts/teardown-stack.sh` removes one stack, identified by `NAME_PREFIX`, and nothing else. Its guard accepts only names that start with `<prefix>-` (buckets: `<project>-<prefix>-`), and it checks every `backend.hcl` for the prefix and bucket before any destroy. Run it in the stack's own clone and shell, with the inputs loaded ([§2.2](#22-inputs)).

It works in this order, so that each step can complete:
1. `teardown-apigee.sh --apply --yes`: the `<prefix>-*` proxies (every deployed revision undeployed, then deleted), app, developer, product and TargetServers. The proxies run as the Apigee SA, so they go first.
2. `terraform destroy` of observability, then triage-iam.
3. Delete the Cloud Run services `<prefix>-agent`, `<prefix>-bridge` and `<prefix>-tools`. The deploy scripts created them, not Terraform.
4. `terraform destroy` of applications (the inventory and orders services, with every revision the re-pin and the scenarios created), then database. First, the services' deletion protection is turned off. The root defaults `deletion_protection` to `true`, and the provider refuses to destroy a service while its state holds `true`. The script reads the two services from the refreshed state. For each one still protected, it runs a targeted `terraform plan -target google_cloud_run_v2_service.<name> -var deletion_protection=false`, using the images, labels and settings the service has now. The plan is printed. Then the script checks it: it must change nothing but `deletion_protection` (`true` to `false`) on those two addresses. If it does, the script applies it; if not, it stops before any destroy. The provider sends the service's whole configuration with that update, so a plan that changed anything else would also change the live service. The apply writes `false` into the state. This also works for a stack applied before the variable existed: its state holds the provider default `true`, and the targeted apply replaces it the same way.
5. Empty `gs://<project>-<prefix>-incidents` and `gs://<project>-<prefix>-build`, all versions. The foundation creates the incidents bucket with `force_destroy = false`, so it must be empty before step 7.
6. Delete every package in the Artifact Registry repository `<prefix>-docker`.
7. `terraform destroy` of foundation: the SAs, queues, secrets, buckets, the webhook channel and the repository. The project APIs stay enabled (`disable_on_destroy = false`), because other workloads may use them.
8. With `--delete-state` only: delete the state bucket `gs://<project>-<prefix>-tfstate`, last, because it holds every root's state.
9. A "nothing left" check over Cloud Run, Cloud SQL, Cloud Tasks queues, secrets, buckets, service accounts, Artifact Registry repositories, alert policies and notification channels named `<prefix>-*`. It exits 1 if anything remains; without `--delete-state`, the state bucket is expected.

**T1. Dry run:**

```bash
cd "${ATA_DIR}"
./scripts/teardown-stack.sh
```

It prints every step and changes nothing. Check that every name it lists starts with `<prefix>-` or `<project>-<prefix>-`.

**T2. Apply:**

```bash
./scripts/teardown-stack.sh --apply --delete-state
```

It asks you to type `NAME_PREFIX` (`--confirm-prefix <prefix>` instead makes it non-interactive). Use `--skip-apigee` if the stack has no Apigee resources. **Expected:** each step reports its deletions, and the final check finds nothing (exit 0).

**If it fails:** the script stops at the failing step with the error. Fix the cause, then run T2 again. With the prefix `atat`, an `ata-*` resource is never touched: the guard refuses it.
- An Apigee step fails with 403: you lack Apigee admin on `APIGEE_ORG`.
- The database destroy fails at `google_sql_user.inventory_iam_user` with `role ... cannot be dropped because some objects depend on it` (the GRANT of Step 8). Remove the user from the state; deleting the instance removes the user with it. Then run T2 again:

  ```bash
  (cd infra/terraform/database && terraform init -reconfigure -backend-config=backend.hcl && terraform state rm google_sql_user.inventory_iam_user)
  ```

- The deletion protection step stops with `the deletion_protection plan changes more than deletion_protection`: the services' live configuration differs from what the root would plan, for example an environment variable that a `gcloud run services update` added. Or the applications destroy fails with `cannot destroy service without setting deletion_protection=false`: the teardown that ran has no deletion protection step, for example a clone from before the variable was added. The state then still holds `true`. In either case, delete the two services, which also removes their invoker bindings, and drop the root's five resources from the state. Then run T2 again:

  ```bash
  for s in inventory-api orders-api; do
    gcloud run services delete "${NAME_PREFIX}-${s}" --project="${PROJECT_ID}" --region="${REGION}" --quiet
  done
  (cd infra/terraform/applications && terraform init -reconfigure -backend-config=backend.hcl && \
    terraform state rm google_cloud_run_v2_service.inventory_api google_cloud_run_v2_service.orders_api \
      google_cloud_run_v2_service_iam_member.inventory_invoker_orders \
      google_cloud_run_v2_service_iam_member.inventory_invoker_apigee \
      google_cloud_run_v2_service_iam_member.orders_invoker_apigee)
  ```

- The foundation destroy fails with `the bucket you tried to delete is not empty`: an object was written after step 5, for example by a late alert. Run T2 again.
- The final check lists a resource: delete it by hand (the check names it), then run [§6](#6-verify-that-nothing-is-left).

**T3. Local files** in the clone: `.deploy-state/`, `.<prefix>-api-key`, `.<prefix>-apigee.env`, `infra/terraform/*/backend.hcl` and `infra/terraform/*/.terraform/` (the first three in `ATA_STACK_DIR` if you set it). The teardown leaves them alone. Delete the clone, or:

```bash
cd "${ATA_DIR}"
rm -rf .deploy-state infra/terraform/*/.terraform
rm -f ".${NAME_PREFIX}-api-key" ".${NAME_PREFIX}-apigee.env" infra/terraform/*/backend.hcl
```

What teardown leaves behind:
- the enabled APIs;
- the log entries;
- the Apigee organization, environment and envgroup.

A deleted Cloud SQL instance name stays reserved for about a week, so a redeploy with the same `NAME_PREFIX` within that window fails at the `database` phase.

---

## 6. Verify that nothing is left

This is an independent check, which also works before a deploy to confirm that a prefix is unused. Run these in the stack's shell. Each command must print nothing (the Apigee lines print `OK`). `~^<prefix>-` matches names that **start with** the prefix. With the prefix `ata`, that excludes `atat-*`.

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

The last loop checks the proxies, API products, developers (`<prefix>-dev@example.com`), TargetServers and deployments. If anything is listed, run the teardown again or delete it by hand, then repeat the checks.
