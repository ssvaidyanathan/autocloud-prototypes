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
      require_ssl  = true
      ssl_mode     = "ENCRYPTED_ONLY"
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
