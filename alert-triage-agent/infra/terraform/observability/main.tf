data "terraform_remote_state" "foundation" {
  backend = "gcs"
  config = {
    bucket = "cloud-assist-fde-2-ata-tfstate"
    prefix = "ata/foundation"
  }
}

locals {
  notification_channel_id = var.notification_channel_id != null ? var.notification_channel_id : data.terraform_remote_state.foundation.outputs.notification_channel_id
}

# -------------------------------------------------------------
# Alert Policy 1: ata-orders-5xx (Cloud Run 5xx > 5%)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "orders_5xx" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-orders-5xx"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud Run orders 5xx error rate"
    condition_threshold {
      filter                  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.name_prefix}-orders-api\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      duration                = "60s"
      comparison              = "COMPARISON_GT"
      threshold_value         = 0.05
      evaluation_missing_data = var.alert_evaluation_missing_data
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-orders-api"
    tier      = "service"
    signal    = "5xx"
    severity  = "p1"
  }
}

# -------------------------------------------------------------
# Alert Policy 2: ata-orders-latency (Cloud Run latency > 2s)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "orders_latency" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-orders-latency"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud Run orders latency > 2s"
    condition_threshold {
      filter                  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.name_prefix}-orders-api\" AND metric.type = \"run.googleapis.com/request_latencies\""
      duration                = "60s"
      comparison              = "COMPARISON_GT"
      threshold_value         = 2000
      evaluation_missing_data = var.alert_evaluation_missing_data
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = var.latency_per_series_aligner
        cross_series_reducer = var.latency_cross_series_reducer
        group_by_fields      = var.latency_group_by
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-orders-api"
    tier      = "service"
    signal    = "latency"
    severity  = "p2"
  }
}

# -------------------------------------------------------------
# Alert Policy 3: ata-inventory-5xx (Cloud Run 5xx > 5%)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "inventory_5xx" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-inventory-5xx"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud Run inventory 5xx error rate"
    condition_threshold {
      filter                  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.name_prefix}-inventory-api\" AND metric.type = \"run.googleapis.com/request_count\" AND metric.labels.response_code_class = \"5xx\""
      duration                = "60s"
      comparison              = "COMPARISON_GT"
      threshold_value         = 0.05
      evaluation_missing_data = var.alert_evaluation_missing_data
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-inventory-api"
    tier      = "service"
    signal    = "5xx"
    severity  = "p1"
  }
}

# -------------------------------------------------------------
# Alert Policy 4: ata-inventory-latency (Cloud Run latency > 2s)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "inventory_latency" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-inventory-latency"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud Run inventory latency > 2s"
    condition_threshold {
      filter                  = "resource.type = \"cloud_run_revision\" AND resource.labels.service_name = \"${var.name_prefix}-inventory-api\" AND metric.type = \"run.googleapis.com/request_latencies\""
      duration                = "60s"
      comparison              = "COMPARISON_GT"
      threshold_value         = 2000
      evaluation_missing_data = var.alert_evaluation_missing_data
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = var.latency_per_series_aligner
        cross_series_reducer = var.latency_cross_series_reducer
        group_by_fields      = var.latency_group_by
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-inventory-api"
    tier      = "service"
    signal    = "latency"
    severity  = "p2"
  }
}

# -------------------------------------------------------------
# Alert Policy 5: ata-apigee-orders-5xx (Apigee 5xx spike)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "apigee_orders_5xx" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-apigee-orders-5xx"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Apigee orders proxy 5xx spike"
    condition_threshold {
      filter                  = "resource.type = \"apigee.googleapis.com/Proxy\" AND resource.labels.proxy_name = \"${var.name_prefix}-orders-v1\" AND metric.type = \"apigee.googleapis.com/proxy/response_count\" AND (metric.labels.response_code = \"500\" OR metric.labels.response_code = \"502\" OR metric.labels.response_code = \"504\")"
      duration                = "60s"
      comparison              = "COMPARISON_GT"
      threshold_value         = 1
      evaluation_missing_data = var.alert_evaluation_missing_data
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-orders-v1"
    tier      = "edge"
    signal    = "5xx"
    severity  = "p1"
  }
}

# -------------------------------------------------------------
# Alert Policy 6: ata-apigee-orders-429 (Apigee SpikeArrest 429)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "apigee_orders_429" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-apigee-orders-429"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Apigee orders proxy 429 rate limit spike"
    condition_threshold {
      filter          = "resource.type = \"apigee.googleapis.com/Proxy\" AND resource.labels.proxy_name = \"${var.name_prefix}-orders-v1\" AND metric.type = \"apigee.googleapis.com/proxy/response_count\" AND metric.labels.response_code = \"429\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 1
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_RATE"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-orders-v1"
    tier      = "edge"
    signal    = "rate-limit"
    severity  = "p2"
  }
}

# -------------------------------------------------------------
# Alert Policy 7: ata-cloudsql-connections-critical (>80% / 20 connections)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_connections" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-cloudsql-connections-critical"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud SQL connection count critical"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND resource.labels.database_id = \"${var.project_id}:${var.name_prefix}-inventory-db\" AND metric.type = \"cloudsql.googleapis.com/database/postgresql/num_backends\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = var.cloudsql_connections_threshold
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-inventory-db"
    tier      = "data"
    signal    = "connections"
    severity  = "p1"
  }
}

# -------------------------------------------------------------
# Alert Policy 8: ata-cloudsql-cpu-high (>85%)
# -------------------------------------------------------------
resource "google_monitoring_alert_policy" "cloudsql_cpu" {
  project               = var.project_id
  display_name          = "${var.name_prefix}-cloudsql-cpu-high"
  combiner              = "OR"
  notification_channels = [local.notification_channel_id]

  alert_strategy {
    auto_close = "1800s"
  }

  conditions {
    display_name = "Cloud SQL CPU utilization > 85%"
    condition_threshold {
      filter          = "resource.type = \"cloudsql_database\" AND resource.labels.database_id = \"${var.project_id}:${var.name_prefix}-inventory-db\" AND metric.type = \"cloudsql.googleapis.com/database/cpu/utilization\""
      duration        = "60s"
      comparison      = "COMPARISON_GT"
      threshold_value = 0.85
      aggregations {
        alignment_period   = "60s"
        per_series_aligner = "ALIGN_MEAN"
      }
    }
  }

  user_labels = {
    app       = "alert-triage-agent"
    component = "${var.name_prefix}-inventory-db"
    tier      = "data"
    signal    = "cpu"
    severity  = "p2"
  }
}
