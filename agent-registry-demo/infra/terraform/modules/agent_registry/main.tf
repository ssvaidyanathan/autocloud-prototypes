resource "google_agent_registry_service" "calculator" {
  project      = var.project_id
  location     = var.region
  service_id   = var.service_id
  display_name = var.display_name
  description  = var.description

  mcp_server_spec {
    type = "NO_SPEC"
  }

  interfaces {
    protocol_binding = "http-json"
    url              = "${var.calculator_service_url}/mcp"
  }
}
