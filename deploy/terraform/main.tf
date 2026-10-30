data "google_project" "current" {}

locals {
  image_repo = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.app.repository_id}"
  image      = "${local.image_repo}/app"
}

# ---------------------------------------------------------------- APIs ----
resource "google_project_service" "apis" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "firestore.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "storage.googleapis.com",
    "orgpolicy.googleapis.com",
    "billingbudgets.googleapis.com",
  ])
  service            = each.value
  disable_on_destroy = false
}

# --------------------------------------------------- Artifact Registry ----
resource "google_artifact_registry_repository" "app" {
  repository_id = var.name
  location      = var.region
  format        = "DOCKER"
  description   = "QXC app container images"

  cleanup_policy_dry_run = false

  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 5 # ~350 MB each; 5 images stays inside the 0.5 GB free tier
    }
  }

  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition {
      older_than = "2592000s" # 30 days
    }
  }

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------- source data bucket ----
# Holds the USAspending CSV exports under raw/. CI copies them into the
# build context; versioning keeps every refresh so a bad export can be
# rolled back.
resource "google_storage_bucket" "data" {
  name                        = "${var.project_id}-${var.name}-data"
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Keep the last few CSV versions, not every one forever.
  lifecycle_rule {
    condition {
      num_newer_versions = 5
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.apis]
}

# ---------------------------------------------------------- budget ----
# Alerts only: Google budgets email, they do not stop spend. The real cap
# is max_instances on the Cloud Run service above.
resource "google_billing_budget" "monthly" {
  count           = var.billing_account_id != "" ? 1 : 0
  billing_account = var.billing_account_id
  display_name    = "${var.name} monthly"

  budget_filter {
    projects = ["projects/${data.google_project.current.number}"]
  }

  amount {
    specified_amount {
      currency_code = "USD"
      units         = tostring(floor(var.monthly_budget_usd))
    }
  }

  dynamic "threshold_rules" {
    for_each = [0.5, 0.9, 1.0, 1.5]
    content {
      threshold_percent = threshold_rules.value
      spend_basis       = "CURRENT_SPEND"
    }
  }

  # No all_updates_rule: with it omitted, the billing account's admins get
  # the threshold emails by default.

  depends_on = [google_project_service.apis]
}

# ------------------------------------------------------------ secrets ----
resource "google_secret_manager_secret" "basic_auth" {
  secret_id = "${var.name}-basic-auth"

  replication {
    auto {}
  }

  depends_on = [google_project_service.apis]
}

resource "google_secret_manager_secret_version" "basic_auth" {
  secret      = google_secret_manager_secret.basic_auth.id
  secret_data = var.basic_auth_credentials
}

# ---------------------------------------------------------- Firestore ----
# User-entered data (viewed flags, notes, contacts, enrichment) lives here
# on Cloud Run, so it survives every deploy. The app switches to it
# automatically when it detects Cloud Run.
resource "google_firestore_database" "default" {
  count       = var.create_firestore_database ? 1 : 0
  name        = "(default)"
  location_id = var.firestore_location
  type        = "FIRESTORE_NATIVE"

  delete_protection_state = "DELETE_PROTECTION_ENABLED"
  deletion_policy         = "ABANDON" # terraform destroy never drops user data

  depends_on = [google_project_service.apis]
}

# ----------------------------------------------- runtime service account ----
resource "google_service_account" "runtime" {
  account_id   = "qxc-app-run" # service-account ids are limited to 30 chars
  display_name = "QXC app runtime"
}

resource "google_secret_manager_secret_iam_member" "runtime_reads_secret" {
  secret_id = google_secret_manager_secret.basic_auth.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.runtime.email}"
}

resource "google_project_iam_member" "runtime_firestore" {
  project = var.project_id
  role    = "roles/datastore.user"
  member  = "serviceAccount:${google_service_account.runtime.email}"
}

# ---------------------------------------------------------- Cloud Run ----
resource "google_cloud_run_v2_service" "app" {
  name                = var.name
  location            = var.region
  ingress             = "INGRESS_TRAFFIC_ALL"
  deletion_protection = false

  template {
    service_account                  = google_service_account.runtime.email
    max_instance_request_concurrency = 80
    timeout                          = "60s"

    scaling {
      min_instance_count = var.min_instances
      max_instance_count = var.max_instances
    }

    containers {
      image = "${local.image}:${var.image_tag}"

      ports {
        container_port = 8080
      }

      resources {
        limits = {
          cpu    = var.cpu
          memory = var.memory
        }
        cpu_idle          = true # bill CPU only while serving requests
        startup_cpu_boost = true
      }

      env {
        name  = "WEB_CONCURRENCY"
        value = "2"
      }
      env {
        name  = "GUNICORN_THREADS"
        value = "4"
      }
      env {
        name = "QXC_BASIC_AUTH"
        value_source {
          secret_key_ref {
            secret = google_secret_manager_secret.basic_auth.secret_id
            # Pinned to the version terraform manages, not "latest", so a
            # password rotation changes the template and rolls a new
            # revision automatically.
            version = google_secret_manager_secret_version.basic_auth.version
          }
        }
      }

      startup_probe {
        http_get {
          path = "/health"
        }
        initial_delay_seconds = 5
        period_seconds        = 5
        timeout_seconds       = 3
        failure_threshold     = 12
      }

      liveness_probe {
        http_get {
          path = "/health"
        }
        period_seconds    = 30
        timeout_seconds   = 3
        failure_threshold = 3
      }
    }
  }

  lifecycle {
    # CI rolls out new images by git sha; don't let terraform revert them.
    ignore_changes = [
      template[0].containers[0].image,
      client,
      client_version,
    ]
  }

  depends_on = [
    google_project_service.apis,
    google_secret_manager_secret_version.basic_auth,
    google_secret_manager_secret_iam_member.runtime_reads_secret,
  ]
}

# Google Workspace organizations usually ship with "domain restricted
# sharing" on, which blocks granting anything to allUsers. This project-level
# exception lifts it for this project only. The app still enforces its own
# login gate.
resource "google_org_policy_policy" "allow_all_members" {
  count  = var.allow_public_access_override ? 1 : 0
  name   = "projects/${var.project_id}/policies/iam.allowedPolicyMemberDomains"
  parent = "projects/${var.project_id}"

  spec {
    rules {
      allow_all = "TRUE"
    }
  }

  depends_on = [google_project_service.apis]
}

# The app enforces its own login gate, so the URL is reachable by anyone.
resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.invoker"
  member   = "allUsers"

  depends_on = [google_org_policy_policy.allow_all_members]
}
