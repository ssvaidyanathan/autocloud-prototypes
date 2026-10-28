output "webhook_notification_channel_id" {
  description = "Notification channel ID for the <name_prefix>-bridge webhook"
  value       = local.notification_channel_id
}

output "alert_policies" {
  description = "List of created alert policies"
  value = [
    google_monitoring_alert_policy.orders_5xx.display_name,
    google_monitoring_alert_policy.orders_latency.display_name,
    google_monitoring_alert_policy.inventory_5xx.display_name,
    google_monitoring_alert_policy.inventory_latency.display_name,
    google_monitoring_alert_policy.apigee_orders_5xx.display_name,
    google_monitoring_alert_policy.apigee_orders_429.display_name,
    google_monitoring_alert_policy.cloudsql_connections.display_name,
    google_monitoring_alert_policy.cloudsql_cpu.display_name,
  ]
}
