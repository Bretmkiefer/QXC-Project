"""
Fetch new prime award records from the SAM.gov Contract Awards API and write
them as a CSV in the same column layout as the existing source file, ready for
scripts/merge_new_records.py / refresh_data.py. See docs/data-refresh.md.

    SAM_API_KEY=... python scripts/fetch_sam_awards.py --probe          # 1 record: check the field mapping
    SAM_API_KEY=... python scripts/fetch_sam_awards.py --existing data/raw/prime_transactions_checkpoint_1.csv --out new/

The key is read ONLY from the SAM_API_KEY environment variable (on the hosted
job it comes from Secret Manager). It is never accepted on the command line and
the request URL, which carries it, is never printed or logged.

What it asks for: awards (not IDVs) signed from a little before the current
data-through date onward that match the search text (default "cisco"). The
small overlap catches records the government reports late; anything already on
file is dropped later by the merge, so overlap costs nothing.

Limits worth knowing (from the API's documentation)
  * 100 records per call; without a SAM.gov role a personal key allows only
    10 calls a day (1,000 with one). --max-calls stops well short of the quota.
  * Civilian awards and DoD awards older than 90 days are returned; very recent
    DoD awards are withheld by the API ("revealed" data), so a few weeks of
    DoD activity can arrive on the next run. The overlap window covers this.
  * The API has no subaward data, no company phone number and no base award
    description: those columns are written blank.
"""
import argparse
import csv
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API_URL = "https://api.sam.gov/contract-awards/v1/search"
PAGE_SIZE = 100
SEARCH_TEXT = os.environ.get("QXC_SAM_QUERY", "cisco")

# Output columns: the ones the database build reads, plus the ones the merge
# uses to recognize a record that is already on file.
COLUMNS = [
    "award_id_piid", "modification_number", "transaction_number", "parent_award_id_piid",
    "federal_action_obligation", "potential_total_value_of_award", "current_total_value_of_award",
    "base_and_all_options_value", "action_date", "period_of_performance_start_date",
    "awarding_agency_name", "awarding_sub_agency_name", "awarding_office_code", "awarding_office_name",
    "funding_agency_name", "funding_sub_agency_name", "funding_office_code", "funding_office_name",
    "recipient_uei", "recipient_name", "recipient_parent_name", "recipient_state_code",
    "recipient_city_name", "recipient_phone_number", "cage_code", "naics_code", "naics_description",
    "product_or_service_code", "product_or_service_code_description", "award_type",
    "type_of_contract_pricing_code", "transaction_description", "prime_award_base_transaction_description",
    "solicitation_identifier",
]
# If more than this share of fetched rows lack one of these, the API's shape
# has probably changed and the job stops instead of loading junk.
CRITICAL = ["award_id_piid", "action_date", "recipient_uei", "federal_action_obligation"]
CRITICAL_MISSING_LIMIT = 0.10


def _get(obj, path, default=""):
    """obj['a']['b'][0]['c'] by 'a.b.0.c', returning default if any step is absent."""
    cur = obj
    for part in path.split("."):
        if isinstance(cur, list):
            try:
                cur = cur[int(part)]
            except (ValueError, IndexError):
                return default
        elif isinstance(cur, dict) and part in cur:
            cur = cur[part]
        else:
            return default
    return default if cur is None else cur


def _first(obj, *paths):
    for p in paths:
        v = _get(obj, p)
        if v not in ("", None):
            return v
    return ""


def _date_part(value) -> str:
    """'2025-02-20T00:00:00Z' or '2025-02-20 00:00:00.000' -> '2025-02-20'."""
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(value or ""))
    return m.group(0) if m else ""


def _looks_like_code(text: str) -> bool:
    return bool(re.fullmatch(r"[A-Z0-9\-]{1,8}", str(text or "").strip()))


def _name_code(obj, base):
    """(code, name) of an org node. The published schema has a sub-agency's
    name and code descriptions swapped, so if the 'name' looks like a code and
    the 'code' looks like a name, swap them back."""
    code, name = str(_get(obj, base + ".code")).strip(), str(_get(obj, base + ".name")).strip()
    if name and code and _looks_like_code(name) and not _looks_like_code(code):
        code, name = name, code
    return code, name


def map_record(rec: dict) -> dict:
    """One awardSummary entry -> one row in our column layout."""
    org = "coreData.federalOrganization."
    c_dept = _name_code(rec, org + "contractingInformation.contractingDepartment")
    c_sub = _name_code(rec, org + "contractingInformation.contractingSubtier")
    c_off = _name_code(rec, org + "contractingInformation.contractingOffice")
    f_dept = _name_code(rec, org + "fundingInformation.fundingDepartment")
    f_sub = _name_code(rec, org + "fundingInformation.fundingSubtier")
    f_off = _name_code(rec, org + "fundingInformation.fundingOffice")
    naics = _get(rec, "coreData.productOrServiceInformation.principalNaics", [])
    naics = naics[0] if isinstance(naics, list) and naics else (naics if isinstance(naics, dict) else {})
    awardee = "awardDetails.awardeeData."
    return {
        "award_id_piid": _get(rec, "contractId.piid"),
        "modification_number": _get(rec, "contractId.modificationNumber"),
        "transaction_number": _get(rec, "contractId.transactionNumber"),
        "parent_award_id_piid": _get(rec, "contractId.referencedIDVPiid"),
        "federal_action_obligation": _get(rec, "awardDetails.dollars.actionObligation"),
        "potential_total_value_of_award": _first(
            rec, "awardDetails.totalContractDollars.totalBaseAndAllOptionsValue", "awardDetails.dollars.baseAndAllOptionsValue"),
        "current_total_value_of_award": _first(
            rec, "awardDetails.totalContractDollars.totalBaseAndExercisedOptionsValue", "awardDetails.dollars.baseAndExercisedOptionsValue"),
        "base_and_all_options_value": _get(rec, "awardDetails.dollars.baseAndAllOptionsValue"),
        "action_date": _date_part(_get(rec, "awardDetails.dates.dateSigned")),
        "period_of_performance_start_date": _date_part(_get(rec, "awardDetails.dates.periodOfPerformanceStartDate")),
        "awarding_agency_name": c_dept[1], "awarding_sub_agency_name": c_sub[1],
        "awarding_office_code": c_off[0], "awarding_office_name": c_off[1],
        "funding_agency_name": f_dept[1], "funding_sub_agency_name": f_sub[1],
        "funding_office_code": f_off[0], "funding_office_name": f_off[1],
        "recipient_uei": _get(rec, awardee + "awardeeUEIInformation.uniqueEntityId"),
        "recipient_name": _first(rec, awardee + "awardeeHeader.awardeeName", awardee + "awardeeHeader.legalBusinessName"),
        "recipient_parent_name": _get(rec, awardee + "awardeeUEIInformation.awardeeUltimateParentName"),
        "recipient_state_code": _get(rec, awardee + "awardeeLocation.state.code"),
        "recipient_city_name": _get(rec, awardee + "awardeeLocation.city"),
        "recipient_phone_number": _get(rec, awardee + "awardeeLocation.phoneNumber"),
        "cage_code": _get(rec, awardee + "awardeeUEIInformation.cageCode"),
        "naics_code": naics.get("code", "") if isinstance(naics, dict) else "",
        "naics_description": naics.get("name", "") if isinstance(naics, dict) else "",
        "product_or_service_code": _get(rec, "coreData.productOrServiceInformation.productOrService.code"),
        "product_or_service_code_description": _get(rec, "coreData.productOrServiceInformation.productOrService.name"),
        "award_type": str(_get(rec, "coreData.awardOrIDVType.name")).upper(),
        "type_of_contract_pricing_code": _get(rec, "coreData.acquisitionData.typeOfContractPricing.code"),
        "transaction_description": _get(rec, "awardDetails.productOrServiceInformation.descriptionOfContractRequirement"),
        "prime_award_base_transaction_description": "",  # not provided by this API
        "solicitation_identifier": _get(rec, "coreData.solicitationId"),
    }


# ---------------------------------------------------------------- http --
class FetchError(Exception):
    """Raised with a message that is safe to log (never contains the key)."""


def http_get(url: str, timeout: int = 60):
    """-> (status, parsed json or None). The URL carries the API key, so
    nothing from it may reach an error message."""
    req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "qxc-refresh"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else None)
    except urllib.error.HTTPError as e:
        return e.code, None
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise FetchError(f"network error ({type(e).__name__})") from None


def build_url(api_key: str, since: datetime.date, offset: int, limit: int = PAGE_SIZE, query: str = None) -> str:
    params = {
        "api_key": api_key,
        "awardOrIDV": "Award",
        "q": query if query is not None else SEARCH_TEXT,
        "dateSigned": f"[{since.strftime('%m/%d/%Y')},]",
        "limit": str(limit),
        "offset": str(offset),
        "includeSections": "contractId,coreData,awardDetails,awardeeData",
    }
    return API_URL + "?" + urllib.parse.urlencode(params, safe="[],/")


def fetch_pages(api_key, since, transport=http_get, max_calls=40, sleep=1.0, log=print, limit=PAGE_SIZE):
    """Yield raw award records, page by page, stopping at the end of the data
    or at max_calls (a quota guard)."""
    offset, calls = 0, 0
    while calls < max_calls:
        url = build_url(api_key, since, offset, limit)
        status, data = None, None
        for attempt in range(4):
            status, data = transport(url)
            calls += 1
            if status in (429, 500, 502, 503, 504) and attempt < 3:
                log(json.dumps({"event": "retry", "status": status, "offset": offset, "attempt": attempt + 1}))
                time.sleep(sleep * (2 ** attempt))
                continue
            break
        if status == 204:
            return
        if status == 403:
            raise FetchError("SAM.gov rejected the API key (403): it is missing, invalid or disabled.")
        if status == 400:
            raise FetchError("SAM.gov rejected the request (400): a parameter or its format is wrong.")
        if status != 200 or not isinstance(data, dict):
            raise FetchError(f"unexpected response from SAM.gov (status {status}).")
        batch = data.get("awardSummary") or []
        log(json.dumps({"event": "page", "offset": offset, "records": len(batch), "total": data.get("totalRecords")}))
        yield from batch
        total = data.get("totalRecords")
        offset += 1
        if len(batch) < limit or (isinstance(total, int) and offset * limit >= total):
            return
        time.sleep(sleep)
    raise FetchError(f"stopped after {max_calls} calls with more data remaining; raise --max-calls or run again later.")


# ----------------------------------------------------------- orchestrate --
def data_through(existing_csv: Path) -> datetime.date:
    """Latest action date in the current source file."""
    latest = None
    with open(existing_csv, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            raw = (row.get("action_date") or "").strip()
            for fmt in ("%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d"):
                try:
                    d = datetime.datetime.strptime(raw, fmt).date()
                    latest = d if latest is None or d > latest else latest
                    break
                except ValueError:
                    continue
    if latest is None:
        raise FetchError(f"no readable action_date values in {existing_csv}")
    return latest


def check_mapped(rows: list):
    """Stop if the API's shape looks to have changed (critical fields empty)."""
    if not rows:
        return
    for col in CRITICAL:
        missing = sum(1 for r in rows if str(r.get(col, "")).strip() == "")
        if missing / len(rows) > CRITICAL_MISSING_LIMIT:
            raise FetchError(
                f"{missing} of {len(rows)} fetched rows have no {col}: the API response shape may have changed. "
                "Nothing was written."
            )


def write_rows(path: Path, rows: list):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)


def fetch_new_awards(api_key, existing_csv: Path, overlap_days=45, transport=http_get, max_calls=40, sleep=1.0, log=print):
    since = data_through(existing_csv) - datetime.timedelta(days=overlap_days)
    log(json.dumps({"event": "fetch_start", "since": since.isoformat(), "query": SEARCH_TEXT}))
    rows = [map_record(r) for r in fetch_pages(api_key, since, transport, max_calls, sleep, log)]
    check_mapped(rows)
    return since, rows


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--existing", type=Path, help="current prime transactions CSV (sets the data-through date)")
    ap.add_argument("--out", type=Path, help="folder to write the new-records CSV into")
    ap.add_argument("--overlap-days", type=int, default=45)
    ap.add_argument("--max-calls", type=int, default=40, help="API calls allowed this run (quota guard)")
    ap.add_argument("--probe", action="store_true", help="fetch ONE record and report which fields mapped; writes nothing")
    if any(a.lower().startswith(("--api-key", "--key", "--api_key")) for a in sys.argv[1:]):
        # refuse before argparse can echo the value back into a terminal or job log
        sys.exit("The API key cannot be passed as an argument. Set the SAM_API_KEY environment variable instead.")
    args = ap.parse_args()

    key = os.environ.get("SAM_API_KEY", "").strip()
    if not key:
        sys.exit("SAM_API_KEY is not set. It must come from the environment (Secret Manager on the job), never a file or argument.")
    try:
        if args.probe:
            since = datetime.date.today() - datetime.timedelta(days=120)
            status, data = http_get(build_url(key, since, 0, limit=1))
            if status != 200 or not data or not data.get("awardSummary"):
                sys.exit(f"Probe got status {status} and no record. (403 = bad key, 400 = bad parameter, 204 = nothing found.)")
            row = map_record(data["awardSummary"][0])
            print(f"totalRecords reported: {data.get('totalRecords')}")
            for col in COLUMNS:
                print(f"  {'ok   ' if str(row[col]).strip() else 'EMPTY'} {col}")
            return
        if not args.existing or not args.out:
            sys.exit("--existing and --out are required (or use --probe).")
        since, rows = fetch_new_awards(key, args.existing, args.overlap_days, max_calls=args.max_calls)
    except FetchError as e:
        sys.exit(f"fetch failed: {e}")
    args.out.mkdir(parents=True, exist_ok=True)
    path = args.out / f"sam_awards_{datetime.date.today():%Y%m%d}.csv"
    write_rows(path, rows)
    print(f"Wrote {len(rows)} rows (signed on or after {since}) to {path}")


if __name__ == "__main__":
    main()
