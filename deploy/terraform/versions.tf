terraform {
  required_version = ">= 1.5"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }

  # Recommended once more than one person applies this: keep state in GCS.
  # Uncomment and fill in after a state bucket exists.
  # backend "gcs" {
  #   bucket = "CHANGE-ME-terraform-state"
  #   prefix = "qxc/cloudrun"
  # }
}

provider "google" {
  project = var.project_id
  region  = var.region

  # Org Policy and Billing Budgets are "quota project" APIs: calls made with
  # a person's local credentials must name a project to bill the quota to.
  user_project_override = true
  billing_project       = var.project_id

  default_labels = {
    project    = "qxc-equipment-intelligence"
    managed-by = "terraform"
  }
}
