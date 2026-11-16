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

## Vendors and the query

`scripts/vendors.py` is the one place that says which vendors the data covers:
Cisco, Palo Alto Networks, Juniper Networks, Aruba Networks, HPE, Arista
Networks and Fortinet. Each entry lists the search keywords for that vendor
(Fortinet is searched as both `fortinet` and `fortigate`) and the rules that
separate its equipment from everything else the keyword matches. Both fetch
scripts below search for every keyword in that file, so **the query is that
list**.

The rules exist because a keyword search is broad. Real examples the rules
remove: about 290 rows for the VA Palo Alto hospital (janitorial, imaging,
furniture), about 1,900 "juniper" rows for an unrelated company, a Coast Guard
cutter and forestry work, and 966 orders of a surgical hemostat called Arista
AH. Real examples they keep: network gear bought through military logistics
contracts, which is coded as marine lifesaving equipment. Cisco is not
filtered, so its numbers match what was already presented.

- **Add a vendor:** append an entry to `VENDORS` in `scripts/vendors.py`, add a
  few real descriptions to `_SAMPLES` there with the verdict each should get,
  run `python scripts/vendors.py` (it must print every sample as `ok`), then
  refresh. A first load of a new vendor is a deliberate spike, so pass
  `--spike-floor` above the number of new rows the dry run reports.
- **Tune a rule:** the fetch logs, per vendor, how many rows each rule dropped.
  If a count looks wrong, pull that vendor with `--raw-dir` (below), look at
  the unfiltered file, adjust the rule and the samples.
- HPE-branded spend is mostly servers and storage. Those rows are kept
  (`skip_compute_only` is off) on the view that extra rows beat missing ones;
  turning it on removes about 450 transactions.

## Refreshing every vendor (the working path today)

`scripts/fetch_vendor_exports.py` is the automated form of the manual export
the original two files came from. It downloads the whole period for every
vendor keyword, filters each row by its vendor's rules and writes two CSVs for
the steps above. It needs no API key, takes about ten minutes, and includes
subawards. The export service's address is not stored in the repository: set
`QXC_EXPORT_API` to it before running (whoever runs refreshes has it).

```bash
# dry run: fetch, then show what would be added (changes nothing)
python scripts/refresh_data.py --incoming /tmp/incoming \
    --fetch-cmd "python scripts/fetch_vendor_exports.py --out /tmp/incoming"

# then the same with --apply, and run the Deploy workflow
```

`--vendors aruba juniper` limits it to some vendors; `--raw-dir DIR` also
keeps each vendor's unfiltered download for inspection. Fetching the whole
period every time is intended: rows already on file are dropped by the merge,
and it is what picks up late-published records and subawards.

History: on 2026-11-16 this path loaded the six vendors beyond Cisco and
brought Cisco up to date. Prime transactions went from 6,628 to 10,724 rows
and subawards from 799 to 1,332; the app went from 5,164 to 8,146 awards.

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
- Asks for awards (not IDVs) matching the query, signed on or after **the
  current data-through date minus 45 days**. The query is every vendor keyword
  in `scripts/vendors.py`, one search each; results are filtered by the
  vendor's rules and a record two searches both return is written once.
  `QXC_SAM_QUERY` overrides the list with comma-separated search texts (for
  example `QXC_SAM_QUERY=cisco` for the previous behavior). The overlap
  catches late-reported records; whatever is already on file is dropped by the
  merge.
- This API returns no base award description, so the rules judge each row on
  its transaction description alone. Expect it to keep slightly fewer rows
  than the export path for the same period.
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
  (100 records each); with a role, 1,000. The query is now nine searches and
  each costs at least one call, so a run needs nine calls before any paging:
  in practice **the full vendor list needs a key with a role**. `--max-calls`
  (default 40) is one budget shared by all the searches and stops the run
  before it can exhaust a quota. With only a personal key, use the export
  path above, or set `QXC_SAM_QUERY` to fewer terms.
- Whether the API's text search treats `palo alto` and `hewlett packard
  enterprise` as phrases has not been checked against the live service. Try
  one with `--probe --query "palo alto"` before trusting the counts.
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
retries; and for the multi-vendor query: one search per keyword, vendor rules
applied, a record returned by two searches written once, the `QXC_SAM_QUERY`
override, and the shared call budget).

Run for real on 2026-11-16: the export path (`fetch_vendor_exports.py`), the
merge against the bucket, the upload, and a Deploy run started by hand. Not
yet run for real: the live SAM.gov API (use `--probe`) and the automatic
deploy trigger.
