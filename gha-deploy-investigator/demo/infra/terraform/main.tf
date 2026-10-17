# Artifact Registry repository for Demo App container images
resource "google_artifact_registry_repository" "demo_repo" {
  project       = var.project_id
  location      = var.region
  repository_id = "${var.name_prefix}-demo-app"
  description   = "Docker repository for GDI Demo App"
  format        = "DOCKER"
}

# Dedicated Service Account for GitHub Actions Deployer
resource "google_service_account" "demo_deployer_sa" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-demo-deployer-sa"
  display_name = "GDI Demo Deployer Service Account"
}

# Dedicated Service Account for Demo App Cloud Run Runtime
resource "google_service_account" "demo_app_sa" {
  project      = var.project_id
  account_id   = "${var.name_prefix}-demo-app-sa"
  display_name = "GDI Demo App Runtime Service Account"
}

# IAM role: Cloud Run Admin for deploying demo revisions
resource "google_project_iam_member" "demo_deployer_run_admin" {
  project = var.project_id
  role    = "roles/run.admin"
  member  = "serviceAccount:${google_service_account.demo_deployer_sa.email}"
}

# IAM role: Artifact Registry Writer to push built container images
resource "google_artifact_registry_repository_iam_member" "demo_deployer_ar_writer" {
  project    = var.region != "" ? var.project_id : var.project_id
  location   = var.region
  repository = google_artifact_registry_repository.demo_repo.name
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.demo_deployer_sa.email}"
}

# IAM role: Service Account User on itself to run Cloud Run as the service account
resource "google_service_account_iam_member" "demo_deployer_sa_user" {
  service_account_id = google_service_account.demo_deployer_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.demo_deployer_sa.email}"
}

# IAM role: Service Account User on demo_app_sa so deployer can deploy Cloud Run with runtime SA
resource "google_service_account_iam_member" "demo_deployer_app_sa_user" {
  service_account_id = google_service_account.demo_app_sa.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.demo_deployer_sa.email}"
}

# Workload Identity Pool for GitHub Actions OIDC federation
resource "google_iam_workload_identity_pool" "pool" {
  project                   = var.project_id
  workload_identity_pool_id = "${var.name_prefix}-demo-pool"
  display_name              = "GDI Demo App WIF Pool"
}

# Workload Identity Provider configuring GitHub OIDC issuer and claims
resource "google_iam_workload_identity_pool_provider" "provider" {
  project                            = var.project_id
  workload_identity_pool_id          = google_iam_workload_identity_pool.pool.workload_identity_pool_id
  workload_identity_pool_provider_id = "${var.name_prefix}-demo-gh-provider"
  attribute_mapping = {
    "google.subject"             = "assertion.sub"
    "attribute.actor"            = "assertion.actor"
    "attribute.repository"       = "assertion.repository"
    "attribute.repository_owner" = "assertion.repository_owner"
    "attribute.ref"              = "assertion.ref"
  }
  attribute_condition = "assertion.repository == 'ssvaidyanathan/gdi-sample-pipeline-app' && assertion.ref == 'refs/heads/main'"

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

# IAM member allowing GitHub Actions OIDC to impersonate demo_deployer_sa
resource "google_service_account_iam_member" "wif_sa_binding" {
  service_account_id = google_service_account.demo_deployer_sa.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.pool.name}/attribute.repository/ssvaidyanathan/gdi-sample-pipeline-app"
}
