variable "project_id" {
  description = "GCP project to deploy into (e.g. qxc-contract)."
  type        = string
}

variable "region" {
  description = "Region for Cloud Run, Artifact Registry and the data bucket."
  type        = string
  default     = "us-central1"
}

variable "name" {
  description = "Base name for resources (service, repo, service accounts)."
  type        = string
  default     = "qxc-equipment-intelligence"
}

variable "image_tag" {
  description = "Image tag for the initial deploy. CI deploys by git sha afterwards and Terraform ignores image changes from then on."
  type        = string
  default     = "latest"
}

variable "cpu" {
  description = "vCPUs per instance."
  type        = string
  default     = "1"
}

variable "memory" {
  description = "Memory per instance."
  type        = string
  default     = "2Gi"
}

variable "min_instances" {
  description = "Instances kept warm. 0 = scale to zero, ~$0 when nobody is using it, ~10 s cold start on the first visit. 1 = always warm for about $8/month."
  type        = number
  default     = 0
}

variable "max_instances" {
  description = "Hard scaling ceiling. This is the cost cap: 2 instances flat-out 24x7 is roughly $150/month, the realistic worst case even under abuse."
  type        = number
  default     = 2
}

variable "basic_auth_credentials" {
  description = "Stopgap shared login as \"user:password\". Stored in Secret Manager and injected as QXC_BASIC_AUTH. Set to \"\" to run with no login gate (not recommended on a public URL)."
  type        = string
  sensitive   = true
}

variable "create_firestore_database" {
  description = "Create the project's (default) Firestore database in Native mode. Set false if the project already has one."
  type        = bool
  default     = true
}

variable "firestore_location" {
  description = "Firestore location when creating the database. A single region is half the per-operation price of the nam5 multi-region and plenty for this app."
  type        = string
  default     = "us-central1"
}

variable "billing_account_id" {
  description = "Billing account ID (Billing > Account management, format XXXXXX-XXXXXX-XXXXXX). When set, a monthly budget with email alerts is created. Needs Billing Account Costs Manager on that account; leave \"\" and have the billing owner create the budget in the console instead."
  type        = string
  default     = ""
}

variable "monthly_budget_usd" {
  description = "Monthly budget for the alert. Emails go to the billing account admins at 50%, 90%, 100% and 150% of this."
  type        = number
  default     = 25
}

variable "allow_public_access_override" {
  description = "Set a project-level exception to the org's domain-restricted-sharing policy so the service can be invoked by anyone (the app enforces its own login). Needed when the Workspace org blocks allUsers; applying it requires the Organization Policy Administrator role. Set false if the apply fails on it or the org has no such policy."
  type        = bool
  default     = true
}

variable "github_repo" {
  description = "GitHub repository allowed to deploy, as owner/name. Set \"\" to skip the keyless GitHub setup."
  type        = string
  default     = "Bretmkiefer/QXC-Project"
}

variable "github_branches" {
  description = "Branches allowed to deploy. Keep this to main once the deploy branch is merged."
  type        = list(string)
  default     = ["main"]
}
