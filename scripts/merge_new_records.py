"""
Merge a new export of award records into the existing source CSV without ever
creating repeats. Used by the data-refresh process (see docs/data-refresh.md).

    python scripts/merge_new_records.py --incoming new.csv --existing data/raw/prime_transactions_checkpoint_1.csv
    python scripts/merge_new_records.py --incoming new.xlsx --existing ... --out merged.csv    # writes a file

Dry run unless --out is given. The kind of file (prime transactions vs.
subawards) is detected from its header.

Guarantees
  * Existing rows are never changed, dropped or reordered. New rows are only
    appended at the end. This matters: subaward ids in the app are assigned
    by row position, so reordering would orphan notes and links.
  * A row counts as already present if its identity matches an existing row:
      prime transactions: award id, parent award id, modification and
        transaction number, recipient UEI, obligated amount, action date
      subawards: prime award id, subaward number, subawardee UEI, amount,
        action date
    Dates and amounts are compared after normalizing, so 9/19/24, 2024-09-19
    and an Excel date serial all match.
  * Same transaction but a different amount/date (a revised record) is NOT
    appended and not silently dropped: it is listed under "revisions".
  * Files missing columns the database build needs are refused, because the
    new rows would load with blank offices and companies (--allow-missing
    overrides).
"""
import argparse
import csv
import datetime
import re
import sys
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

# Columns db/build_db.py reads from each file; a new file must carry them.
REQUIRED = {
    "prime": [
        "award_id_piid", "parent_award_id_piid", "federal_action_obligation", "potential_total_value_of_award",
        "current_total_value_of_award", "base_and_all_options_value", "action_date",
        "period_of_performance_start_date", "awarding_agency_name", "awarding_sub_agency_name",
        "awarding_office_code", "awarding_office_name", "funding_agency_name", "funding_sub_agency_name",
        "funding_office_code", "funding_office_name", "recipient_uei", "recipient_name", "recipient_parent_name",
        "recipient_state_code", "recipient_city_name", "recipient_phone_number", "cage_code", "naics_code",
        "naics_description", "product_or_service_code", "product_or_service_code_description", "award_type",
        "type_of_contract_pricing_code", "transaction_description", "prime_award_base_transaction_description",
    ],
    "subawards": [
        "prime_award_piid", "prime_award_amount", "prime_award_awarding_agency_name",
        "prime_award_awarding_sub_agency_name", "prime_award_awarding_office_code",
        "prime_award_awarding_office_name", "prime_award_funding_agency_name",
        "prime_award_funding_sub_agency_name", "prime_award_funding_office_code",
        "prime_award_funding_office_name", "prime_award_period_of_performance_start_date", "prime_awardee_uei",
        "prime_awardee_name", "prime_award_base_transaction_description", "subaward_type", "subaward_number",
        "subaward_amount", "subaward_action_date", "subawardee_uei", "subawardee_name", "subawardee_state_code",
        "subawardee_city_name", "subaward_description",
    ],
}


# ------------------------------------------------------------- reading --
def read_csv(path: Path):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return list(reader.fieldnames or []), list(reader)


def read_xlsx(path: Path):
    """First sheet of an .xlsx, using only the standard library."""
    ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(path)
    strings = []
    if "xl/sharedStrings.xml" in z.namelist():
        for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall(ns + "si"):
            strings.append("".join(t.text or "" for t in si.iter(ns + "t")))
    sheet = sorted(n for n in z.namelist() if n.startswith("xl/worksheets/sheet"))[0]

    def col_index(ref):
        n = 0
        for ch in re.match(r"[A-Z]+", ref).group():
            n = n * 26 + ord(ch) - 64
        return n - 1

    table = []
    for row in ET.fromstring(z.read(sheet)).iter(ns + "row"):
        cells = {}
        for c in row.findall(ns + "c"):
            v = c.find(ns + "v")
            if v is None:
                continue
            cells[col_index(c.get("r"))] = strings[int(v.text)] if c.get("t") == "s" else v.text
        table.append(cells)
    header = [table[0].get(i, "") for i in range(max(table[0]) + 1)]
    rows = [{header[i]: r.get(i, "") for i in range(len(header))} for r in table[1:]]
    return header, rows


def read_any(path: Path):
    return read_xlsx(path) if path.suffix.lower() == ".xlsx" else read_csv(path)


def detect_kind(header) -> str:
    cols = set(header)
    if {"award_id_piid", "transaction_number"} <= cols:
        return "prime"
    if "subaward_number" in cols:
        return "subawards"
    sys.exit("Cannot tell what kind of file this is: no award_id_piid/transaction_number or subaward_number column.")


# ------------------------------------------------------- normalization --
def norm_date(value) -> str:
    """Any common date spelling (or an Excel serial) -> YYYY-MM-DD, '' if none."""
    v = (value or "").strip()
    if not v:
        return ""
    if re.fullmatch(r"\d+(\.\d+)?", v) and 20000 < float(v) < 80000:
        return (datetime.date(1899, 12, 30) + datetime.timedelta(days=int(float(v)))).isoformat()
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S"):
        try:
            return datetime.datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            pass
    return v


def norm_amount(value) -> str:
    try:
        return f"{float(str(value).replace(',', '').replace('$', '').strip()):.2f}"
    except ValueError:
        return (value or "").strip()


def clean(value) -> str:
    v = (value or "").strip()
    return "" if v.lower() in ("nan", "none", "null") else v


def identity(kind, r) -> tuple:
    if kind == "prime":
        return (clean(r.get("award_id_piid")), clean(r.get("parent_award_id_piid")),
                clean(r.get("modification_number")) or "0", clean(r.get("transaction_number")) or "0",
                clean(r.get("recipient_uei")).upper(), norm_amount(r.get("federal_action_obligation")),
                norm_date(r.get("action_date")))
    return (clean(r.get("prime_award_piid")), clean(r.get("subaward_number")), clean(r.get("subawardee_uei")).upper(),
            norm_amount(r.get("subaward_amount")), norm_date(r.get("subaward_action_date")))


def transaction_key(kind, r) -> tuple:
    """The part of identity that should never change between revisions."""
    ident = identity(kind, r)
    return ident[:4] if kind == "prime" else ident[:3]


def to_existing_style(column, value) -> str:
    """Write new dates the way the existing file does (M/D/YY)."""
    if column.endswith("date") or column in ("action_date", "solicitation_date"):
        iso = norm_date(value)
        if re.fullmatch(r"\d{4}-\d\d-\d\d", iso):
            d = datetime.date.fromisoformat(iso)
            return f"{d.month}/{d.day}/{d.year % 100:02d}"
    return clean(value) if value is not None else ""


# --------------------------------------------------------------- merge --
def write_csv(path: Path, header, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def merge(kind, existing_header, existing_rows, incoming_header, incoming_rows):
    have = {identity(kind, r) for r in existing_rows}
    txn_amounts = {}
    for r in existing_rows:
        txn_amounts.setdefault(transaction_key(kind, r), set()).add(identity(kind, r))
    new_rows, dupes, revisions, seen = [], 0, [], set()
    for r in incoming_rows:
        ident = identity(kind, r)
        if ident in have or ident in seen:
            dupes += 1
            continue
        tk = transaction_key(kind, r)
        if tk in txn_amounts:
            # same transaction already on file with a different amount/date/company
            revisions.append((tk, sorted(txn_amounts[tk])[0][-2:], ident[-2:]))
            continue
        seen.add(ident)
        new_rows.append({c: to_existing_style(c, r.get(c, "")) for c in existing_header})
    return new_rows, dupes, revisions


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--incoming", required=True, type=Path, help="new export (.csv or .xlsx)")
    ap.add_argument("--existing", required=True, type=Path, help="current source CSV")
    ap.add_argument("--out", type=Path, help="write the merged CSV here (default: dry run)")
    ap.add_argument("--allow-missing", action="store_true", help="accept a file lacking columns the build needs")
    args = ap.parse_args()

    inc_header, inc_rows = read_any(args.incoming)
    ex_header, ex_rows = read_csv(args.existing)
    kind = detect_kind(inc_header)
    if kind != detect_kind(ex_header):
        sys.exit(f"Kind mismatch: incoming looks like {kind}, existing looks like {detect_kind(ex_header)}.")

    missing = [c for c in REQUIRED[kind] if c not in inc_header]
    print(f"incoming: {args.incoming.name}  kind={kind}  rows={len(inc_rows)}")
    if missing:
        print(f"  missing {len(missing)} column(s) the build needs: {', '.join(missing[:8])}{' ...' if len(missing) > 8 else ''}")
        if not args.allow_missing:
            sys.exit("Refusing to merge: new rows would load with blank values. Use a fuller export, or --allow-missing.")

    new_rows, dupes, revisions = merge(kind, ex_header, ex_rows, inc_header, inc_rows)
    print(f"  already present (skipped): {dupes}")
    print(f"  revised records, not appended: {len(revisions)}")
    for tk, old, new in revisions[:10]:
        print(f"     {tk}: on file (amount, date)={old} vs incoming={new}")
    print(f"  NEW rows to append: {len(new_rows)}")

    if not args.out:
        print("Dry run. Nothing written. Pass --out to write the merged file.")
        return
    if not new_rows:
        print("No new rows, so no output file was written.")
        return
    write_csv(args.out, ex_header, ex_rows + new_rows)
    print(f"Wrote {args.out}: {len(ex_rows)} existing + {len(new_rows)} new = {len(ex_rows) + len(new_rows)} rows.")


if __name__ == "__main__":
    main()
