data "google_project" "this" {
  project_id = var.project_id
}

locals {
  # Every Google API the stack calls: Terraform itself, the deploy scripts
  # (Cloud Build, Artifact Registry, Cloud Run), the runtime services (Cloud
  # Tasks, Secret Manager, Cloud SQL Admin for the connector, Monitoring,
  # Logging, Storage, IAM Credentials for ID tokens), Vertex AI (agent gray-zone
  # scoring) and Apigee (proxies, agent change specialist).
  required_services = toset([
    "aiplatform.googleapis.com",
    "apigee.googleapis.com",
    "artifactregistry.googleapis.com",
    "cloudbuild.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "cloudtasks.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "logging.googleapis.com",
    "monitoring.googleapis.com",
    "run.googleapis.com",
    "secretmanager.googleapis.com",
    "serviceusage.googleapis.com",
    "sqladmin.googleapis.com",
    "storage.googleapis.com",
  ])

  common_labels = {
    app        = "alert-triage-agent"
    stack      = var.name_prefix
    role       = "triage-system"
    tier       = "platform"
    env        = var.environment
    managed-by = "terraform"
    owner      = var.owner
  }
}

# 0. Project APIs. disable_on_destroy = false: a destroy of this stack must not
# switch off APIs that other workloads in the project may use.
resource "google_project_service" "required" {
  for_each = local.required_services

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

# 1. GCS Bucket for Incidents (OKF Markdown State Store)
resource "google_storage_bucket" "incidents" {
  depends_on = [google_project_service.required]

  name                        = "${var.project_id}-${var.name_prefix}-incidents"
  location                    = var.region
  uniform_bucket_level_access = true
  force_destroy               = false

  versioning {
    enabled = true
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-agent"
  })
}

# 2. Cloud Tasks Queue (Serialized Triage Queue)
# Note: Cloud Tasks queues do not support GCP resource labels.
resource "google_cloud_tasks_queue" "triage_queue" {
  depends_on = [google_project_service.required]

  name     = "${var.name_prefix}-triage-queue"
  location = var.region

  rate_limits {
    max_concurrent_dispatches = 1
    max_dispatches_per_second = 10
  }

  # Cloud Tasks stops retrying only once BOTH max_attempts and max_retry_duration
  # are satisfied, so a small max_attempts makes the duration the binding limit.
  retry_config {
    max_attempts       = var.triage_queue_max_attempts
    max_retry_duration = var.triage_queue_max_retry_duration
    min_backoff        = var.triage_queue_min_backoff
    max_backoff        = var.triage_queue_max_backoff
  }
}

resource "google_cloud_tasks_queue" "timer_queue" {
  depends_on = [google_project_service.required]

  name     = "${var.name_prefix}-timer-queue"
  location = var.region
  rate_limits {
    max_concurrent_dispatches = 50
    max_dispatches_per_second = 50
  }

  # Same rule as the triage queue: retrying stops only once BOTH max_attempts and
  # max_retry_duration are satisfied, so the duration is the binding limit.
  retry_config {
    max_attempts       = var.timer_queue_max_attempts
    max_retry_duration = var.timer_queue_max_retry_duration
    min_backoff        = var.timer_queue_min_backoff
    max_backoff        = var.timer_queue_max_backoff
  }
}

# 3. Artifact Registry Docker Repository
resource "google_artifact_registry_repository" "docker_repo" {
  depends_on = [google_project_service.required]

  repository_id = "${var.name_prefix}-docker"
  location      = var.region
  format        = "DOCKER"
  description   = "Docker repository for alert triage agent services"
  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-tools"
  })
}

# 4. Service Accounts
resource "google_service_account" "bridge_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-bridge-sa"
  display_name = "Alert Triage Bridge Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-bridge,role=triage-system,tier=platform,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

resource "google_service_account" "agent_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-agent-sa"
  display_name = "Alert Triage Agent Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-agent,role=triage-system,tier=platform,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

# Demo workload and Apigee SAs. Created in Terraform so a fresh project needs no
# hand-made SAs. On the live stack these were created by hand before this code
# existed: adopt them with `terraform import` (see the adoption runbook) instead
# of an import {} block, which would fail in a fresh project.
resource "google_service_account" "inventory_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-inventory-sa"
  display_name = "${upper(var.name_prefix)} Inventory Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-inventory-api,role=demo-workload,tier=service,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

resource "google_service_account" "orders_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-orders-sa"
  display_name = "${upper(var.name_prefix)} Orders Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-orders-api,role=demo-workload,tier=service,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

resource "google_service_account" "apigee_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-apigee-sa"
  display_name = "${upper(var.name_prefix)} Apigee Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-apigee,role=demo-workload,tier=edge,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

# Cloud Build runs as this SA (gcloud builds submit --service-account), so builds
# do not depend on the default Cloud Build / Compute SA or on the automatic role
# grants that orgs often disable. Source staging and build logs go to the build
# bucket below (--gcs-source-staging-dir / --gcs-log-dir).
resource "google_service_account" "build_sa" {
  depends_on = [google_project_service.required]

  account_id   = "${var.name_prefix}-build-sa"
  display_name = "Alert Triage Cloud Build Service Account"
  description  = "app=alert-triage-agent,component=${var.name_prefix}-build,role=triage-system,tier=platform,env=${var.environment},managed-by=terraform,owner=${var.owner}"
}

resource "google_storage_bucket" "build" {
  depends_on = [google_project_service.required]

  name                        = "${var.project_id}-${var.name_prefix}-build"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = true

  lifecycle_rule {
    condition {
      age = 30
    }
    action {
      type = "Delete"
    }
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-build"
  })
}

# Build SA: read staged source and write logs in the build bucket, push images
# to the stack's repository, and write build logs to Cloud Logging.
resource "google_storage_bucket_iam_member" "build_bucket_admin" {
  bucket = google_storage_bucket.build.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.build_sa.email}"
}

resource "google_artifact_registry_repository_iam_member" "build_repo_writer" {
  project    = var.project_id
  location   = google_artifact_registry_repository.docker_repo.location
  repository = google_artifact_registry_repository.docker_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.build_sa.email}"
}

resource "google_project_iam_member" "build_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.build_sa.email}"
}

# Demo workload roles (the roles granted by hand on the live stack).
# inventory: Cloud SQL connector + IAM database login, logs.
resource "google_project_iam_member" "inventory_roles" {
  for_each = toset([
    "roles/cloudsql.client",
    "roles/cloudsql.instanceUser",
    "roles/logging.logWriter",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.inventory_sa.email}"
}

resource "google_project_iam_member" "orders_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.orders_sa.email}"
}

# The Apigee ML-CloudLogging policy writes the access log as this SA.
resource "google_project_iam_member" "apigee_log_writer" {
  project = var.project_id
  role    = "roles/logging.logWriter"
  member  = "serviceAccount:${google_service_account.apigee_sa.email}"
}

# Deployers that may act as the Apigee SA (deploy-apigee-proxies.sh deploys the
# proxies with serviceAccount=<apigee SA>) and as the build SA (gcloud builds
# submit --service-account). Project Owners already hold
# iam.serviceAccountUser; list other deployer principals here.
resource "google_service_account_iam_member" "deployer_act_as_apigee" {
  for_each = toset(var.deployer_members)

  service_account_id = google_service_account.apigee_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

resource "google_service_account_iam_member" "deployer_act_as_build" {
  for_each = toset(var.deployer_members)

  service_account_id = google_service_account.build_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = each.value
}

# Bridge SA IAM. Every grant is scoped to this stack's resources, so two stacks
# (different name_prefix) in one project cannot read each other's secrets,
# write each other's bucket, enqueue into each other's queue or act as each
# other's SAs. run.invoker on this stack's agent is in the triage-iam root.

# Enqueue triage tasks (bridge/app/core/dispatcher.py) into this stack's queue.
resource "google_cloud_tasks_queue_iam_member" "bridge_triage_enqueuer" {
  project  = var.project_id
  location = google_cloud_tasks_queue.triage_queue.location
  name     = google_cloud_tasks_queue.triage_queue.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Read this stack's webhook token and ServiceNow credentials (bridge/app/config.py,
# and the bridge deploy's --set-secrets).
resource "google_secret_manager_secret_iam_member" "bridge_secret_accessor" {
  for_each = merge(
    { "webhook-token" = google_secret_manager_secret.webhook_token.id },
    { for k, v in google_secret_manager_secret.servicenow_placeholders : k => v.id },
  )
  project   = var.project_id
  secret_id = each.value
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Write HTML reports to this stack's incidents bucket (bridge/app/adapters/servicenow.py).
resource "google_storage_bucket_iam_member" "bridge_incidents_writer" {
  bucket = google_storage_bucket.incidents.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# The bridge's Cloud Tasks carry an OIDC token for the bridge SA itself
# (effective_tasks_invoker_sa), so it must be able to act as itself only.
resource "google_service_account_iam_member" "bridge_sa_service_account_user" {
  service_account_id = google_service_account.bridge_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.bridge_sa.email}"
}

# Agent SA Viewer Roles on Production Project (Strictly Read-Only Production Boundary)
resource "google_project_iam_member" "agent_viewer_roles" {
  for_each = toset([
    "roles/logging.viewer",
    "roles/monitoring.viewer",
    "roles/run.viewer",
    "roles/cloudsql.viewer"
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Agent SA runtime role outside the viewer boundary above, inference-only: Vertex AI
# Gemini calls for gray-zone correlation scoring (agent/app/correlation/engine.py).
resource "google_project_iam_member" "agent_runtime_roles" {
  for_each = toset([
    "roles/aiplatform.user",
  ])
  project = var.project_id
  role    = each.value
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Apigee reads of ata-tools (which runs as the agent SA), and nothing else. Not
# roles/apigee.readOnlyAdmin: that role also reads every developer app's consumer key
# and secret in the shared org, i.e. other stacks' credentials (sec M3).
#   apigee.deployments.list   get_change_timeline: GET .../environments/<env>/deployments
#   apigee.proxyrevisions.get get_proxy_bundle:    GET .../apis/<api>/revisions/<rev>?format=bundle
# The change specialist reads Apigee only through those two tools; the Apigee
# specialist reads Cloud Monitoring metrics (roles/monitoring.viewer).
#
# Teardown and redeploy: destroying a custom role only soft-deletes it, and its id
# stays reserved (7 days undeletable, then up to 37 more days until it is purged).
# Within the first 7 days the google provider undeletes and updates a soft-deleted
# role of the same id on create, so a teardown/redeploy cycle just works. After that,
# set apigee_reader_role_suffix (e.g. "v2") to use a fresh id.
resource "google_project_iam_custom_role" "apigee_deploy_reader" {
  project     = var.project_id
  role_id     = "${var.name_prefix}ApigeeDeployReader${var.apigee_reader_role_suffix}"
  title       = "${var.name_prefix} Apigee deployment reader"
  description = "app=alert-triage-agent,stack=${var.name_prefix}: Apigee deployments list and proxy revision bundles for ${var.name_prefix}-tools (change timeline). No developer app or key reads."
  permissions = [
    "apigee.deployments.list",
    "apigee.proxyrevisions.get",
  ]
}

resource "google_project_iam_member" "agent_apigee_deploy_reader" {
  project = var.project_id
  role    = google_project_iam_custom_role.apigee_deploy_reader.name
  member  = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Agent SA Write Access strictly scoped to Incidents Bucket Only
resource "google_storage_bucket_iam_member" "agent_incidents_writer" {
  bucket = google_storage_bucket.incidents.name
  role   = "roles/storage.objectUser"
  member = "serviceAccount:${google_service_account.agent_sa.email}"
}

# Enqueue timer tasks (agent/app/timers/scheduler.py) into this stack's timer
# queue only, with an OIDC token for the agent SA itself (SERVICE_ACCOUNT_EMAIL):
# act-as is granted on the agent SA only, not project-wide.
resource "google_cloud_tasks_queue_iam_member" "agent_cloudtasks_enqueuer" {
  project  = var.project_id
  location = google_cloud_tasks_queue.timer_queue.location
  name     = google_cloud_tasks_queue.timer_queue.name
  role     = "roles/cloudtasks.enqueuer"
  member   = "serviceAccount:${google_service_account.agent_sa.email}"
}

resource "google_service_account_iam_member" "agent_sa_service_account_user" {
  service_account_id = google_service_account.agent_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.agent_sa.email}"
}

# 5. Webhook Token Generated Directly in Terraform (Sensitive, never user-entered)
resource "random_password" "webhook_token" {
  length  = 32
  special = false
  keepers = {
    rotation = "v2_basicauth"
  }
}

resource "google_secret_manager_secret" "webhook_token" {
  depends_on = [google_project_service.required]

  secret_id = "${var.name_prefix}-webhook-token"

  replication {
    auto {}
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}

resource "google_secret_manager_secret_version" "webhook_token_version" {
  secret      = google_secret_manager_secret.webhook_token.id
  secret_data = random_password.webhook_token.result
}

# 6. ServiceNow Secret Placeholders (User sets ONLY these 2 credentials via set-secrets.sh)
locals {
  servicenow_secret_keys = [
    "servicenow-username",
    "servicenow-password"
  ]
}

resource "google_secret_manager_secret" "servicenow_placeholders" {
  depends_on = [google_project_service.required]

  for_each  = toset(local.servicenow_secret_keys)
  secret_id = "${var.name_prefix}-${each.value}"

  replication {
    auto {}
  }

  labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}

resource "google_secret_manager_secret_version" "servicenow_placeholder_versions" {
  for_each    = toset(local.servicenow_secret_keys)
  secret      = google_secret_manager_secret.servicenow_placeholders[each.key].id
  secret_data = "placeholder"

  lifecycle {
    ignore_changes = [secret_data]
  }
}

# 7. Cloud Monitoring Webhook Notification Channel
resource "google_monitoring_notification_channel" "bridge_webhook" {
  depends_on = [google_project_service.required]

  project      = var.project_id
  display_name = "${var.name_prefix}-bridge-webhook"
  type         = "webhook_basicauth"
  labels = {
    # Deterministic Cloud Run URL of the bridge (https://<service>-<project
    # number>.<region>.run.app), known before the bridge is deployed.
    url      = "https://${var.name_prefix}-bridge-${data.google_project.this.number}.${var.region}.run.app/webhook/monitoring"
    username = "${var.name_prefix}-monitoring"
  }
  sensitive_labels {
    password = random_password.webhook_token.result
  }
  user_labels = merge(local.common_labels, {
    component = "${var.name_prefix}-bridge"
  })
}
