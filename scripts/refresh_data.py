"""
Refresh the award data: fold new records into the source CSVs held in the cloud
data bucket, without creating repeats, and stop if the result looks wrong.
See docs/data-refresh.md.

    # what would change? (nothing is written anywhere)
    python scripts/refresh_data.py --incoming path/to/new-files/

    # the monthly job: fetch from SAM.gov, check, update the bucket, start a rebuild
    SAM_API_KEY=... python scripts/refresh_data.py --fetch-sam --incoming /tmp/new --apply --trigger-deploy

Where new files come from: either drop exports into --incoming (any mix of
.csv/.xlsx) or add --fetch-sam to pull them from SAM.gov first. Everything
after that (checking, de-duplicating, appending, uploading) is automatic.

Safety checks (always run, also in a dry run). The job STOPS with exit code 3
and uploads nothing when:
  * no new prime records were found (override with --allow-zero),
  * the number of new records is a spike: more than max(--spike-floor,
    --max-new-pct percent of the rows already on file),
  * any new record is dated in the future, or before the fetch window.
A stop means "a person should look", so a scheduler can alert on it.

After --apply the database must be rebuilt from the updated CSVs: add
--trigger-deploy (needs GITHUB_DISPATCH_TOKEN) or run the Deploy workflow by hand.

For testing without cloud access, point --existing-dir at a folder holding the
two current CSVs instead of --bucket.
"""
import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

import fetch_sam_awards as sam  # same folder
import merge_new_records as mnr  # same folder

BUCKET_DEFAULT = "gs://quantum-513214-qxc-equipment-intelligence-data"
FILES = {"prime": "prime_transactions_checkpoint_1.csv", "subawards": "subawards_checkpoint_1.csv"}
EXIT_SAFETY_STOP = 3


def log(event: str, **fields):
    """One JSON object per line: Cloud Logging turns these into searchable entries."""
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


# -------------------------------------------------------------- bucket --
def _split(bucket_uri: str):
    if not bucket_uri.startswith("gs://"):
        sys.exit("--bucket must look like gs://name")
    return bucket_uri[5:].rstrip("/")


def bucket_download(bucket_uri: str, name: str, dest: Path):
    try:
        from google.cloud import storage  # present in the job image
        storage.Client().bucket(_split(bucket_uri)).blob(f"raw/{name}").download_to_filename(str(dest))
        return
    except ImportError:
        pass
    _gcloud(f"{bucket_uri}/raw/{name}", str(dest))


def bucket_upload(bucket_uri: str, name: str, src: Path):
    """The bucket is versioned, so the previous copy of the file is kept."""
    try:
        from google.cloud import storage
        storage.Client().bucket(_split(bucket_uri)).blob(f"raw/{name}").upload_from_filename(str(src))
        return
    except ImportError:
        pass
    _gcloud(str(src), f"{bucket_uri}/raw/{name}")


def _gcloud(*args):
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        sys.exit("Neither google-cloud-storage nor gcloud is available. Install one, or use --existing-dir.")
    subprocess.run([exe, "storage", "cp", *args], check=True)


# -------------------------------------------------------------- safety --
def check_safety(new_rows: list, existing_count: int, since, today, max_new_pct=20.0, spike_floor=1000, allow_zero=False):
    """Problems that should stop the job (empty list = fine)."""
    problems = []
    n = len(new_rows)
    if n == 0 and not allow_zero:
        problems.append("zero new prime records: the fetch may have failed silently or the source changed.")
    limit = max(spike_floor, existing_count * max_new_pct / 100.0)
    if n > limit:
        problems.append(f"{n} new prime records is a spike (limit {int(limit)}, i.e. more than {max_new_pct:g}% of the {existing_count} on file).")
    dates = [mnr.norm_date(r.get("action_date", "")) for r in new_rows]
    dates = [d for d in dates if len(d) == 10]
    if dates:
        if max(dates) > (today + datetime.timedelta(days=30)).isoformat():
            problems.append(f"a new record is dated {max(dates)}, in the future.")
        if since and min(dates) < since.isoformat():
            problems.append(f"a new record is dated {min(dates)}, before the fetch window starting {since}.")
    return problems


# --------------------------------------------------------------- deploy --
def trigger_deploy(repo: str, token: str, transport=None):
    """Start the Deploy workflow so the app is rebuilt from the updated CSVs."""
    url = f"https://api.github.com/repos/{repo}/actions/workflows/deploy.yml/dispatches"
    body = json.dumps({"ref": "main"}).encode()
    if transport:
        return transport(url, body)
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "User-Agent": "qxc-refresh"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.status


# ----------------------------------------------------------------- main --
def run(argv=None, sam_transport=None, deploy_transport=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", type=Path, required=True, help="folder of new export files")
    ap.add_argument("--bucket", default=BUCKET_DEFAULT, help="data bucket holding raw/ (default: the project bucket)")
    ap.add_argument("--existing-dir", type=Path, help="use local copies of the two CSVs instead of the bucket")
    ap.add_argument("--fetch-sam", action="store_true", help="first pull new awards from SAM.gov (needs SAM_API_KEY)")
    ap.add_argument("--overlap-days", type=int, default=45)
    ap.add_argument("--max-calls", type=int, default=40, help="SAM.gov calls allowed this run")
    ap.add_argument("--fetch-cmd", help="command that writes new exports into --incoming (other sources)")
    ap.add_argument("--max-new-pct", type=float, default=float(os.environ.get("QXC_REFRESH_MAX_NEW_PCT", 20)))
    ap.add_argument("--spike-floor", type=int, default=int(os.environ.get("QXC_REFRESH_SPIKE_FLOOR", 1000)))
    ap.add_argument("--allow-zero", action="store_true", help="a run with no new records is not an error")
    ap.add_argument("--apply", action="store_true", help="write results (default is a dry run)")
    ap.add_argument("--trigger-deploy", action="store_true", help="after --apply, start the Deploy workflow")
    ap.add_argument("--allow-missing", action="store_true", help="accept files lacking columns the build needs")
    args = ap.parse_args(argv)

    args.incoming.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix="qxc-refresh-"))
    log("start", apply=args.apply, fetch_sam=args.fetch_sam, existing_dir=bool(args.existing_dir))

    state = {}
    for kind, name in FILES.items():
        dest = work / name
        if args.existing_dir:
            shutil.copy(args.existing_dir / name, dest)
        else:
            bucket_download(args.bucket, name, dest)
        state[kind] = mnr.read_csv(dest)
    before = {k: len(v[1]) for k, v in state.items()}
    log("loaded_existing", **before)

    since = None
    if args.fetch_sam:
        key = os.environ.get("SAM_API_KEY", "").strip()
        if not key:
            log("fetch_failed", reason="SAM_API_KEY is not set")
            return 2
        try:
            since, rows = sam.fetch_new_awards(
                key, work / FILES["prime"], args.overlap_days,
                transport=sam_transport or sam.http_get, max_calls=args.max_calls, log=lambda s: print(s, flush=True))
        except sam.FetchError as e:
            log("fetch_failed", reason=str(e))
            return 2
        sam.write_rows(args.incoming / f"sam_awards_{datetime.date.today():%Y%m%d}.csv", rows)
        log("fetched", rows=len(rows), since=since)
    if args.fetch_cmd:
        subprocess.run(args.fetch_cmd, shell=True, check=True)

    new_counts = {k: 0 for k in FILES}
    new_prime_rows = []
    saw_prime_input = bool(args.fetch_sam)
    incoming = sorted(p for p in args.incoming.iterdir() if p.suffix.lower() in (".csv", ".xlsx"))
    for path in incoming:
        header, rows = mnr.read_any(path)
        kind = mnr.detect_kind(header)
        missing = [c for c in mnr.REQUIRED[kind] if c not in header]
        if missing and not args.allow_missing:
            log("file_skipped", file=path.name, missing=len(missing), sample=missing[:5])
            continue
        ex_header, ex_rows = state[kind]
        new_rows, dupes, revisions = mnr.merge(kind, ex_header, ex_rows, header, rows)
        state[kind] = (ex_header, ex_rows + new_rows)
        new_counts[kind] += len(new_rows)
        if kind == "prime":
            saw_prime_input = True
            new_prime_rows += new_rows
        log("merged", file=path.name, kind=kind, incoming=len(rows), new=len(new_rows), already_present=dupes, revised=len(revisions))

    if not incoming and not args.fetch_sam:
        print(f"No .csv/.xlsx files in {args.incoming}. Nothing to do.")
        return 0
    # The checks guard the prime-record feed, so they apply whenever one was fetched or supplied.
    problems = check_safety(new_prime_rows, before["prime"], since, datetime.date.today(),
                            args.max_new_pct, args.spike_floor, args.allow_zero) if saw_prime_input else []

    print("\nSummary")
    for kind in FILES:
        print(f"  {kind}: {before[kind]} rows -> {before[kind] + new_counts[kind]} (+{new_counts[kind]})")
    if problems:
        for p in problems:
            log("safety_stop", reason=p)
        print("\nSAFETY STOP: nothing was uploaded.\n  - " + "\n  - ".join(problems))
        return EXIT_SAFETY_STOP
    if not any(new_counts.values()):
        print("No new records. Nothing to upload.")
        return 0
    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to update the CSVs.")
        return 0

    for kind, name in FILES.items():
        if not new_counts[kind]:
            continue
        out = work / ("merged_" + name)
        mnr.write_csv(out, *state[kind])
        if args.existing_dir:
            shutil.copy(out, args.existing_dir / name)
            log("updated_local", file=name)
        else:
            bucket_upload(args.bucket, name, out)
            log("uploaded", file=name, bucket=args.bucket)
    if args.trigger_deploy:
        token = os.environ.get("GITHUB_DISPATCH_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "Bretmkiefer/QXC-Project")
        if not token and not deploy_transport:
            log("deploy_not_triggered", reason="GITHUB_DISPATCH_TOKEN is not set")
            print("Updated the data, but could not start the rebuild: run the Deploy workflow by hand.")
            return 0
        status = trigger_deploy(repo, token, deploy_transport)
        log("deploy_triggered", status=status)
    else:
        print("\nDone. Now run the Deploy workflow so the app is rebuilt with the new records.")
    return 0


if __name__ == "__main__":
    sys.exit(run())
