# Deploying QXC Equipment Intelligence to Google Cloud

This gets the app running on Google Cloud **as it is today**: one container
on Cloud Run, behind a shared password, with the award data baked into the
image and user-entered data in Firestore so it survives deploys. The known
gaps (real SSO, automated data refresh) are a separate phase and are listed
at the bottom.

## Architecture

```
Internet ──HTTPS──► Cloud Run service (1 vCPU / 2 GiB, 1..3 instances, port 8080)
                        │  gunicorn + Flask + baked SQLite (award data, read-only)
                        ├─ pulls image from Artifact Registry
                        ├─ reads QXC_BASIC_AUTH from Secret Manager
                        ├─ writes notes / contacts / viewed / enrichment to Firestore
                        └─ logs to Cloud Logging
GitHub Actions ──(Workload Identity, no keys)──► build ──► push ──► gcloud run services update
GCS <project>-<name>-data/raw/   ◄── the two USAspending CSVs (versioned)
```

Cloud Run gives you an HTTPS URL on `*.run.app` with no domain needed. A
custom domain is a later, optional step (Cloud Run > Manage custom domains).

## What's in the box

| Piece | Purpose |
|---|---|
| `Dockerfile` | Two-stage build: stage 1 runs `db/build_db.py` on the CSVs, stage 2 ships only the app plus the built SQLite file, served by gunicorn as a non-root user. |
| `deploy/entrypoint.sh` | Starts gunicorn on `$PORT`. |
| `docker-compose.yml` | Run the production container locally on port 8080 (SQLite mode). |
| `deploy/terraform/main.tf` | Enables APIs; Artifact Registry repo, versioned GCS data bucket, Secret Manager secret, Firestore database, runtime service account, Cloud Run service. |
| `deploy/terraform/github.tf` | Workload Identity Federation pool/provider and a deploy service account limited to one repo and branch. |
| `.github/workflows/deploy.yml` | On push to `main`: pull CSVs from GCS, build, push, roll Cloud Run to the new image. |

App changes were kept minimal and are all driven by environment variables:

| Variable | Effect |
|---|---|
| `QXC_BASIC_AUTH` | One or more comma-separated `user:password[:role]` logins (HTTP Basic). `role` is `edit` (default) or `view`; viewers can browse everything but any change is refused. Passwords can't contain `:` or `,`. Unset locally, so nothing changes for `python app/app.py`. |
| `QXC_USE_FIRESTORE` | `1`/`0`. Defaults to on when Cloud Run's `K_SERVICE` is present, off otherwise. |
| `QXC_FIRESTORE_DATABASE` | Firestore database name. Default `(default)`. |
| `QXC_DB_PATH` | Use a SQLite file at this path instead of the baked copy (not used on Cloud Run). |
| `PORT`, `WEB_CONCURRENCY`, `GUNICORN_THREADS` | Server tuning. Cloud Run sets `PORT`; the others default to 2 / 4. |

`GET /health` is the startup and liveness probe, is never gated, and
(note: Cloud Run's front end reserves `/healthz` on public URLs and answers
it with a 404 before reaching the app, hence `/health`) it
reports which user-data store is active (`firestore` on Cloud Run) plus the
data vintage baked into the image: when it was built and the latest award
action date in it. That answers "when was the data last updated".

## Run the container locally

```bash
docker compose up --build
```

Open http://localhost:8080. To try the login gate:

```bash
QXC_BASIC_AUTH="qxc:testpass,board:readonly:view" docker compose up --build
```

## Where this runs

The GCP project lives in the **kiefer.xyz Google Workspace organization**
(shown as "Sweet Valley Venture"), display name **Quantum**, project ID `quantum-513214`, with billing
linked. You are a project Owner there under your **kiefer.xyz** account,
not your yellowbasket.io one, so every `gcloud auth login` below uses the
kiefer.xyz account. Use the project **ID**, not the display name, in
`terraform.tfvars`: Cloud console > project dropdown > ID column.

The eventual URL is `quantum.sweetvalley.net` (or `quantum.kiefer.xyz` in
the meantime). Until DNS is set up the app runs on the HTTPS `*.run.app`
address Cloud Run assigns, which is fine for the first weeks.

### Public access and the organization policy

Google Workspace organizations enable "domain restricted sharing" by
default, which is why adding a yellowbasket.io address to the project was
refused. The same policy blocks making the Cloud Run URL reachable by
anyone, which the app needs since it does its own login. The Terraform
sets a project-level exception (`allow_public_access_override = true`).
Applying it needs the **Organization Policy Administrator** role on the
organization, which an Owner does not have by default. As a Workspace
super admin you can grant it to yourself:

1. Cloud console > IAM & Admin > IAM.
2. Project dropdown > pick the **kiefer.xyz organization** (top level),
   not the project.
3. Grant access > your kiefer.xyz email > role "Organization Policy
   Administrator" > Save. Wait a minute for it to propagate.

If you'd rather not, set `allow_public_access_override = false`, ask Jack
to do the three steps above, and re-apply later.

## On your machine

```bash
brew install --cask google-cloud-sdk
brew install opentofu        # or terraform
gcloud auth login                         # kiefer.xyz account
gcloud auth application-default login     # same account; credentials terraform uses
gcloud config set project quantum-513214
```

## First deploy (one time)

1. **Create the registry and data bucket first, and upload the CSVs.**
   Cloud Run needs an image to exist before the service can be created,
   so the first apply is targeted:

   ```bash
   cd deploy/terraform
   cp terraform.tfvars.example terraform.tfvars   # fill in project_id and the two passwords
   tofu init
   tofu apply -target=google_project_service.apis -target=google_artifact_registry_repository.app -target=google_storage_bucket.data
   gcloud storage cp ../../data/raw/*.csv "gs://$(tofu output -raw data_bucket)/raw/"
   ```

2. **Build and push the first image.** On Apple Silicon the
   `--platform` flag matters; Cloud Run runs x86_64.

   ```bash
   gcloud auth configure-docker us-central1-docker.pkg.dev --quiet
   IMAGE=us-central1-docker.pkg.dev/quantum-513214/qxc-equipment-intelligence/app
   TAG=$(date -u +%Y%m%d-%H%M)
   docker build --platform linux/amd64 -t "$IMAGE:$TAG" ../..
   docker push "$IMAGE:$TAG"
   ```

   Then set `image_tag = "<that tag>"` in `terraform.tfvars`. Use a dated or
   git-sha tag, not `latest`: on the first deploy the registry never
   recorded a `latest` tag no matter how it was pushed, and Cloud Run
   refused to start until a concrete tag was used. (`tofu output` has no
   values yet at this point because the first apply was targeted, hence the
   literal paths.)

3. **Create everything else.**

   ```bash
   tofu apply
   tofu output service_url
   ```

   The service is live within about a minute. Open the URL, enter the
   edit login, and check `<url>/health` shows `"user_data_store":
   "firestore"`. Add a note to any record, then sign in with the view
   login in a private window and confirm the note is visible but can't be
   changed. Redeploy later to confirm the note persists.

   If the apply fails on `google_org_policy_policy.allow_all_members`,
   you don't have the Organization Policy Administrator role yet (see
   "Public access and the organization policy" above).

4. **Wire up GitHub so pushes deploy.** In the GitHub repo, Settings >
   Secrets and variables > Actions > Variables, add:

   | Variable | Value |
   |---|---|
   | `GCP_PROJECT_ID` | the project id |
   | `GCP_REGION` | `tofu output region` |
   | `GCP_WORKLOAD_IDENTITY_PROVIDER` | `tofu output github_workload_identity_provider` |
   | `GCP_DEPLOY_SERVICE_ACCOUNT` | `tofu output github_deploy_service_account` |
   | `GCP_IMAGE` | `tofu output image` |
   | `GCP_DATA_BUCKET` | `tofu output data_bucket` |
   | `CLOUD_RUN_SERVICE` | `tofu output cloud_run_service` |

   From then on every push to `main` builds, pushes and rolls the service
   with zero downtime (Cloud Run shifts traffic once the new revision
   passes its startup probe).

## Everyday operations

- **Ship a code change:** merge to `main`. Or build, push and run `gcloud run services update <service> --region <region> --image <image>:<tag>` by hand. If the change also touches the Terraform (probe path, env vars, scaling), set `image_tag` in tfvars to the new tag and `tofu apply` instead, so the new image and the new settings land in one revision.
- **Refresh the data:** upload the two new CSVs to `gs://<data_bucket>/raw/` (same filenames), then Actions > Deploy > Run workflow. The bucket is versioned so the previous export can be restored. User-entered data is untouched because it lives in Firestore.
- **Roll back:** Cloud Run > the service > Revisions > pick the previous one > Manage traffic > 100%. Or redeploy an older git-sha tag.
- **Change or add a login:** Secret Manager > the secret > New version with the full comma-separated list, then redeploy (the value is read at instance start). Or edit `basic_auth_credentials` in tfvars and `tofu apply`.
- **"When was the data last updated?"** `<url>/health` reports `built_at` and `latest_action_date` for the image currently serving.
- **Custom domain** (`quantum.kiefer.xyz` now, `quantum.sweetvalley.net` later): verify the domain in Google Search Console with the kiefer.xyz account, then Cloud Run > the service > Manage custom domains > Add mapping, and add the CNAME it gives you at the DNS host. HTTPS is automatic. Terraform for this can follow once the domain is decided.
- **Logs:** Cloud Run > the service > Logs, or Logs Explorer filtered by `resource.type="cloud_run_revision"`.
- **Cost:** see "Cost guardrails" below. Idle is about $0 to $2 a month with the defaults.

## Cost guardrails

Everything here is sized so that doing nothing costs nothing, normal use
costs a few dollars, and abuse is capped.

| Item | Idle | Normal use (a few people, daily) | Hard ceiling |
|---|---|---|---|
| Cloud Run (`min_instances = 0`, `max_instances = 2`, CPU billed only during requests) | $0 | $1 to $5 | 2 instances busy 24x7, about $150/month. Can't exceed this. |
| Firestore (single region, free tier 50k reads / 20k writes per day) | $0 | $0 | Not a realistic risk at this data size. |
| Artifact Registry (5 images kept) | under $1 | under $1 | Bounded by the cleanup policy. |
| Cloud Storage (two CSVs, 5 versions kept) | under $0.10 | under $0.10 | Bounded by the lifecycle rule. |
| Secret Manager, Logging, Workload Identity, APIs | $0 | $0 | Free tiers cover this. |
| GitHub Actions | $0 | $0 | 2,000 free minutes/month on a private repo; a build uses about 4. |

What the two knobs do:

- `min_instances`: 0 means scale to zero and pay nothing between visits, with a ~10 s cold start on the first page view after idle. Set to 1 for the board-meeting week so the demo is instant; that costs about $8/month and can go back to 0 after.
- `max_instances`: the only way this bill can grow is more instances, and this caps them at 2. Even a bot hammering the login page cannot push it past the ceiling in the table.

**Set a budget alert.** Google budgets email, they don't stop spend, but
they're the early warning. Either set `billing_account_id` in tfvars (needs
Billing Account Costs Manager, which only Jack's account has by default), or
Jack does it once in the console: Billing > Budgets & alerts > Create
budget > this project > $25/month > alerts at 50%, 90%, 100%. Takes two
minutes and is worth doing before the first apply.

**Nothing here has a per-hour charge when unused.** There is no VM, no load
balancer, no NAT, no database instance. If the project is abandoned, the
bill goes to zero on its own. To be certain, `tofu destroy` removes
everything except the Firestore data, which is protected.

## Notes and caveats

- **Deployed 2026-10-30.** URL: https://qxc-equipment-intelligence-kjqejtfiiq-uc.a.run.app. Revision image tag `20261030-1509`. Org-policy exception, Firestore (us-central1, Native), secret, runtime and deploy service accounts, Workload Identity pool, and the $25 budget all exist. GitHub repository variables are not set yet, so pushes to `main` don't deploy until step 4 is done.
- **Local credentials need a quota project** for the Org Policy and Billing Budgets APIs. The provider sets `user_project_override` and `billing_project`, and `gcloud auth application-default set-quota-project quantum-513214` was run once; both are harmless to repeat.

- **Firestore path is Bret's code, exercised for the first time on this deploy.** Every write function has a Firestore branch and it reads cleanly, but it has not run against a real database from this repo. The first smoke test after deploy (add a note, link a contact, mark viewed, redeploy, check they're still there) is the verification.
- **Shared password over HTTPS** is acceptable for a short evaluation period, not for real use. Replace with SSO (next phase).
- **Terraform state** is local by default. Once a second person applies, uncomment the GCS backend in `versions.tf`.

## Walkthrough order for the screen-share

1. Show the repo: `Dockerfile`, `deploy/terraform/`, `.github/workflows/deploy.yml`, this file.
2. `docker compose up --build` locally, open http://localhost:8080, show the login and `/health`.
3. Cloud console: project, billing, IAM (you as Owner), the org-policy role.
4. The targeted `tofu apply`, then the CSV upload, then the image build and push. Show the image in Artifact Registry.
5. The full `tofu apply`. Walk the console while it runs: Secret Manager, Firestore, Cloud Run.
6. Open the URL, log in as editor, add a note; log in as viewer, show it's read-only.
7. Firestore console: show the note landed in the `notes` collection.
8. GitHub: add the seven repository variables, push a trivial commit, watch the Deploy action roll the service.

## Next phase (the gap list)

1. SSO login (step two in Jack's words): Identity-Aware Proxy in front of Cloud Run, allowing named Google accounts and eventually whole domains (Quantum Exchange's included), replacing the shared logins. Little or no app code.
2. Data refresh program (Jack's ask): scheduled pull from USAspending into the bucket, rebuild, redeploy, with the last-update date surfaced in the UI rather than only on `/health`.
3. Custom domain on Cloud Run.
4. CSRF protection on the write routes once there are real user sessions.
5. Remote Terraform state in GCS.
