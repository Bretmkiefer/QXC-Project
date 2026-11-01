# Refreshing the award data

How new records get into the app without creating repeats. Internal runbook;
it does not name any data source.

## How the data flows

```
new export file(s)  ->  scripts/refresh_data.py  ->  two CSVs in the cloud bucket (raw/)
                                                          |
                          Deploy workflow rebuilds the image  <-+
                                                          |
                                       app shows "Data through <date>" on the home page
```

- The app serves a read-only database that is **rebuilt from the two CSVs
  every time the Deploy workflow runs**. Everything people type in (notes,
  contacts, enrichment, viewed flags) lives separately in Firestore and is
  never touched by a refresh.
- The bucket is versioned, so every upload keeps the previous CSV and a bad
  refresh can be rolled back by restoring the earlier version and redeploying.

## Steps for a refresh

1. Get the new export(s) into a folder. Any mix of `.csv` / `.xlsx`, prime
   transactions and/or subawards, with the same column layout as the current
   files. A file missing columns the build needs is refused (otherwise new
   rows would load with blank offices and companies).
2. Dry run, which changes nothing:
   `python scripts/refresh_data.py --incoming <folder>`
3. Read the summary: rows already present, **new** rows, and "revised" rows
   (same transaction already on file with a different amount, date or
   company; these are listed, never appended and never silently dropped).
4. Apply: `python scripts/refresh_data.py --incoming <folder> --apply`
   (needs `gcloud auth login` as a user with write access to the bucket).
5. GitHub > Actions > Deploy > Run workflow.
6. Check the home page: "Data through ..." should have moved, and `/health`
   shows the new record counts.

`scripts/merge_new_records.py` does the same for one file, for ad-hoc use.

## Rules the tools enforce

- **Existing rows are never changed, dropped or reordered; new rows are only
  appended.** Subaward ids in the app are the row's position in the file
  (`sub-0`, `sub-1`, ...), so reordering would detach notes and links.
- **What counts as "already present"**
  - prime transactions: award id + parent award id + modification +
    transaction number + recipient UEI + obligated amount + action date
  - subawards: prime award id + subaward number + subawardee UEI + amount +
    action date
  - dates and amounts are compared normalized, so `9/19/24`, `2024-09-19` and
    an Excel date serial all match.
- Running the same refresh twice adds nothing the second time.

## Known data quirks

- **Duplicate subawards in the source.** 21 rows (11 groups) repeat an earlier
  row exactly, worth about $7.9M. The database build now drops these (keeping
  the first, so ids are stable). Subaward total went from $320.3M to $312.4M
  and the count from 799 to 778.
- **Award ids are not always unique (handled).** 14 award ids are reused by
  different companies under different parent contracts, mostly at one health
  agency (about $16.9M). The build now treats rows as one award only when they
  share the award id, the recipient and the parent contract (a blank parent
  joins the recipient's only parent), so each company's award is its own
  record: 5,147 awards became 5,164, with dollars unchanged to the cent.
  The first award seen in the file keeps the plain id as its record key and
  later ones become `ID~2`, `ID~3`, so no existing record id changed. Screens
  and exports always show the real award id, never the `~` key. Because the
  source only grows by appending, an award's key does not change on a
  refresh. Limit: if an award's company genuinely changes mid-life (a
  novation), the data cannot tell that apart from a reused id and it will be
  split into two records.
- Offices can change across the life of an award, so one award id can show
  more than one awarding office code. This is expected, not a duplicate.

## The monthly SAM.gov refresh

`scripts/fetch_sam_awards.py` pulls new prime awards from the SAM.gov Contract
Awards API; `scripts/refresh_data.py --fetch-sam` then merges them with the
steps above. Intended cadence: **about once a month**.

What it does
- Asks for awards (not IDVs) matching the search text (default `cisco`, set
  with `QXC_SAM_QUERY`) signed on or after **the current data-through date
  minus 45 days**. The overlap catches late-reported records; whatever is
  already on file is dropped by the merge.
- Maps the response to our column layout. The API has no subaward data, no
  company phone and no base award description; those stay blank, so
  subawards still need a manual file.
- The API key is read **only** from the `SAM_API_KEY` environment variable
  (Secret Manager on the job). It is not accepted as an argument and the
  request URL, which carries it, is never printed or logged.

Safety checks (also in a dry run). The job exits with code **3** and uploads
nothing if: zero new prime records (`--allow-zero` to accept a quiet month);
more new records than max(1,000, 20% of the rows on file) (`--spike-floor`,
`--max-new-pct`); a new record dated in the future or before the fetch window;
or more than 10% of fetched rows lack a piid, date, UEI or amount (the API
shape probably changed). Exit code 2 means the fetch itself failed (no key,
403/400 from SAM.gov, a network error, or the call cap was hit).

Before the first scheduled run, someone with the key should check the mapping
once, because it was written from the API documentation and has not yet been
run against the live service:

```bash
SAM_API_KEY=<key> python scripts/fetch_sam_awards.py --probe
```

It fetches one record and lists which of our columns were filled and which
came back empty. Then do a full dry run (`--fetch-sam`, no `--apply`) and
look at the new/already-present counts before enabling the schedule.

Things to know
- **Quota:** a personal key with no SAM.gov role gets only **10 calls a day**
  (100 records each); with a role, 1,000. `--max-calls` (default 40) stops the
  run before it can exhaust a quota; a monthly run normally needs a handful.
- **Recent DoD awards** are withheld by the API for 90 days, so they arrive
  on a later run; the overlap window covers this.
- **Coverage may not match the existing files.** They were built by a
  different selection, and the API's text search will not return exactly the
  same set. Expect the first runs to add records the old files lacked, and
  review the counts instead of assuming parity.
- The job needs **write access to the data bucket** and, to rebuild the app
  automatically, a GitHub token that can start the Deploy workflow
  (`GITHUB_DISPATCH_TOKEN`, `--trigger-deploy`). Without the token it still
  updates the bucket and says to run Deploy by hand.

Hosting (for whoever owns the infrastructure): `Dockerfile.jobs` builds the job
image; `deploy/terraform/jobs.tf.example` sketches the Cloud Run job, Cloud
Scheduler cron (`0 6 3 * *`), separate service accounts and the secrets. It
is an unvalidated example, not applied.

## Not yet verified against the real bucket

The merge, refresh, safety checks and SAM.gov fetch are tested end to end on
local copies of the current files with a simulated API (new rows, repeats,
revisions, thin files, running twice, spike, future dates, bad key, quota cap,
retries). Not yet run for real: the live SAM.gov API (use `--probe`), the
upload to the bucket, and the deploy trigger.
