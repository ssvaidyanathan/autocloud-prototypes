terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # Partial configuration: bucket and prefix come from backend.hcl, written by
  # scripts/bootstrap-tfstate.sh (terraform init -backend-config=backend.hcl).
  backend "gcs" {}
}

provider "google" {
  project = var.project_id
  region  = var.region
}
