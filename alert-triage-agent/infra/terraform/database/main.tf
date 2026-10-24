resource "google_sql_database_instance" "inventory_db" {
  name                = "${var.name_prefix}-inventory-db"
  project             = var.project_id
  region              = var.region
  database_version    = "POSTGRES_15"
  deletion_protection = false

  settings {
    tier = "db-f1-micro"

    database_flags {
      name  = "cloudsql.iam_authentication"
      value = "on"
    }

    ip_configuration {
      ipv4_enabled = true
      require_ssl  = false
      ssl_mode     = "ALLOW_UNENCRYPTED_AND_ENCRYPTED"
    }

    user_labels = {
      app        = "alert-triage-agent"
      component  = "${var.name_prefix}-inventory-db"
      role       = "demo-workload"
      tier       = "data"
      env        = var.environment
      managed-by = "terraform"
      owner      = var.owner
    }
  }
}

resource "google_sql_database" "inventory_database" {
  name     = "inventory"
  project  = var.project_id
  instance = google_sql_database_instance.inventory_db.name
}

resource "google_sql_user" "inventory_iam_user" {
  name     = "${var.name_prefix}-inventory-sa@${var.project_id}.iam"
  project  = var.project_id
  instance = google_sql_database_instance.inventory_db.name
  type     = "CLOUD_IAM_SERVICE_ACCOUNT"
}
