"""
Fetch the award export for every vendor in scripts/vendors.py and write it as
two CSVs (prime transactions, subawards) ready for scripts/refresh_data.py.
See docs/data-refresh.md.

    # write the files, then look at what a refresh would add (changes nothing)
    python scripts/fetch_vendor_exports.py --out /tmp/incoming
    python scripts/refresh_data.py --incoming /tmp/incoming

    # or in one step, as the refresh tool's fetch command
    python scripts/refresh_data.py --incoming /tmp/incoming \\
        --fetch-cmd "python scripts/fetch_vendor_exports.py --out /tmp/incoming"

This is the automated form of the manual export the original two files came
from: one keyword download per vendor keyword, covering the whole period each
time. It needs no API key and, unlike the monthly award fetch, it includes
subawards and the base award description. The export service's base address is
not kept in this repository: set it in the QXC_EXPORT_API environment variable
(whoever runs refreshes has it). Every row is then passed through its
vendor's rules (vendors.keep_row), so a refresh only ever sees rows that are
about that vendor's equipment. Rows already on file are dropped by the merge,
so fetching the whole period again costs nothing but about ten minutes.

    --vendors aruba juniper     only these vendors
    --start 2023-10-01          first action date (default: where the data starts)
    --raw-dir DIR               also keep each vendor's unfiltered download there
    --reuse-raw                 read the downloads from --raw-dir instead of fetching

Standard library only, so it runs on a laptop, in CI or in the job image.
"""
import argparse
import csv
import datetime
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import vendors  # same folder

# Base address of the export service. Deliberately has no default: the data
# source is not named in this repository.
EXPORT_API = os.environ.get("QXC_EXPORT_API", "").strip().rstrip("/")
DATA_START = "2023-10-01"
# Contracts plus indefinite-delivery vehicles: what the original files hold.
AWARD_TYPE_CODES = [
    "A", "B", "C", "D",
    "IDV_A", "IDV_B", "IDV_B_A", "IDV_B_B", "IDV_B_C", "IDV_C", "IDV_D", "IDV_E",
]
TRANSACTION_KEY = "contract_transaction_unique_key"

csv.field_size_limit(10**9)


def log(event: str, **fields):
    """One JSON object per line, like refresh_data.py."""
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


class FetchError(Exception):
    pass


def _request(url, body=None, timeout=120, retries=4):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, data=data, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code < 500 and exc.code != 429:
                raise FetchError(f"export service refused the request (HTTP {exc.code})") from None
            last = f"HTTP {exc.code}"
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = type(exc).__name__
        time.sleep(5 * (attempt + 1))
    raise FetchError(f"export service unreachable after {retries} attempts ({last})")


def download_keyword(keyword, start, end, poll_seconds=8, max_wait=1800):
    """One keyword download -> (tx_header, tx_rows, sub_header, sub_rows)."""
    filters = {
        "keywords": [keyword],
        "time_period": [{"start_date": start, "end_date": end}],
        "award_type_codes": AWARD_TYPE_CODES,
    }
    job = json.loads(_request(f"{EXPORT_API}/api/v2/download/transactions/", {"filters": filters, "file_format": "csv"}))
    waited = 0
    while True:
        status = json.loads(_request(job["status_url"]))
        if status.get("status") == "finished":
            break
        if status.get("status") == "failed":
            raise FetchError(f"the export for {keyword!r} failed on the service side")
        if waited >= max_wait:
            raise FetchError(f"the export for {keyword!r} was not ready after {max_wait}s")
        time.sleep(poll_seconds)
        waited += poll_seconds

    archive = zipfile.ZipFile(io.BytesIO(_request(job["file_url"], timeout=600)))
    tx_header, tx_rows, sub_header, sub_rows = None, [], None, []
    for name in sorted(archive.namelist()):
        if not name.startswith("Contracts_"):
            continue  # Assistance_* files are grants and loans, not equipment purchases
        reader = csv.reader(io.TextIOWrapper(archive.open(name), encoding="utf-8", errors="replace", newline=""))
        header = next(reader, None)
        if header is None:
            continue
        if "PrimeTransactions" in name:
            if tx_header and header != tx_header:
                raise FetchError("the prime transactions column layout changed between files")
            tx_header = header
            tx_rows.extend(reader)
        elif "Subawards" in name:
            if sub_header and header != sub_header:
                raise FetchError("the subawards column layout changed between files")
            sub_header = header
            sub_rows.extend(reader)
    return tx_header, tx_rows, sub_header, sub_rows


def _read(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = next(reader, None)
        return header, list(reader)


def _write(path: Path, header, rows):
    tmp = path.with_suffix(path.suffix + ".part")
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)
    tmp.replace(path)


def _merge_downloads(parts):
    """Combine several keyword downloads for one vendor. Prime rows are
    unique by transaction key; a subaward row that two keywords both return
    is kept as many times as it appears in the fuller of the two."""
    header, rows, seen = None, [], set()
    for part_header, part_rows in parts:
        if part_header is None:
            continue
        if header and part_header != header:
            raise FetchError("column layout differs between keyword downloads")
        header = part_header
        key_idx = header.index(TRANSACTION_KEY) if TRANSACTION_KEY in header else None
        counts_have = {}
        if key_idx is None:
            for r in rows:
                counts_have[tuple(r)] = counts_have.get(tuple(r), 0) + 1
        counts_new = {}
        for r in part_rows:
            if key_idx is not None:
                if r[key_idx] in seen:
                    continue
                seen.add(r[key_idx])
                rows.append(r)
            else:
                t = tuple(r)
                counts_new[t] = counts_new.get(t, 0) + 1
                if counts_new[t] > counts_have.get(t, 0):
                    rows.append(r)
    return header, rows


def vendor_raw(vendor, start, end, raw_dir, reuse_raw):
    """(tx_header, tx_rows, sub_header, sub_rows) for one vendor, unfiltered."""
    tx_path = raw_dir / f"prime_transactions_{vendor['key']}.csv" if raw_dir else None
    sub_path = raw_dir / f"subawards_{vendor['key']}.csv" if raw_dir else None
    if reuse_raw:
        if not tx_path or not tx_path.exists():
            raise FetchError(f"--reuse-raw: {tx_path} is missing")
        tx_header, tx_rows = _read(tx_path)
        sub_header, sub_rows = _read(sub_path) if sub_path.exists() else (None, [])
        return tx_header, tx_rows, sub_header, sub_rows
    tx_parts, sub_parts = [], []
    for keyword in vendor["keywords"]:
        log("download_start", vendor=vendor["key"], keyword=keyword)
        th, tr, sh, sr = download_keyword(keyword, start, end)
        log("download_done", vendor=vendor["key"], keyword=keyword, transactions=len(tr), subawards=len(sr))
        tx_parts.append((th, tr))
        sub_parts.append((sh, sr))
    tx_header, tx_rows = _merge_downloads(tx_parts)
    sub_header, sub_rows = _merge_downloads(sub_parts)
    if tx_header is None:
        raise FetchError(f"no prime transactions came back for {vendor['key']}")
    if raw_dir:
        _write(tx_path, tx_header, tx_rows)
        if sub_header:
            _write(sub_path, sub_header, sub_rows)
    return tx_header, tx_rows, sub_header, sub_rows


def filter_rows(vendor, header, rows, text_cols, psc_col, naics_col):
    """Rows that pass the vendor's rules, plus {reason: dropped count}."""
    idx = {c: header.index(c) for c in text_cols + [c for c in (psc_col, naics_col) if c] if c in header}
    missing = [c for c in text_cols if c not in idx]
    if missing:
        raise FetchError(f"export is missing the column(s) {missing}; cannot judge its rows")
    kept, dropped = [], {}
    for r in rows:
        text = " ".join(r[idx[c]] for c in text_cols)
        psc = r[idx[psc_col]] if psc_col in idx else ""
        naics = r[idx[naics_col]] if naics_col in idx else ""
        keep, reason = vendors.keep_row(vendor, text, psc, naics)
        if keep:
            kept.append(r)
        else:
            dropped[reason] = dropped.get(reason, 0) + 1
    return kept, dropped


def run(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", type=Path, required=True, help="folder to write the two CSVs into (refresh_data's --incoming)")
    ap.add_argument("--vendors", nargs="*", default=[v["key"] for v in vendors.VENDORS], help="vendor keys (default: all)")
    ap.add_argument("--start", default=DATA_START, help="first action date")
    ap.add_argument("--end", default=datetime.date.today().isoformat(), help="last action date (default: today)")
    ap.add_argument("--raw-dir", type=Path, help="keep each vendor's unfiltered download here")
    ap.add_argument("--reuse-raw", action="store_true", help="read downloads from --raw-dir instead of fetching")
    args = ap.parse_args(argv)

    unknown = [k for k in args.vendors if k not in vendors.VENDOR_BY_KEY]
    if unknown:
        ap.error(f"unknown vendor(s): {', '.join(unknown)}; known: {', '.join(vendors.VENDOR_BY_KEY)}")
    if args.reuse_raw and not args.raw_dir:
        ap.error("--reuse-raw needs --raw-dir")
    if not args.reuse_raw and not EXPORT_API:
        log("fetch_failed", reason="QXC_EXPORT_API is not set")
        print("Set QXC_EXPORT_API to the export service's base address (it is not stored in the repository).", file=sys.stderr)
        return 2
    if args.raw_dir:
        args.raw_dir.mkdir(parents=True, exist_ok=True)

    log("start", vendors=args.vendors, start=args.start, end=args.end, reuse_raw=args.reuse_raw)
    tx_header, tx_all, tx_seen = None, [], set()
    sub_header, sub_all, sub_seen = None, [], set()
    try:
        for key in args.vendors:
            vendor = vendors.VENDOR_BY_KEY[key]
            th, tr, sh, sr = vendor_raw(vendor, args.start, args.end, args.raw_dir, args.reuse_raw)
            tx_kept, tx_dropped = filter_rows(
                vendor, th, tr, ["transaction_description", "prime_award_base_transaction_description"],
                "product_or_service_code", "naics_code")
            # The subaward file carries the prime award's NAICS but no product
            # code, so its IT test is NAICS only.
            sub_kept, sub_dropped = (filter_rows(
                vendor, sh, sr, ["subaward_description", "prime_award_base_transaction_description"],
                None, "prime_award_naics_code") if sh else ([], {}))
            log("vendor_done", vendor=key, transactions=len(tr), transactions_kept=len(tx_kept), transactions_dropped=tx_dropped,
                subawards=len(sr), subawards_kept=len(sub_kept), subawards_dropped=sub_dropped)

            if tx_header and th != tx_header:
                raise FetchError("prime transactions column layout differs between vendors")
            tx_header = th
            key_idx = th.index(TRANSACTION_KEY) if TRANSACTION_KEY in th else None
            for r in tx_kept:  # one order can name two vendors; write it once
                ident = r[key_idx] if key_idx is not None else tuple(r)
                if ident not in tx_seen:
                    tx_seen.add(ident)
                    tx_all.append(r)
            if sh:
                if sub_header and sh != sub_header:
                    raise FetchError("subawards column layout differs between vendors")
                sub_header = sh
                for r in sub_kept:
                    t = tuple(r)
                    if t not in sub_seen:
                        sub_seen.add(t)
                        sub_all.append(r)
    except FetchError as exc:
        log("fetch_failed", reason=str(exc))
        return 2

    args.out.mkdir(parents=True, exist_ok=True)
    stamp = f"{datetime.date.today():%Y%m%d}"
    _write(args.out / f"vendor_prime_transactions_{stamp}.csv", tx_header, tx_all)
    if sub_header:
        _write(args.out / f"vendor_subawards_{stamp}.csv", sub_header, sub_all)
    log("written", folder=str(args.out), transactions=len(tx_all), subawards=len(sub_all))
    return 0


if __name__ == "__main__":
    sys.exit(run())
