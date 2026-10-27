# Plan-only checks of var.deletion_protection against a mocked google provider (no credentials,
# no API calls): `terraform init -backend=false && terraform test`. Run by
# tests/test_terraform_portability.py when terraform is on PATH.

mock_provider "google" {}

variables {
  project_id      = "test-project"
  region          = "test-region"
  name_prefix     = "tst"
  inventory_image = "test-region-docker.pkg.dev/test-project/tst-docker/tst-inventory-api@sha256:0"
  orders_image    = "test-region-docker.pkg.dev/test-project/tst-docker/tst-orders-api@sha256:0"
}

run "default_protects_both_services" {
  command = plan

  assert {
    condition     = google_cloud_run_v2_service.inventory_api.deletion_protection == true
    error_message = "inventory_api must default to deletion_protection = true"
  }
  assert {
    condition     = google_cloud_run_v2_service.orders_api.deletion_protection == true
    error_message = "orders_api must default to deletion_protection = true"
  }
}

run "false_override_reaches_both_services" {
  command = plan

  variables {
    deletion_protection = false
  }

  assert {
    condition     = google_cloud_run_v2_service.inventory_api.deletion_protection == false
    error_message = "inventory_api must follow var.deletion_protection = false"
  }
  assert {
    condition     = google_cloud_run_v2_service.orders_api.deletion_protection == false
    error_message = "orders_api must follow var.deletion_protection = false"
  }
}
