output "service_url" {
  description = "Public HTTPS URL Cloud Run assigned to the app."
  value       = google_cloud_run_v2_service.app.uri
}

output "image" {
  description = "Push images here (append :<tag>)."
  value       = local.image
}

output "artifact_registry_host" {
  description = "Docker registry host to authenticate against."
  value       = "${var.region}-docker.pkg.dev"
}

output "data_bucket" {
  description = "GCS bucket holding the source CSVs under raw/."
  value       = google_storage_bucket.data.name
}

output "cloud_run_service" {
  value = google_cloud_run_v2_service.app.name
}

output "region" {
  value = var.region
}

output "github_workload_identity_provider" {
  description = "Set as the GCP_WORKLOAD_IDENTITY_PROVIDER repository variable in GitHub."
  value       = local.github_enabled ? google_iam_workload_identity_pool_provider.github[0].name : null
}

output "github_deploy_service_account" {
  description = "Set as the GCP_DEPLOY_SERVICE_ACCOUNT repository variable in GitHub."
  value       = local.github_enabled ? google_service_account.github_deploy[0].email : null
}
