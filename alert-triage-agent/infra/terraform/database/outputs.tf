output "instance_name" {
  description = "Cloud SQL instance name"
  value       = google_sql_database_instance.inventory_db.name
}

output "instance_connection_name" {
  description = "Cloud SQL instance connection name"
  value       = google_sql_database_instance.inventory_db.connection_name
}

output "database_name" {
  description = "Inventory database name"
  value       = google_sql_database.inventory_database.name
}

output "database_ip" {
  description = "Cloud SQL public IP address"
  value       = google_sql_database_instance.inventory_db.public_ip_address
}

output "iam_user" {
  description = "IAM DB user name"
  value       = google_sql_user.inventory_iam_user.name
}
