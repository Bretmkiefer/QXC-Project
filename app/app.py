"""
QXC Checkpoint 1 proof-of-concept UI.

Local-only Flask app reading db/qxc.db (built by db/build_db.py). Data is
loaded into memory at startup with pandas and re-aggregated per request -
the dataset is small (a few thousand rows) so this stays instant and keeps
the route code simple/readable instead of hand-writing SQL.

Layout is master-detail: each section (offices, awardees) renders a
persistent record list alongside the detail panel for whatever is selected,
so browsing feels like one continuous screen rather than list -> detail page
jumps.
"""
import base64
import json
import os
import re
import sqlite3
from pathlib import Path

import pandas as pd
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "db" / "qxc.db"

app = Flask(__name__)

ROLES = {
    "awarding": {
        "label": "Awarding Office",
        "code": "awarding_office_code",
        "name": "awarding_office_name",
        "agency": "awarding_agency_name",
        "sub_agency": "awarding_sub_agency_name",
    },
    "funding": {
        "label": "Funding Office",
        "code": "funding_office_code",
        "name": "funding_office_name",
        "agency": "funding_agency_name",
        "sub_agency": "funding_sub_agency_name",
    },
}

AVATAR_PALETTE = [
    ("#e0e7ff", "#3730a3"),
    ("#dbeafe", "#1e40af"),
    ("#cffafe", "#0e7490"),
    ("#d1fae5", "#047857"),
    ("#fef3c7", "#b45309"),
    ("#fee2e2", "#b91c1c"),
    ("#fce7f3", "#be185d"),
    ("#ede9fe", "#6d28d9"),
]


def avatar(name: str) -> dict:
    name = (name or "?").strip()
    words = [w for w in name.replace("-", " ").split() if w]
    initials = "".join(w[0] for w in words[:2]).upper() or "?"
    idx = sum(ord(c) for c in name) % len(AVATAR_PALETTE) if name else 0
    bg, fg = AVATAR_PALETTE[idx]
    return {"initials": initials, "bg": bg, "fg": fg}


def coverage(direct_total: float, sub_total: float) -> dict:
    direct_total = direct_total or 0
    sub_total = sub_total or 0
    if direct_total > 0 and sub_total > 0:
        return {"label": "Direct + Sub", "cls": "green"}
    if direct_total > 0:
        return {"label": "Direct Only", "cls": "accent"}
    if sub_total > 0:
        return {"label": "Subaward Only", "cls": "amber"}
    return {"label": "No Cisco Spend", "cls": "muted"}


def visibility_tier(direct_total: float, sub_total: float) -> str:
    """How much of this record's Cisco footprint comes through subaward
    data (the harder-to-get, more valuable dataset) vs. direct awards."""
    direct_total = direct_total or 0
    sub_total = sub_total or 0
    total = direct_total + sub_total
    if total <= 0:
        return "low"
    pct = sub_total / total
    if pct >= 0.4:
        return "high"
    if pct >= 0.1:
        return "medium"
    return "low"


def concentration_tier(top_share) -> str:
    if top_share is None:
        return "diversified"
    return "concentrated" if top_share >= 0.5 else "diversified"


def entity_key(uei: str, name: str) -> str:
    uei = (uei or "").strip()
    if uei:
        return f"uei:{uei}"
    return f"name:{(name or '').strip().upper()}"


def encode_key(key: str) -> str:
    return base64.urlsafe_b64encode(key.encode()).decode().rstrip("=")


def decode_key(token: str) -> str:
    padded = token + "=" * (-len(token) % 4)
    return base64.urlsafe_b64decode(padded.encode()).decode()


def load_data():
    with sqlite3.connect(DB_PATH) as conn:
        awards = pd.read_sql("SELECT * FROM awards", conn)
        subawards = pd.read_sql("SELECT * FROM subawards", conn)

    for col in ["recipient_uei", "recipient_name"]:
        awards[col] = awards[col].fillna("")
    for col in ["subawardee_uei", "subawardee_name", "prime_awardee_name", "matched_award_id_piid"]:
        subawards[col] = subawards[col].fillna("")

    awards["entity_key"] = [
        entity_key(u, n) for u, n in zip(awards["recipient_uei"], awards["recipient_name"])
    ]
    subawards["entity_key"] = [
        entity_key(u, n) for u, n in zip(subawards["subawardee_uei"], subawards["subawardee_name"])
    ]
    return awards, subawards


AWARDS, SUBAWARDS = load_data()


def strip_abbreviation(name):
    """The subawards source file appends a trailing "(ABBR)" to some
    agency/sub-agency names that the prime transactions file doesn't (e.g.
    "Department of Defense (DOD)" vs "Department of Defense") - normalize
    both to the same string so the same agency doesn't fragment into two
    rows anywhere it's grouped."""
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return name
    return re.sub(r"\s*\([A-Z]{2,8}\)$", "", str(name)).strip()


_ORG_COLS = ["awarding_agency_name", "funding_agency_name", "awarding_sub_agency_name", "funding_sub_agency_name"]

for _df in (AWARDS, SUBAWARDS):
    for _col in _ORG_COLS:
        _df[_col] = _df[_col].apply(strip_abbreviation)

# The subawards file also frequently spells the same office/agency in
# ALL CAPS where the prime transactions file uses title case (e.g.
# "NATIONAL AERONAUTICS AND SPACE ADMINISTRATION" vs "National Aeronautics
# and Space Administration"). Build a case-insensitive lookup from the
# (cleaner) awards file and remap subawards onto it so the two datasets
# merge into one row per office/agency instead of splitting on casing.
_CANONICAL_CASE = {}
for _col in _ORG_COLS:
    for _v in AWARDS[_col].dropna().unique():
        _CANONICAL_CASE.setdefault(_v.lower(), _v)


def normalize_org_spelling(name: str) -> str:
    """The subawards file also spells some sub-agencies differently, not
    just in different case: abbreviated ("DEPT OF THE ARMY" vs "Department
    of the Army") and reversed ("EDUCATION, DEPARTMENT OF" vs "Department
    of Education"). Normalize both patterns before the case-insensitive
    canonical lookup so these collapse onto the awards file's spelling too."""
    s = name.strip()
    m = re.match(r"^(.+),\s*DEPARTMENT OF$", s, flags=re.IGNORECASE)
    if m:
        s = f"DEPARTMENT OF {m.group(1)}"
    s = re.sub(r"\bDEPT\b", "DEPARTMENT", s, flags=re.IGNORECASE)
    return s


def canonicalize_case(name):
    if name is None or (isinstance(name, float) and pd.isna(name)):
        return name
    name = str(name)
    normalized = normalize_org_spelling(name)
    return _CANONICAL_CASE.get(normalized.lower(), _CANONICAL_CASE.get(name.lower(), normalized))


for _col in _ORG_COLS:
    SUBAWARDS[_col] = SUBAWARDS[_col].apply(canonicalize_case)


def format_phone(raw) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    digits = re.sub(r"\D", "", str(raw))
    if len(digits) == 10:
        return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"
    if len(digits) == 11 and digits[0] == "1":
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:11]}"
    return str(raw).strip()


# ---------------------------------------------------- read/write storage --
# The award/subaward data above is bundled read-only into the deploy (built
# by db/build_db.py) and always read via sqlite3, everywhere. But viewed
# status and notes are written at runtime by users, and Cloud Run gives an
# instance no durable local disk between requests - so on Cloud Run those two
# go to Firestore instead. Locally (no K_SERVICE env var, which only Cloud
# Run sets) they keep using db/qxc.db exactly as before, so `python
# app/app.py` needs no GCP credentials and behaves unchanged.
USE_FIRESTORE = bool(os.environ.get("K_SERVICE"))

if USE_FIRESTORE:
    from google.cloud import firestore

    _fs_client = firestore.Client()


def _fs_timestamp(value) -> str:
    if value is None:
        return ""
    return value.strftime("%Y-%m-%d %H:%M:%S")


def ensure_viewed_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS viewed_records (
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                viewed_at TEXT NOT NULL,
                PRIMARY KEY (record_type, record_id)
            )
            """
        )


def get_viewed_set() -> set:
    if USE_FIRESTORE:
        docs = _fs_client.collection("viewed_records").stream()
        return {(d.get("record_type"), d.get("record_id")) for d in (doc.to_dict() for doc in docs)}
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT record_type, record_id FROM viewed_records")
        return set(cur.fetchall())


def set_viewed(record_type: str, record_id: str, viewed: bool):
    if USE_FIRESTORE:
        ref = _fs_client.collection("viewed_records").document(f"{record_type}_{record_id}")
        if viewed:
            ref.set({"record_type": record_type, "record_id": record_id, "viewed_at": firestore.SERVER_TIMESTAMP})
        else:
            ref.delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        if viewed:
            conn.execute(
                "INSERT OR IGNORE INTO viewed_records (record_type, record_id, viewed_at) "
                "VALUES (?, ?, datetime('now'))",
                (record_type, record_id),
            )
        else:
            conn.execute(
                "DELETE FROM viewed_records WHERE record_type = ? AND record_id = ?",
                (record_type, record_id),
            )
        conn.commit()


ensure_viewed_table()


def ensure_notes_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS notes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                note_text TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_notes_record ON notes(record_type, record_id)")


def get_notes(record_type: str, record_id: str) -> list:
    if USE_FIRESTORE:
        # Two equality filters with no order_by needs no composite index;
        # sort client-side instead of adding .order_by() in the query.
        docs = (
            _fs_client.collection("notes")
            .where("record_type", "==", record_type)
            .where("record_id", "==", record_id)
            .stream()
        )
        notes = [
            {"id": doc.id, "note_text": d.get("note_text", ""), "created_at": _fs_timestamp(d.get("created_at"))}
            for doc, d in ((doc, doc.to_dict()) for doc in docs)
        ]
        notes.sort(key=lambda n: n["created_at"], reverse=True)
        return notes
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT id, note_text, created_at FROM notes WHERE record_type = ? AND record_id = ? ORDER BY id DESC",
            (record_type, record_id),
        )
        return [dict(row) for row in cur.fetchall()]


def add_note(record_type: str, record_id: str, text: str):
    text = (text or "").strip()
    if not text:
        return
    if USE_FIRESTORE:
        _fs_client.collection("notes").add(
            {
                "record_type": record_type,
                "record_id": record_id,
                "note_text": text,
                "created_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO notes (record_type, record_id, note_text, created_at) VALUES (?, ?, ?, datetime('now'))",
            (record_type, record_id, text),
        )
        conn.commit()


def delete_note(note_id):
    if USE_FIRESTORE:
        _fs_client.collection("notes").document(str(note_id)).delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM notes WHERE id = ?", (int(note_id),))
        conn.commit()


ensure_notes_table()


def build_records() -> pd.DataFrame:
    """One row per individual contract/subaward record, normalized to a
    shared column set so the agency rollup and the parametric search can
    treat direct awards and subawards uniformly."""
    common = [
        "record_type", "record_id", "display_id", "company_name", "company_key", "prime_name",
        "description", "amount", "action_date", "service_start", "contact_phone",
        "awarding_office_name", "awarding_office_code", "awarding_agency_name", "awarding_sub_agency_name",
        "funding_office_name", "funding_office_code", "funding_agency_name", "funding_sub_agency_name",
    ]

    a = AWARDS.copy()
    a["record_type"] = "award"
    a["record_id"] = a["award_id_piid"]
    a["display_id"] = a["award_id_piid"]
    a["company_name"] = a["recipient_name"]
    a["company_key"] = a["entity_key"]
    a["prime_name"] = ""
    a["description"] = a["transaction_description"].fillna("")
    a["description"] = a["description"].where(a["description"] != "", a["base_transaction_description"].fillna(""))
    a["amount"] = a["total_federal_action_obligation"]
    a["action_date"] = a["latest_action_date"]
    a["service_start"] = a["period_of_performance_start_date"]
    a["contact_phone"] = a["recipient_phone_number"].apply(format_phone)

    s = SUBAWARDS.copy()
    s["record_type"] = "subaward"
    s["record_id"] = s["subaward_id"]
    s["display_id"] = s["subaward_number"].fillna("").astype(str)
    s["display_id"] = s["display_id"].where(s["display_id"] != "", s["subaward_id"])
    s["company_name"] = s["subawardee_name"]
    s["company_key"] = s["entity_key"]
    s["prime_name"] = s["prime_awardee_name"]
    s["description"] = s["subaward_description"].fillna("")
    s["description"] = s["description"].where(s["description"] != "", s["prime_award_description"].fillna(""))
    s["amount"] = s["subaward_amount"]
    s["action_date"] = s["subaward_action_date"]
    s["service_start"] = s["period_of_performance_start_date"]
    s["contact_phone"] = ""  # not present in the subawards source file

    records = pd.concat([a[common], s[common]], ignore_index=True)
    records["office_url_awarding"] = records["awarding_office_code"].astype(str).apply(encode_key)
    records["office_url_funding"] = records["funding_office_code"].astype(str).apply(encode_key)
    records["company_url"] = records["company_key"].apply(encode_key)
    return records


RECORDS = build_records()


def money(v) -> str:
    if v is None or pd.isna(v):
        return "$0"
    return f"${v:,.0f}"


def agency_display(name) -> str:
    """Display-only relabel: DoD's 2025 secondary title change to
    Department of War. The underlying data keeps the source records'
    original agency name (Department of Defense) so it still traces back
    cleanly to the original procurement filings; this only affects what's
    rendered on screen."""
    if not name or (isinstance(name, float) and pd.isna(name)):
        return name
    name = str(name)
    name = name.replace("Department of Defense (DOD)", "Department of War (DOW)")
    name = name.replace("Department of Defense", "Department of War")
    return name


def record_url(record_type: str, record_id) -> str:
    return url_for("record_detail_view", record_type=record_type, token=encode_key(str(record_id)))


def _compute_asset_version() -> str:
    """Content hash of the static JS/CSS, appended to their URLs as a cache
    buster. Firebase Hosting/CDN and browsers otherwise keep serving a
    stale cached app.js/style.css across deploys since the filename never
    changes - this forces a fresh fetch whenever the content actually does."""
    import hashlib

    h = hashlib.md5()
    for name in ("app.js", "style.css"):
        path = ROOT / "app" / "static" / name
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()[:10]


ASSET_VERSION = _compute_asset_version()

app.jinja_env.filters["money"] = money
app.jinja_env.filters["agency"] = agency_display
app.jinja_env.filters["phone"] = format_phone
app.jinja_env.globals["avatar"] = avatar
app.jinja_env.globals["record_url"] = record_url
app.jinja_env.globals["asset_version"] = ASSET_VERSION


# ---------------------------------------------------------------- agencies --

AGENCY_SORTS = {
    "combined": ("Combined Total", lambda r: r["combined_total"], True),
    "direct": ("Direct Total", lambda r: r["direct_total"], True),
    "subaward": ("Subaward Total", lambda r: r["subaward_total"], True),
    "unreviewed": ("Unreviewed Records", lambda r: r["unviewed_count"], True),
    "records": ("Record Count", lambda r: r["record_count"], True),
    "name": ("Name (A-Z)", lambda r: (r["agency"] or "").lower(), False),
}


def build_agency_rows(role: str, sort: str = "combined"):
    agency_c = f"{role}_agency_name"
    viewed = get_viewed_set()

    rows = []
    for agency_name, g in RECORDS.groupby(agency_c, dropna=False):
        direct_g = g[g["record_type"] == "award"]
        sub_g = g[g["record_type"] == "subaward"]
        direct_total = direct_g["amount"].sum()
        sub_total = sub_g["amount"].sum()
        record_count = len(g)
        viewed_count = sum(1 for rt, rid in zip(g["record_type"], g["record_id"]) if (rt, rid) in viewed)
        rows.append(
            {
                "agency": agency_name,
                "url_agency": encode_key(str(agency_name)),
                "direct_total": direct_total,
                "subaward_total": sub_total,
                "combined_total": direct_total + sub_total,
                "record_count": record_count,
                "viewed_count": viewed_count,
                "unviewed_count": record_count - viewed_count,
                "pct_viewed": round(100 * viewed_count / record_count) if record_count else 0,
                "coverage": coverage(direct_total, sub_total),
            }
        )

    _, keyfunc, reverse = AGENCY_SORTS.get(sort, AGENCY_SORTS["combined"])
    rows.sort(key=keyfunc, reverse=reverse)

    total_records = sum(r["record_count"] for r in rows)
    total_viewed = sum(r["viewed_count"] for r in rows)
    return rows, {
        "agencies": len(rows),
        "records": total_records,
        "viewed": total_viewed,
        "unviewed": total_records - total_viewed,
        "direct_total": sum(r["direct_total"] for r in rows),
        "subaward_total": sum(r["subaward_total"] for r in rows),
    }


def filter_records(role: str, agency: str = None, department: str = None, company_q: str = None, q: str = None):
    agency_c = f"{role}_agency_name"
    sub_agency_c = f"{role}_sub_agency_name"
    office_c = f"{role}_office_name"

    df = RECORDS
    if agency:
        df = df[df[agency_c] == agency]
    if department:
        df = df[df[sub_agency_c] == department]
    if company_q:
        ql = company_q.strip().lower()
        df = df[
            df["company_name"].str.lower().str.contains(ql, na=False)
            | df["prime_name"].fillna("").str.lower().str.contains(ql, na=False)
        ]
    if q:
        ql = q.strip().lower()
        mask = (
            df["description"].fillna("").str.lower().str.contains(ql, na=False)
            | df["company_name"].fillna("").str.lower().str.contains(ql, na=False)
            | df["prime_name"].fillna("").str.lower().str.contains(ql, na=False)
            | df[office_c].fillna("").str.lower().str.contains(ql, na=False)
            | df[agency_c].fillna("").str.lower().str.contains(ql, na=False)
        )
        df = df[mask]
    return df.sort_values("amount", ascending=False)


def department_options(role: str, agency: str = None):
    sub_agency_c = f"{role}_sub_agency_name"
    agency_c = f"{role}_agency_name"
    df = RECORDS
    if agency:
        df = df[df[agency_c] == agency]
    vals = sorted(v for v in df[sub_agency_c].dropna().unique() if v)
    return vals


def department_map(role: str) -> dict:
    """agency name -> sorted list of its departments, for the client-side
    cascading filter on pages (like the home page) that don't otherwise
    have per-department rows in the DOM to derive this from."""
    agency_c = f"{role}_agency_name"
    sub_agency_c = f"{role}_sub_agency_name"
    df = RECORDS[[agency_c, sub_agency_c]].dropna().drop_duplicates()
    out = {}
    for agency, group in df.groupby(agency_c):
        depts = sorted(v for v in group[sub_agency_c].unique() if v)
        if depts:
            out[agency] = depts
    return out


def build_department_detail(role: str, agency: str, department: str):
    agency_c = f"{role}_agency_name"
    sub_agency_c = f"{role}_sub_agency_name"
    office_code_c = f"{role}_office_code"
    office_name_c = f"{role}_office_name"

    df = RECORDS[(RECORDS[agency_c] == agency) & (RECORDS[sub_agency_c] == department)]
    if df.empty:
        return None

    direct_total = df[df["record_type"] == "award"]["amount"].sum()
    sub_total = df[df["record_type"] == "subaward"]["amount"].sum()
    record_count = len(df)

    viewed = get_viewed_set()
    viewed_count = sum(1 for rt, rid in zip(df["record_type"], df["record_id"]) if (rt, rid) in viewed)

    offices = []
    for code, g in df.groupby(office_code_c, dropna=False):
        o_direct = g[g["record_type"] == "award"]["amount"].sum()
        o_sub = g[g["record_type"] == "subaward"]["amount"].sum()
        offices.append(
            {
                "code": code,
                "name": g[office_name_c].iloc[0],
                "url_code": encode_key(str(code)),
                "direct_total": o_direct,
                "subaward_total": o_sub,
                "combined_total": o_direct + o_sub,
                "record_count": len(g),
                "coverage": coverage(o_direct, o_sub),
            }
        )
    offices.sort(key=lambda o: o["combined_total"], reverse=True)

    return {
        "role": role,
        "agency": agency,
        "department": department,
        "direct_total": direct_total,
        "subaward_total": sub_total,
        "record_count": record_count,
        "viewed_count": viewed_count,
        "unviewed_count": record_count - viewed_count,
        "pct_viewed": round(100 * viewed_count / record_count) if record_count else 0,
        "coverage": coverage(direct_total, sub_total),
        "offices": offices,
    }


def department_url(role: str, agency, department) -> str:
    return url_for(
        "department_detail_view",
        role=role,
        agency_token=encode_key(str(agency)),
        dept_token=encode_key(str(department)),
    )


app.jinja_env.globals["department_url"] = department_url


# --------------------------------------------------------- record detail --

def _field(label, value):
    return {"label": label, "value": value if (value not in (None, "", "nan")) else "—"}


def build_record_detail(record_type: str, record_id: str):
    if record_type == "award":
        matches = AWARDS[AWARDS["award_id_piid"] == record_id]
        if matches.empty:
            return None
        a = matches.iloc[0]

        groups = [
            {
                "title": "Award",
                "fields": [
                    _field("Award ID (PIID)", a["award_id_piid"]),
                    _field("Parent Award ID", a["parent_award_id_piid"]),
                    _field("Award Type", a["award_type"]),
                    _field("Contract Pricing Type", a["type_of_contract_pricing_code"]),
                    _field(
                        "NAICS",
                        f"{int(a['naics_code'])} — {a['naics_description']}" if a["naics_code"] not in (None, "") and not pd.isna(a["naics_code"]) else None,
                    ),
                    _field(
                        "Product/Service Code",
                        f"{a['product_or_service_code']} — {a['product_or_service_code_description']}"
                        if a["product_or_service_code"] not in (None, "") and not pd.isna(a["product_or_service_code"])
                        else None,
                    ),
                    _field("Transactions on Record", int(a["transaction_count"])),
                    _field("First Action Date", a["first_action_date"]),
                    _field("Latest Action Date", a["latest_action_date"]),
                    _field("Period of Performance Start", a["period_of_performance_start_date"]),
                ],
            },
            {
                "title": "Financials",
                "fields": [
                    _field("Total Obligated (this award)", money(a["total_federal_action_obligation"])),
                    _field("Potential Value", money(a["potential_total_value_of_award"])),
                    _field("Current Value", money(a["current_total_value_of_award"])),
                    _field("Base + All Options Value", money(a["base_and_all_options_value"])),
                ],
            },
            {
                "title": "Recipient",
                "fields": [
                    _field("Name", a["recipient_name"]),
                    _field("UEI", a["recipient_uei"]),
                    _field("Parent Company", a["recipient_parent_name"]),
                    _field(
                        "Location",
                        f"{a['recipient_city_name']}, {a['recipient_state_code']}"
                        if a["recipient_city_name"] not in (None, "") and not pd.isna(a["recipient_city_name"])
                        else a["recipient_state_code"],
                    ),
                    _field("Phone", format_phone(a["recipient_phone_number"])),
                ],
            },
            {
                "title": "Offices",
                "fields": [
                    _field("Awarding Office", a["awarding_office_name"]),
                    _field("Awarding Agency", agency_display(a["awarding_agency_name"])),
                    _field("Awarding Sub-Agency", a["awarding_sub_agency_name"]),
                    _field("Funding Office", a["funding_office_name"]),
                    _field("Funding Agency", agency_display(a["funding_agency_name"])),
                    _field("Funding Sub-Agency", a["funding_sub_agency_name"]),
                ],
            },
        ]

        return {
            "record_type": "award",
            "record_id": record_id,
            "type_label": "Direct Award",
            "title": a["award_id_piid"],
            "description": a["transaction_description"] or a["base_transaction_description"],
            "amount": a["total_federal_action_obligation"],
            "amount_label": "Total Obligated",
            "company_name": a["recipient_name"],
            "company_url": encode_key(a["entity_key"]),
            "office_url_awarding": encode_key(str(a["awarding_office_code"])),
            "office_url_funding": encode_key(str(a["funding_office_code"])),
            "groups": groups,
            "related": None,
        }

    if record_type == "subaward":
        matches = SUBAWARDS[SUBAWARDS["subaward_id"] == record_id]
        if matches.empty:
            return None
        s = matches.iloc[0]

        related = None
        if s["matched_award_id_piid"]:
            related = {
                "label": f"Prime award {s['matched_award_id_piid']} is in our direct-award dataset",
                "url": record_url("award", s["matched_award_id_piid"]),
            }

        groups = [
            {
                "title": "Subaward",
                "fields": [
                    _field("Subaward Number", s["subaward_number"]),
                    _field("Subaward Type", s["subaward_type"]),
                    _field("Prime Award PIID", s["prime_award_piid"]),
                    _field("Action Date", s["subaward_action_date"]),
                    _field("Period of Performance Start", s["period_of_performance_start_date"]),
                ],
            },
            {
                "title": "Financials",
                "fields": [
                    _field("Subaward Amount", money(s["subaward_amount"])),
                    _field("Prime Award Amount", money(s["prime_award_amount"])),
                ],
            },
            {
                "title": "Subawardee",
                "fields": [
                    _field("Name", s["subawardee_name"]),
                    _field("UEI", s["subawardee_uei"]),
                    _field(
                        "Location",
                        f"{s['subawardee_city_name']}, {s['subawardee_state_code']}"
                        if s["subawardee_city_name"] not in (None, "") and not pd.isna(s["subawardee_city_name"])
                        else s["subawardee_state_code"],
                    ),
                ],
            },
            {
                "title": "Prime Contractor",
                "fields": [
                    _field("Name", s["prime_awardee_name"]),
                    _field("UEI", s["prime_awardee_uei"]),
                ],
            },
            {
                "title": "Offices",
                "fields": [
                    _field("Awarding Office", s["awarding_office_name"]),
                    _field("Awarding Agency", agency_display(s["awarding_agency_name"])),
                    _field("Awarding Sub-Agency", s["awarding_sub_agency_name"]),
                    _field("Funding Office", s["funding_office_name"]),
                    _field("Funding Agency", agency_display(s["funding_agency_name"])),
                    _field("Funding Sub-Agency", s["funding_sub_agency_name"]),
                ],
            },
        ]

        return {
            "record_type": "subaward",
            "record_id": record_id,
            "type_label": "Subaward",
            "title": s["subaward_number"] or record_id,
            "description": s["subaward_description"] or s["prime_award_description"],
            "amount": s["subaward_amount"],
            "amount_label": "Subaward Amount",
            "company_name": s["subawardee_name"],
            "company_url": encode_key(s["entity_key"]),
            "office_url_awarding": encode_key(str(s["awarding_office_code"])),
            "office_url_funding": encode_key(str(s["funding_office_code"])),
            "groups": groups,
            "related": related,
        }

    return None


# ---------------------------------------------------------------- offices --

def build_offices_rows(role: str):
    cfg = ROLES[role]
    code_c, name_c, agency_c = cfg["code"], cfg["name"], cfg["agency"]

    # Grouped by office code alone - the direct-award and subaward source
    # files sometimes label the same office's agency slightly differently
    # (e.g. "Department of Defense" vs "Department of Defense (DOD)"), which
    # would otherwise fragment one office into two rows if agency/name were
    # part of the join key.
    direct = (
        AWARDS.groupby(code_c, dropna=False)
        .agg(
            name=(name_c, "first"),
            agency=(agency_c, "first"),
            direct_total=("total_federal_action_obligation", "sum"),
            award_count=("award_id_piid", "nunique"),
        )
        .reset_index()
        .rename(columns={code_c: "code"})
    )
    sub = (
        SUBAWARDS.groupby(code_c, dropna=False)
        .agg(
            name=(name_c, "first"),
            agency=(agency_c, "first"),
            subaward_total=("subaward_amount", "sum"),
            subaward_count=("subaward_number", "count"),
        )
        .reset_index()
        .rename(columns={code_c: "code"})
    )

    merged = pd.merge(direct, sub, on="code", how="outer", suffixes=("", "_sub"))
    merged["name"] = merged["name"].fillna(merged["name_sub"])
    merged["agency"] = merged["agency"].fillna(merged["agency_sub"])
    merged = merged.drop(columns=["name_sub", "agency_sub"])
    merged[["direct_total", "award_count", "subaward_total", "subaward_count"]] = merged[
        ["direct_total", "award_count", "subaward_total", "subaward_count"]
    ].fillna(0)
    merged["combined_sort"] = merged["direct_total"] + merged["subaward_total"]
    merged = merged.sort_values("combined_sort", ascending=False)

    rows = merged.to_dict("records")
    for r in rows:
        r["url_code"] = encode_key(str(r["code"]))
        r["coverage"] = coverage(r["direct_total"], r["subaward_total"])
    return rows, merged["direct_total"].sum(), merged["subaward_total"].sum()


def build_office_detail(role: str, code: str):
    cfg = ROLES[role]
    code_c, name_c, agency_c, sub_agency_c = cfg["code"], cfg["name"], cfg["agency"], cfg["sub_agency"]

    office_awards = AWARDS[AWARDS[code_c].astype(str) == code].sort_values(
        "total_federal_action_obligation", ascending=False
    )
    office_subs_all = SUBAWARDS[SUBAWARDS[code_c].astype(str) == code]

    if office_awards.empty and office_subs_all.empty:
        return None

    if not office_awards.empty:
        name = office_awards.iloc[0][name_c]
        agency = office_awards.iloc[0][agency_c]
        sub_agency = office_awards.iloc[0][sub_agency_c]
    else:
        name = office_subs_all.iloc[0][name_c]
        agency = office_subs_all.iloc[0][agency_c]
        sub_agency = office_subs_all.iloc[0][sub_agency_c]

    awardees = []
    for _, a in office_awards.iterrows():
        nested = office_subs_all[office_subs_all["matched_award_id_piid"] == a["award_id_piid"]]
        awardees.append(
            {
                "award": a.to_dict(),
                "entity_url": encode_key(a["entity_key"]),
                "subawardees": nested.to_dict("records"),
                "sub_total": nested["subaward_amount"].sum(),
            }
        )

    unmatched = office_subs_all[office_subs_all["matched_award_id_piid"] == ""]
    unmatched_primes = []
    if not unmatched.empty:
        for prime_name, grp in unmatched.groupby("prime_awardee_name"):
            unmatched_primes.append(
                {
                    "prime_name": prime_name,
                    "prime_uei": grp.iloc[0]["prime_awardee_uei"],
                    "subawardees": grp.to_dict("records"),
                    "sub_total": grp["subaward_amount"].sum(),
                }
            )
        unmatched_primes.sort(key=lambda p: p["sub_total"], reverse=True)

    direct_total = office_awards["total_federal_action_obligation"].sum()
    sub_total_traced = office_subs_all["subaward_amount"].sum()
    n_awards = len(office_awards)
    n_distinct_awardees = office_awards["entity_key"].nunique()
    n_subawards_traced = len(office_subs_all)

    top_share = None
    top_name = None
    if n_awards and direct_total > 0:
        by_entity = office_awards.groupby("entity_key")["total_federal_action_obligation"].sum()
        top_entity = by_entity.idxmax()
        top_name = office_awards[office_awards["entity_key"] == top_entity].iloc[0]["recipient_name"]
        top_share = by_entity.max() / direct_total

    strengths = []
    weaknesses = []
    if n_distinct_awardees >= 5:
        strengths.append(
            f"Diversified vendor base — {n_distinct_awardees} distinct awardees across {n_awards} award{'s' if n_awards != 1 else ''}."
        )
    elif n_distinct_awardees >= 1:
        strengths.append(
            f"{n_distinct_awardees} distinct awardee{'s' if n_distinct_awardees != 1 else ''} across {n_awards} award{'s' if n_awards != 1 else ''} on record."
        )
    if sub_total_traced > 0:
        strengths.append(
            f"{n_subawards_traced} subaward{'s' if n_subawards_traced != 1 else ''} worth {money(sub_total_traced)} traced back to this office, extending visibility beyond direct awards."
        )
    if not strengths:
        strengths.append("No award activity on record yet for this office.")

    if top_share is not None and top_share >= 0.5:
        weaknesses.append(
            f"{top_share * 100:.0f}% of direct spend is concentrated with a single awardee ({top_name})."
        )
    if sub_total_traced == 0:
        weaknesses.append(
            "No subaward activity traced to this office — spend could still be flowing through subcontracts we don't have visibility into (most small-business subawards aren't required to be reported)."
        )
    if n_awards and n_awards <= 2:
        weaknesses.append(f"Thin sample — only {n_awards} direct award{'s' if n_awards != 1 else ''} on record.")
    weaknesses.append("Contract descriptions not yet classified as hardware vs. services — planned for Checkpoint 2.")

    return {
        "code": code,
        "name": name,
        "agency": agency,
        "sub_agency": sub_agency,
        "awardees": awardees,
        "direct_total": direct_total,
        "unmatched_primes": unmatched_primes,
        "unmatched_total": unmatched["subaward_amount"].sum(),
        "total_sub_traced": sub_total_traced,
        "coverage": coverage(direct_total, sub_total_traced),
        "visibility": visibility_tier(direct_total, sub_total_traced),
        "concentration": concentration_tier(top_share),
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


@app.route("/")
def index():
    role = request.args.get("role", "awarding")
    if role not in ROLES:
        abort(404)
    sort = request.args.get("sort", "combined")
    if sort not in AGENCY_SORTS:
        sort = "combined"
    jump_agency = request.args.get("agency") or None
    jump_department = request.args.get("department") or None

    rows, totals = build_agency_rows(role, sort=sort)
    agency_names = sorted(v for v in RECORDS[f"{role}_agency_name"].dropna().unique() if v)

    return render_template(
        "landing.html",
        role=role,
        roles=ROLES,
        rows=rows,
        totals=totals,
        sort=sort,
        sort_options=AGENCY_SORTS,
        agency_names=agency_names,
        dept_names=department_options(role, jump_agency),
        dept_map_json=json.dumps(department_map(role)),
        jump_agency=jump_agency,
        jump_department=jump_department,
        active="home",
    )


@app.route("/records")
def records_view():
    role = request.args.get("role", "awarding")
    if role not in ROLES:
        abort(404)
    agency = request.args.get("agency") or None
    department = request.args.get("department") or None
    company_q = request.args.get("company") or None
    q = request.args.get("q") or None

    df = filter_records(role, agency=agency, department=department, company_q=company_q, q=q)
    viewed = get_viewed_set()

    total_matches = len(df)
    # The static-site freezer passes limit=all so every record is embedded
    # in the frozen page for the client-side filter script to work with;
    # the live app always uses the normal cap.
    LIMIT = total_matches if request.args.get("limit") == "all" else 250
    df = df.head(LIMIT)

    records = df.to_dict("records")
    for r in records:
        r["is_viewed"] = (r["record_type"], str(r["record_id"])) in viewed

    viewed_in_results = sum(1 for r in records if r["is_viewed"])

    agency_names = sorted(v for v in RECORDS[f"{role}_agency_name"].dropna().unique() if v)

    return render_template(
        "records.html",
        role=role,
        roles=ROLES,
        records=records,
        total_matches=total_matches,
        shown=len(records),
        limit=LIMIT,
        viewed_in_results=viewed_in_results,
        agency=agency,
        department=department,
        company_q=company_q,
        q=q,
        agency_names=agency_names,
        dept_names=department_options(role, agency),
        active="records",
    )


@app.route("/api/records/<record_type>/<record_id>/viewed", methods=["POST"])
def api_set_viewed(record_type, record_id):
    if record_type not in ("award", "subaward"):
        abort(404)
    payload = request.get_json(silent=True) or {}
    viewed = bool(payload.get("viewed", True))
    set_viewed(record_type, record_id, viewed)
    return jsonify({"viewed": viewed})


@app.route("/record/<record_type>/<token>")
def record_detail_view(record_type, token):
    if record_type not in ("award", "subaward"):
        abort(404)
    record_id = decode_key(token)
    detail = build_record_detail(record_type, record_id)
    if detail is None:
        abort(404)
    detail["viewed"] = (record_type, str(record_id)) in get_viewed_set()
    detail["notes"] = get_notes(record_type, record_id)
    return render_template("record_detail.html", r=detail, token=token, active=None)


NOTE_TYPES = ("award", "subaward", "office", "awardee", "department")


@app.route("/notes/<record_type>/<token>/add", methods=["POST"])
def add_note_route(record_type, token):
    if record_type not in NOTE_TYPES:
        abort(404)
    record_id = decode_key(token)
    add_note(record_type, record_id, request.form.get("note_text", ""))
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#notes")


@app.route("/notes/<record_type>/<token>/<int:note_id>/delete", methods=["POST"])
def delete_note_route(record_type, token, note_id):
    if record_type not in NOTE_TYPES:
        abort(404)
    delete_note(note_id)
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#notes")


@app.route("/offices/<role>", defaults={"code_token": None})
@app.route("/offices/<role>/<code_token>")
def offices_view(role, code_token):
    if role not in ROLES:
        abort(404)
    cfg = ROLES[role]
    rows, total_direct, total_sub = build_offices_rows(role)

    selected = None
    code = None
    note_token = None
    notes = []
    if code_token:
        code = decode_key(code_token)
        selected = build_office_detail(role, code)
        if selected is None:
            abort(404)
        note_record_id = f"{role}:{code}"
        note_token = encode_key(note_record_id)
        notes = get_notes("office", note_record_id)

    return render_template(
        "offices.html",
        role=role,
        role_label=cfg["label"],
        roles=ROLES,
        rows=rows,
        total_direct=total_direct,
        total_sub=total_sub,
        selected=selected,
        selected_code=code,
        note_token=note_token,
        notes=notes,
        active=f"offices-{role}",
    )


@app.route("/department/<role>/<agency_token>/<dept_token>")
def department_detail_view(role, agency_token, dept_token):
    if role not in ROLES:
        abort(404)
    agency = decode_key(agency_token)
    department = decode_key(dept_token)
    detail = build_department_detail(role, agency, department)
    if detail is None:
        abort(404)

    note_record_id = f"{role}:{agency}:{department}"
    note_token = encode_key(note_record_id)
    notes = get_notes("department", note_record_id)

    return render_template(
        "department_detail.html",
        d=detail,
        role_label=ROLES[role]["label"],
        note_token=note_token,
        notes=notes,
        active=None,
    )


# --------------------------------------------------------------- awardees --

def build_awardees_rows():
    direct = (
        AWARDS.groupby("entity_key")
        .agg(
            direct_total=("total_federal_action_obligation", "sum"),
            direct_count=("award_id_piid", "nunique"),
            display_name=("recipient_name", "last"),
            state=("recipient_state_code", "last"),
        )
        .reset_index()
    )
    sub = (
        SUBAWARDS.groupby("entity_key")
        .agg(
            subaward_total=("subaward_amount", "sum"),
            subaward_count=("subaward_number", "count"),
            sub_display_name=("subawardee_name", "last"),
            sub_state=("subawardee_state_code", "last"),
        )
        .reset_index()
    )

    merged = pd.merge(direct, sub, on="entity_key", how="outer")
    merged["display_name"] = merged["display_name"].fillna(merged["sub_display_name"])
    merged["state"] = merged["state"].fillna(merged["sub_state"])
    merged[["direct_total", "direct_count", "subaward_total", "subaward_count"]] = merged[
        ["direct_total", "direct_count", "subaward_total", "subaward_count"]
    ].fillna(0)
    merged["combined_total"] = merged["direct_total"] + merged["subaward_total"]
    merged = merged.sort_values("combined_total", ascending=False)

    rows = merged.to_dict("records")
    for r in rows:
        r["url_key"] = encode_key(r["entity_key"])
        r["coverage"] = coverage(r["direct_total"], r["subaward_total"])
    return rows, merged["direct_total"].sum(), merged["subaward_total"].sum()


def build_awardee_detail(key: str):
    direct_awards = AWARDS[AWARDS["entity_key"] == key].sort_values(
        "total_federal_action_obligation", ascending=False
    )
    as_subawardee = SUBAWARDS[SUBAWARDS["entity_key"] == key].sort_values(
        "subaward_amount", ascending=False
    )

    if direct_awards.empty and as_subawardee.empty:
        return None

    if not direct_awards.empty:
        display_name = direct_awards.iloc[0]["recipient_name"]
        uei = direct_awards.iloc[0]["recipient_uei"]
        state = direct_awards.iloc[0]["recipient_state_code"]
        phone = format_phone(direct_awards.iloc[0]["recipient_phone_number"])
    else:
        display_name = as_subawardee.iloc[0]["subawardee_name"]
        uei = as_subawardee.iloc[0]["subawardee_uei"]
        state = as_subawardee.iloc[0]["subawardee_state_code"]
        phone = ""  # not available for subawardee-only companies in this data

    direct_rows = []
    for _, a in direct_awards.iterrows():
        direct_rows.append(
            {
                "award": a.to_dict(),
                "office_url_awarding": encode_key(str(a["awarding_office_code"])),
                "office_url_funding": encode_key(str(a["funding_office_code"])),
            }
        )

    sub_rows = []
    for _, s in as_subawardee.iterrows():
        sub_rows.append(
            {
                "sub": s.to_dict(),
                "office_url_awarding": encode_key(str(s["awarding_office_code"])),
            }
        )

    direct_total = direct_awards["total_federal_action_obligation"].sum()
    sub_total = as_subawardee["subaward_amount"].sum()
    n_awards = len(direct_awards)
    n_offices = direct_awards["awarding_office_name"].nunique()
    n_subawards = len(as_subawardee)
    n_primes = as_subawardee["prime_awardee_name"].nunique()

    top_share = None
    top_office = None
    if n_awards and direct_total > 0:
        by_office = direct_awards.groupby("awarding_office_name")["total_federal_action_obligation"].sum()
        top_office = by_office.idxmax()
        top_share = by_office.max() / direct_total

    strengths = []
    weaknesses = []
    if n_awards:
        if n_offices >= 3:
            strengths.append(f"Active with {n_offices} distinct awarding offices across {n_awards} direct award{'s' if n_awards != 1 else ''}.")
        else:
            strengths.append(f"{n_awards} direct award{'s' if n_awards != 1 else ''} worth {money(direct_total)} on record.")
    if n_subawards:
        strengths.append(
            f"{n_subawards} subcontracted engagement{'s' if n_subawards != 1 else ''} worth {money(sub_total)} under {n_primes} prime{'s' if n_primes != 1 else ''}, extending reach beyond direct awards."
        )
    if not strengths:
        strengths.append("No award activity on record yet for this company.")

    if not direct_rows:
        weaknesses.append(
            "No direct government awards on record — visibility is entirely dependent on subaward reporting, which many primes aren't required to disclose."
        )
    elif top_share is not None and top_share >= 0.5:
        weaknesses.append(f"{top_share * 100:.0f}% of direct-award spend comes from a single office ({top_office}).")
    if not sub_rows and direct_rows:
        weaknesses.append("No subcontracting activity on record for this company.")
    if n_awards and n_awards <= 2 and direct_rows:
        weaknesses.append(f"Thin sample — only {n_awards} direct award{'s' if n_awards != 1 else ''} on record.")
    weaknesses.append("Contract descriptions not yet classified as hardware vs. services — planned for Checkpoint 2.")

    return {
        "display_name": display_name,
        "uei": uei,
        "state": state,
        "phone": phone,
        "direct_rows": direct_rows,
        "sub_rows": sub_rows,
        "direct_total": direct_total,
        "sub_total": sub_total,
        "coverage": coverage(direct_total, sub_total),
        "visibility": visibility_tier(direct_total, sub_total),
        "concentration": concentration_tier(top_share),
        "strengths": strengths,
        "weaknesses": weaknesses,
    }


@app.route("/awardees", defaults={"key_token": None})
@app.route("/awardees/<key_token>")
def awardees_view(key_token):
    rows, total_direct, total_sub = build_awardees_rows()

    selected = None
    key = None
    note_token = None
    notes = []
    if key_token:
        key = decode_key(key_token)
        selected = build_awardee_detail(key)
        if selected is None:
            abort(404)
        note_token = encode_key(key)
        notes = get_notes("awardee", key)

    return render_template(
        "awardees.html",
        rows=rows,
        total_direct=total_direct,
        total_sub=total_sub,
        selected=selected,
        selected_key=key,
        note_token=note_token,
        notes=notes,
        active="awardees",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5057)
