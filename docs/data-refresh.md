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

## Scheduling (not set up)

Collecting new exports automatically needs a decision on where they come from
and how often. Two reasonable shapes, both calling the steps above:

- Cloud Scheduler -> a Cloud Run job that fetches, runs `refresh_data.py
  --apply`, then triggers the Deploy workflow; or
- a scheduled GitHub Actions workflow doing the same.

`refresh_data.py --fetch-cmd "<command>"` is the hook: the command writes new
exports into the `--incoming` folder, and the merge, upload and checks run
unchanged. `--fetch-cmd` is untested against a real feed because none is
configured yet.

## Not yet verified against the real bucket

The merge and refresh logic is tested end to end on local copies of the
current files (new rows, repeats, revisions, thin files, running twice). The
upload to the bucket uses `gcloud storage cp` and has not been run.
