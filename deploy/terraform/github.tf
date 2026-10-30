# Lets the GitHub Actions workflow in .github/workflows/deploy.yml push images
# and roll out revisions without a downloaded service-account key (Workload
# Identity Federation). Trust is limited to one repository and one branch.

locals {
  github_enabled = var.github_repo != ""
}

resource "google_iam_workload_identity_pool" "github" {
  count                     = local.github_enabled ? 1 : 0
  workload_identity_pool_id = "github"
  display_name              = "GitHub Actions"

  depends_on = [google_project_service.apis]
}

resource "google_iam_workload_identity_pool_provider" "github" {
  count                              = local.github_enabled ? 1 : 0
  workload_identity_pool_id          = google_iam_workload_identity_pool.github[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "github-oidc"
  display_name                       = "GitHub OIDC"

  attribute_mapping = {
    "google.subject"       = "assertion.sub"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }

  attribute_condition = "assertion.repository == \"${var.github_repo}\" && assertion.ref == \"refs/heads/${var.github_branch}\""

  oidc {
    issuer_uri = "https://token.actions.githubusercontent.com"
  }
}

resource "google_service_account" "github_deploy" {
  count        = local.github_enabled ? 1 : 0
  account_id   = "qxc-gh-deploy" # service-account ids are limited to 30 chars
  display_name = "GitHub Actions deployer"
}

# GitHub -> this service account
resource "google_service_account_iam_member" "github_wif" {
  count              = local.github_enabled ? 1 : 0
  service_account_id = google_service_account.github_deploy[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principalSet://iam.googleapis.com/${google_iam_workload_identity_pool.github[0].name}/attribute.repository/${var.github_repo}"
}

# What the deployer may do: push images, read CSVs, roll out the service,
# and act as the runtime service account when deploying.
resource "google_artifact_registry_repository_iam_member" "github_push" {
  count      = local.github_enabled ? 1 : 0
  repository = google_artifact_registry_repository.app.name
  location   = google_artifact_registry_repository.app.location
  role       = "roles/artifactregistry.writer"
  member     = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

resource "google_storage_bucket_iam_member" "github_reads_data" {
  count  = local.github_enabled ? 1 : 0
  bucket = google_storage_bucket.data.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

resource "google_cloud_run_v2_service_iam_member" "github_deploys" {
  count    = local.github_enabled ? 1 : 0
  name     = google_cloud_run_v2_service.app.name
  location = google_cloud_run_v2_service.app.location
  role     = "roles/run.developer"
  member   = "serviceAccount:${google_service_account.github_deploy[0].email}"
}

resource "google_service_account_iam_member" "github_acts_as_runtime" {
  count              = local.github_enabled ? 1 : 0
  service_account_id = google_service_account.runtime.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.github_deploy[0].email}"
}
