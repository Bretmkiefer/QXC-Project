"""
Refresh the award data: fold new export files into the source CSVs held in the
cloud data bucket, without creating repeats. See docs/data-refresh.md.

    # what would change? (nothing is written anywhere)
    python scripts/refresh_data.py --incoming path/to/new-files/

    # actually update the bucket copies
    python scripts/refresh_data.py --incoming path/to/new-files/ --apply

Where the new files come from is deliberately not part of this script: put the
exports in a folder (any mix of .csv/.xlsx, prime transactions and/or
subawards) or set --fetch-cmd to a command that drops them there. Everything
after that (checking, de-duplicating, appending, uploading) is automatic.

After --apply, run the Deploy workflow (GitHub > Actions > Deploy > Run
workflow) so the image is rebuilt from the updated CSVs.

For testing without cloud access, point --existing-dir at a folder holding the
two current CSVs instead of --bucket.
"""
import argparse
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import merge_new_records as mnr  # same folder

BUCKET_DEFAULT = "gs://quantum-513214-qxc-equipment-intelligence-data"
FILES = {"prime": "prime_transactions_checkpoint_1.csv", "subawards": "subawards_checkpoint_1.csv"}


def gcloud(*args):
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd")
    if not exe:
        sys.exit("gcloud not found. Install the Google Cloud CLI and run `gcloud auth login`, or use --existing-dir.")
    subprocess.run([exe, "storage", "cp", *args], check=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", type=Path, required=True, help="folder of new export files")
    ap.add_argument("--bucket", default=BUCKET_DEFAULT, help="data bucket holding raw/ (default: the project bucket)")
    ap.add_argument("--existing-dir", type=Path, help="use local copies of the two CSVs instead of the bucket")
    ap.add_argument("--fetch-cmd", help="command that writes new exports into --incoming before merging")
    ap.add_argument("--apply", action="store_true", help="write results (default is a dry run)")
    ap.add_argument("--allow-missing", action="store_true", help="accept files lacking columns the build needs")
    args = ap.parse_args()

    args.incoming.mkdir(parents=True, exist_ok=True)
    if args.fetch_cmd:
        print(f"fetching: {args.fetch_cmd}")
        subprocess.run(args.fetch_cmd, shell=True, check=True)

    work = Path(tempfile.mkdtemp(prefix="qxc-refresh-"))
    state = {}  # kind -> (header, rows)
    for kind, name in FILES.items():
        dest = work / name
        if args.existing_dir:
            shutil.copy(args.existing_dir / name, dest)
        else:
            gcloud(f"{args.bucket}/raw/{name}", str(dest))
        state[kind] = mnr.read_csv(dest)
    before = {k: len(v[1]) for k, v in state.items()}

    new_counts = {k: 0 for k in FILES}
    incoming = sorted(p for p in args.incoming.iterdir() if p.suffix.lower() in (".csv", ".xlsx"))
    if not incoming:
        print(f"No .csv/.xlsx files in {args.incoming}. Nothing to do.")
        return
    for path in incoming:
        header, rows = mnr.read_any(path)
        kind = mnr.detect_kind(header)
        missing = [c for c in mnr.REQUIRED[kind] if c not in header]
        if missing and not args.allow_missing:
            print(f"SKIPPED {path.name}: missing {len(missing)} required columns ({', '.join(missing[:5])}...)")
            continue
        ex_header, ex_rows = state[kind]
        new_rows, dupes, revisions = mnr.merge(kind, ex_header, ex_rows, header, rows)
        state[kind] = (ex_header, ex_rows + new_rows)
        new_counts[kind] += len(new_rows)
        print(f"{path.name}: {kind}, {len(rows)} rows -> {len(new_rows)} new, {dupes} already present, {len(revisions)} revised (not added)")

    print("\nSummary")
    for kind in FILES:
        print(f"  {kind}: {before[kind]} rows -> {before[kind] + new_counts[kind]} (+{new_counts[kind]})")
    if not any(new_counts.values()):
        print("No new records. Nothing to upload.")
        return
    if not args.apply:
        print("\nDry run. Nothing was written. Re-run with --apply to update the CSVs.")
        return
    for kind, name in FILES.items():
        if not new_counts[kind]:
            continue
        out = work / ("merged_" + name)
        mnr.write_csv(out, *state[kind])
        if args.existing_dir:
            shutil.copy(out, args.existing_dir / name)
            print(f"updated {args.existing_dir / name}")
        else:
            gcloud(str(out), f"{args.bucket}/raw/{name}")  # bucket is versioned: the old copy is kept
            print(f"uploaded {name} to {args.bucket}/raw/")
    print("\nDone. Now run the Deploy workflow so the app is rebuilt with the new records.")


if __name__ == "__main__":
    main()
