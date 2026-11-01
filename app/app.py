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
import csv
import datetime
import io
import json
import os
import re
import secrets
import sqlite3
from pathlib import Path

import pandas as pd
from flask import Flask, Response, abort, g, jsonify, redirect, render_template, request, url_for

ROOT = Path(__file__).resolve().parent.parent
# QXC_DB_PATH lets a hosted deployment keep the database on a mounted volume
# (see deploy/entrypoint.sh, which seeds it from the image's copy on first
# boot). Unset locally, so `python app/app.py` behaves exactly as before.
DB_PATH = Path(os.environ.get("QXC_DB_PATH") or ROOT / "db" / "qxc.db")

app = Flask(__name__)

# ------------------------------------------------------------ deploy gate --
# Stopgap access control for hosted deployments until SSO lands. QXC_BASIC_AUTH
# holds one or more comma-separated "user:password[:role]" entries, e.g.
#   "qxc:S3cret,board:ReadOnlyPw:view"
# role is "edit" (default) or "view"; viewers can browse everything but every
# write (POST) is refused with 403. Passwords therefore can't contain ':' or
# ','. Wired from Secret Manager in deploy/terraform. When the var is unset
# the gate is off, so local runs are unaffected.
def _parse_basic_auth(raw: str) -> dict:
    users = {}
    for entry in raw.split(","):
        parts = entry.strip().split(":")
        if len(parts) < 2:
            continue
        user, password = parts[0].strip(), parts[1]
        role = parts[2].strip().lower() if len(parts) > 2 else "edit"
        if user and password:
            users[user] = (password, "view" if role == "view" else "edit")
    return users


BASIC_AUTH_USERS = _parse_basic_auth(os.environ.get("QXC_BASIC_AUTH", ""))
BASIC_AUTH_ENABLED = bool(BASIC_AUTH_USERS)

# Written at image build time by the Dockerfile (data vintage); absent locally.
_BUILD_INFO_PATH = DB_PATH.parent / "build_info.json"
BUILD_INFO = json.loads(_BUILD_INFO_PATH.read_text()) if _BUILD_INFO_PATH.exists() else {}


@app.get("/health")
def health():
    """Liveness/readiness probe for the container platform; never gated."""
    return jsonify(
        status="ok",
        awards=int(len(AWARDS)),
        subawards=int(len(SUBAWARDS)),
        user_data_store="firestore" if USE_FIRESTORE else "sqlite",
        data=BUILD_INFO,
    )


@app.before_request
def require_basic_auth():
    if not BASIC_AUTH_ENABLED or request.path == "/health":
        return None
    auth = request.authorization
    if auth is not None and auth.type == "basic":
        expected = BASIC_AUTH_USERS.get(auth.username or "")
        if expected is not None and secrets.compare_digest(auth.password or "", expected[0]):
            g.auth_user, g.auth_role = auth.username, expected[1]
            if expected[1] == "view" and request.method not in ("GET", "HEAD", "OPTIONS"):
                return Response("This account is view-only.", 403)
            return None
    return Response(
        "Authentication required.", 401, {"WWW-Authenticate": 'Basic realm="QXC Equipment Intelligence"'}
    )


# Source/Source URL are kept in the data for the team's own bookkeeping but
# are never shown in the app unless QXC_SHOW_SOURCES=1 is set on the service.
SHOW_SOURCES = os.environ.get("QXC_SHOW_SOURCES", "0") == "1"


@app.context_processor
def inject_auth():
    return {
        "auth_user": getattr(g, "auth_user", None),
        "auth_role": getattr(g, "auth_role", "edit"),
        "show_sources": SHOW_SOURCES,
        "data_through": DATA_VINTAGE["through"],
        "data_built": DATA_VINTAGE["built"],
    }


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

    for col in ["recipient_uei", "recipient_name", "recipient_cage_code",
                "product_or_service_code", "product_or_service_code_description"]:
        awards[col] = awards[col].fillna("")
    for col in ["subawardee_uei", "subawardee_name", "prime_awardee_name",
                "matched_award_id_piid", "prime_awardee_uei"]:
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


# Model numbers, equipment type and purchase category are text heuristics
# (there is no structured SKU/line-item field in the data); see classify.py.
import assistant  # noqa: E402
from classify import (  # noqa: E402
    CATEGORIES, EQUIPMENT_TYPES, classify_category, classify_equipment, extract_models,
)


# ---------------------------------------------------- read/write storage --
# The award/subaward data above is bundled read-only into the deploy (built
# by db/build_db.py) and always read via sqlite3, everywhere. But everything
# users enter at runtime (viewed status, notes, contacts, enrichment) must
# outlive a container, and Cloud Run gives an instance no durable local disk
# - so on Cloud Run those go to Firestore instead. Locally (no K_SERVICE env
# var, which only Cloud Run sets) they keep using db/qxc.db exactly as
# before, so `python app/app.py` needs no GCP credentials and behaves
# unchanged. QXC_USE_FIRESTORE=0|1 overrides the auto-detection either way.
USE_FIRESTORE = (
    os.environ.get("QXC_USE_FIRESTORE", "1" if os.environ.get("K_SERVICE") else "0") == "1"
)

if USE_FIRESTORE:
    from google.cloud import firestore

    _fs_client = firestore.Client(database=os.environ.get("QXC_FIRESTORE_DATABASE", "(default)"))


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


def ensure_enrichment_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS enrichment_targets (
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                added_at TEXT NOT NULL,
                PRIMARY KEY (record_type, record_id)
            )
            """
        )


def get_enrichment_set() -> set:
    if USE_FIRESTORE:
        docs = _fs_client.collection("enrichment_targets").stream()
        return {(d.get("record_type"), d.get("record_id")) for d in (doc.to_dict() for doc in docs)}
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("SELECT record_type, record_id FROM enrichment_targets")
        return set(cur.fetchall())


def set_enrichment_target(record_type: str, record_id: str, target: bool):
    if USE_FIRESTORE:
        ref = _fs_client.collection("enrichment_targets").document(f"{record_type}_{record_id}")
        if target:
            ref.set({"record_type": record_type, "record_id": record_id, "added_at": firestore.SERVER_TIMESTAMP})
        else:
            ref.delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        if target:
            conn.execute(
                "INSERT OR IGNORE INTO enrichment_targets (record_type, record_id, added_at) "
                "VALUES (?, ?, datetime('now'))",
                (record_type, record_id),
            )
        else:
            conn.execute(
                "DELETE FROM enrichment_targets WHERE record_type = ? AND record_id = ?",
                (record_type, record_id),
            )
        conn.commit()


ensure_enrichment_table()


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
        try:
            note_id = int(note_id)
        except (TypeError, ValueError):
            return
        conn.execute("DELETE FROM notes WHERE id = ?", (note_id,))
        conn.commit()


ensure_notes_table()


CONTACT_FIELDS = (
    "first_name", "last_name", "title", "organization_name", "uei", "cage",
    "agency_office", "email", "phone", "linkedin_url", "contact_type",
    "source", "source_url", "verified_at", "confidence",
)


def ensure_contacts_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contacts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                first_name TEXT,
                last_name TEXT,
                title TEXT,
                organization_name TEXT,
                uei TEXT,
                cage TEXT,
                agency_office TEXT,
                email TEXT,
                phone TEXT,
                linkedin_url TEXT,
                contact_type TEXT,
                source TEXT,
                source_url TEXT,
                verified_at TEXT,
                confidence TEXT,
                is_test_data INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_contacts_uei ON contacts(uei)")


def get_all_contacts() -> list:
    if USE_FIRESTORE:
        docs = _fs_client.collection("contacts").stream()
        out = []
        for doc in docs:
            d = doc.to_dict()
            d["id"] = doc.id
            out.append(d)
        return out
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM contacts ORDER BY id DESC")
        return [dict(row) for row in cur.fetchall()]


def get_contact(contact_id) -> dict:
    if USE_FIRESTORE:
        doc = _fs_client.collection("contacts").document(str(contact_id)).get()
        if not doc.exists:
            return None
        d = doc.to_dict()
        d["id"] = doc.id
        return d
    try:
        contact_id = int(contact_id)
    except (TypeError, ValueError):
        return None
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def create_contact(fields: dict, is_test_data: bool = False) -> str:
    data = {k: (fields.get(k) or "").strip() for k in CONTACT_FIELDS}
    if USE_FIRESTORE:
        doc_ref = _fs_client.collection("contacts").document()
        doc_ref.set(
            {
                **data,
                "is_test_data": is_test_data,
                "created_at": firestore.SERVER_TIMESTAMP,
                "updated_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return doc_ref.id
    columns = ", ".join(CONTACT_FIELDS)
    placeholders = ", ".join("?" for _ in CONTACT_FIELDS)
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            f"INSERT INTO contacts ({columns}, is_test_data, created_at, updated_at) "
            f"VALUES ({placeholders}, ?, datetime('now'), datetime('now'))",
            (*[data[k] for k in CONTACT_FIELDS], int(is_test_data)),
        )
        conn.commit()
        return str(cur.lastrowid)


def update_contact(contact_id, fields: dict):
    # Only fields the form actually sent are written, so hidden ones (source,
    # source_url) keep their stored values instead of being blanked.
    data = {k: (fields.get(k) or "").strip() for k in CONTACT_FIELDS if k in fields}
    if USE_FIRESTORE:
        _fs_client.collection("contacts").document(str(contact_id)).update(
            {**data, "updated_at": firestore.SERVER_TIMESTAMP}
        )
        return
    set_clause = ", ".join([*(f"{k} = ?" for k in data), "updated_at = datetime('now')"])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"UPDATE contacts SET {set_clause} WHERE id = ?",
            (*data.values(), int(contact_id)),
        )
        conn.commit()


def delete_contact(contact_id):
    if USE_FIRESTORE:
        _fs_client.collection("contacts").document(str(contact_id)).delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM contacts WHERE id = ?", (int(contact_id),))
        conn.commit()


def _contact_entity_key(c: dict) -> str:
    return entity_key(c.get("uei"), c.get("organization_name"))


def get_contacts_for_entity(key: str) -> list:
    return [c for c in get_all_contacts() if _contact_entity_key(c) == key]


def get_contacts_for_office(code: str) -> list:
    contact_ids = get_contact_ids_for_record("office", str(code))
    out = []
    for cid in contact_ids:
        c = get_contact(cid)
        if c is not None:
            out.append(c)
    return out


ensure_contacts_table()


def ensure_contact_records_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS contact_records (
                contact_id TEXT NOT NULL,
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                linked_at TEXT NOT NULL,
                PRIMARY KEY (contact_id, record_type, record_id)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_contact_records_record ON contact_records(record_type, record_id)"
        )


def link_contact_to_record(contact_id, record_type: str, record_id: str):
    contact_id = str(contact_id)
    record_id = str(record_id)
    if USE_FIRESTORE:
        doc_id = f"{contact_id}_{record_type}_{record_id}"
        _fs_client.collection("contact_records").document(doc_id).set(
            {
                "contact_id": contact_id,
                "record_type": record_type,
                "record_id": record_id,
                "linked_at": firestore.SERVER_TIMESTAMP,
            }
        )
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO contact_records (contact_id, record_type, record_id, linked_at) "
            "VALUES (?, ?, ?, datetime('now'))",
            (contact_id, record_type, record_id),
        )
        conn.commit()


def unlink_contact_from_record(contact_id, record_type: str, record_id: str):
    contact_id = str(contact_id)
    record_id = str(record_id)
    if USE_FIRESTORE:
        doc_id = f"{contact_id}_{record_type}_{record_id}"
        _fs_client.collection("contact_records").document(doc_id).delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM contact_records WHERE contact_id = ? AND record_type = ? AND record_id = ?",
            (contact_id, record_type, record_id),
        )
        conn.commit()


def get_record_links_for_contact(contact_id) -> list:
    contact_id = str(contact_id)
    if USE_FIRESTORE:
        docs = _fs_client.collection("contact_records").where("contact_id", "==", contact_id).stream()
        return [
            {"record_type": d.get("record_type"), "record_id": d.get("record_id")}
            for d in (doc.to_dict() for doc in docs)
        ]
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT record_type, record_id FROM contact_records WHERE contact_id = ?", (contact_id,)
        )
        return [{"record_type": rt, "record_id": rid} for rt, rid in cur.fetchall()]


def get_contact_ids_for_record(record_type: str, record_id) -> list:
    record_id = str(record_id)
    if USE_FIRESTORE:
        docs = (
            _fs_client.collection("contact_records")
            .where("record_type", "==", record_type)
            .where("record_id", "==", record_id)
            .stream()
        )
        return [d.get("contact_id") for d in (doc.to_dict() for doc in docs)]
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "SELECT contact_id FROM contact_records WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        return [row[0] for row in cur.fetchall()]


def get_records_for_contact_display(contact_id) -> list:
    out = []
    for link in get_record_links_for_contact(contact_id):
        rt, rid = link["record_type"], link["record_id"]
        if rt == "office":
            office_awards = AWARDS[AWARDS["awarding_office_code"].astype(str) == str(rid)]
            office_subs = SUBAWARDS[SUBAWARDS["awarding_office_code"].astype(str) == str(rid)]
            if office_awards.empty and office_subs.empty:
                continue
            name = (office_awards.iloc[0]["awarding_office_name"] if not office_awards.empty
                    else office_subs.iloc[0]["awarding_office_name"])
            out.append(
                {
                    "record_type": rt,
                    "record_id": rid,
                    "display_id": name,
                    "company_name": None,
                    "amount": None,
                    "url": url_for("offices_view", role="awarding", code_token=encode_key(str(rid))),
                }
            )
            continue
        match = RECORDS[(RECORDS["record_type"] == rt) & (RECORDS["record_id"].astype(str) == str(rid))]
        if match.empty:
            continue
        row = match.iloc[0]
        out.append(
            {
                "record_type": rt,
                "record_id": rid,
                "display_id": row["display_id"],
                "company_name": row["company_name"],
                "amount": row["amount"],
                "url": record_url(rt, rid),
            }
        )
    return out


ensure_contact_records_table()


ENRICHMENT_FIELDS = ("quantity", "equipment_details", "source", "source_url", "verified_at", "confidence")


def ensure_record_enrichment_table():
    if USE_FIRESTORE:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS record_enrichment (
                record_type TEXT NOT NULL,
                record_id TEXT NOT NULL,
                quantity TEXT,
                equipment_details TEXT,
                source TEXT,
                source_url TEXT,
                verified_at TEXT,
                confidence TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (record_type, record_id)
            )
            """
        )


def get_record_enrichment(record_type: str, record_id) -> dict:
    record_id = str(record_id)
    if USE_FIRESTORE:
        doc = _fs_client.collection("record_enrichment").document(f"{record_type}_{record_id}").get()
        if not doc.exists:
            return None
        d = doc.to_dict()
        # Same "YYYY-MM-DD HH:MM:SS" text SQLite returns, not a datetime repr.
        for k in ("created_at", "updated_at"):
            d[k] = _fs_timestamp(d.get(k))
        return d
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        cur = conn.execute(
            "SELECT * FROM record_enrichment WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def save_record_enrichment(record_type: str, record_id, fields: dict):
    record_id = str(record_id)
    data = {k: (fields.get(k) or "").strip() for k in ENRICHMENT_FIELDS}
    # On update, only fields the form sent are overwritten (hidden source
    # fields keep their stored values); a brand-new row gets every field.
    sent = {k: v for k, v in data.items() if k in fields}
    if USE_FIRESTORE:
        doc_ref = _fs_client.collection("record_enrichment").document(f"{record_type}_{record_id}")
        existing = doc_ref.get()
        payload = {**(sent if existing.exists else data), "record_type": record_type, "record_id": record_id,
                   "updated_at": firestore.SERVER_TIMESTAMP}
        if not existing.exists:
            payload["created_at"] = firestore.SERVER_TIMESTAMP
        doc_ref.set(payload, merge=True)
        return
    columns = ", ".join(ENRICHMENT_FIELDS)
    placeholders = ", ".join("?" for _ in ENRICHMENT_FIELDS)
    update_clause = ", ".join([*(f"{k} = excluded.{k}" for k in sent), "updated_at = datetime('now')"])
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            f"INSERT INTO record_enrichment (record_type, record_id, {columns}, created_at, updated_at) "
            f"VALUES (?, ?, {placeholders}, datetime('now'), datetime('now')) "
            f"ON CONFLICT(record_type, record_id) DO UPDATE SET {update_clause}",
            (record_type, record_id, *[data[k] for k in ENRICHMENT_FIELDS]),
        )
        conn.commit()


def delete_record_enrichment(record_type: str, record_id):
    record_id = str(record_id)
    if USE_FIRESTORE:
        _fs_client.collection("record_enrichment").document(f"{record_type}_{record_id}").delete()
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "DELETE FROM record_enrichment WHERE record_type = ? AND record_id = ?",
            (record_type, record_id),
        )
        conn.commit()


ensure_record_enrichment_table()


def build_records() -> pd.DataFrame:
    """One row per individual contract/subaward record, normalized to a
    shared column set so the agency rollup and the parametric search can
    treat direct awards and subawards uniformly."""
    common = [
        "record_type", "record_id", "display_id", "company_name", "company_key", "prime_name",
        "description", "amount", "action_date", "service_start", "contact_phone",
        "company_uei", "prime_uei", "psc_code", "psc_description", "cage_code",
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
    a["company_uei"] = a["recipient_uei"]
    a["prime_uei"] = ""
    a["psc_code"] = a["product_or_service_code"]
    a["psc_description"] = a["product_or_service_code_description"]
    a["cage_code"] = a["recipient_cage_code"]

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
    s["company_uei"] = s["subawardee_uei"]
    s["prime_uei"] = s["prime_awardee_uei"]
    s["psc_code"] = ""
    s["psc_description"] = ""
    s["cage_code"] = ""

    records = pd.concat([a[common], s[common]], ignore_index=True)
    records["office_url_awarding"] = records["awarding_office_code"].astype(str).apply(encode_key)
    records["office_url_funding"] = records["funding_office_code"].astype(str).apply(encode_key)
    records["company_url"] = records["company_key"].apply(encode_key)
    records["models"] = records["description"].apply(extract_models)
    records["models_str"] = records["models"].apply(lambda m: ", ".join(m))

    # Purchase-type fields used by the filters and the CSV export.
    records["award_type"] = pd.concat(
        [a["award_type"].fillna("").astype(str).str.title(), s["subaward_type"].fillna("").astype(str).str.title()],
        ignore_index=True,
    ).str.replace("Bpa", "BPA")
    records["parent_award_id"] = pd.concat(
        [a["parent_award_id_piid"].fillna("").astype(str), pd.Series([""] * len(s))], ignore_index=True
    )
    records["year"] = pd.to_datetime(records["action_date"], errors="coerce", format="mixed").dt.year
    records["categories"] = [
        classify_category(d, p, pd_)
        for d, p, pd_ in zip(records["description"], records["psc_code"], records["psc_description"])
    ]
    records["category_str"] = records["categories"].apply(" + ".join)
    records["equipment"] = [classify_equipment(d, m) for d, m in zip(records["description"], records["models"])]
    records["equipment_str"] = records["equipment"].apply(", ".join)
    return records


RECORDS = build_records()


def _data_vintage() -> dict:
    """When the data runs through and (on a hosted build) when it was rebuilt."""
    latest = pd.to_datetime(RECORDS["action_date"], errors="coerce", format="mixed").max()
    built = (BUILD_INFO.get("built_at") or "")[:10]
    return {
        "through": latest.strftime("%b %d, %Y").replace(" 0", " ") if pd.notna(latest) else "",
        "built": built,
    }


DATA_VINTAGE = _data_vintage()


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


def contact_url(contact_id) -> str:
    return url_for("contact_detail_view", token=encode_key(str(contact_id)))


def contact_token(contact_id) -> str:
    return encode_key(str(contact_id))


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
app.jinja_env.globals["contact_url"] = contact_url
app.jinja_env.globals["contact_token"] = contact_token
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


def filter_records(role: str, agency: str = None, department: str = None, company_q: str = None,
                    prime_q: str = None, uei_q: str = None, cage_q: str = None, psc_q: str = None,
                    q: str = None, enrichment_only: bool = False, year: int = None,
                    amount_min: float = None, amount_max: float = None, award_type: str = None,
                    model_q: str = None, category: str = None, equipment: str = None):
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
    if prime_q:
        ql = prime_q.strip().lower()
        df = df[df["prime_name"].fillna("").str.lower().str.contains(ql, na=False)]
    if uei_q:
        ql = uei_q.strip().lower()
        df = df[
            df["company_uei"].fillna("").str.lower().str.contains(ql, na=False)
            | df["prime_uei"].fillna("").str.lower().str.contains(ql, na=False)
        ]
    if cage_q:
        ql = cage_q.strip().lower()
        df = df[df["cage_code"].fillna("").str.lower().str.contains(ql, na=False)]
    if psc_q:
        ql = psc_q.strip().lower()
        df = df[
            df["psc_code"].fillna("").str.lower().str.contains(ql, na=False)
            | df["psc_description"].fillna("").str.lower().str.contains(ql, na=False)
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
    if year is not None:
        df = df[df["year"] == year]
    if amount_min is not None:
        df = df[df["amount"] >= amount_min]
    if amount_max is not None:
        df = df[df["amount"] <= amount_max]
    if award_type:
        df = df[df["award_type"] == award_type]
    if model_q:
        ql = model_q.strip().lower()
        df = df[df["models_str"].fillna("").str.lower().str.contains(ql, regex=False, na=False)]
    if category:
        df = df[df["categories"].apply(lambda labels: category in labels)]
    if equipment:
        df = df[df["equipment"].apply(lambda labels: equipment in labels)]
    if enrichment_only:
        targets = get_enrichment_set()
        keep = pd.Series(
            [(rt, str(rid)) in targets for rt, rid in zip(df["record_type"], df["record_id"])],
            index=df.index, dtype=bool,
        )
        df = df[keep]
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
                    {**_field("Parent Award ID (Vehicle)", a["parent_award_id_piid"]),
                     "href": (url_for("vehicle_detail_view", token=encode_key(str(a["parent_award_id_piid"])))
                              if a["parent_award_id_piid"] not in (None, "", "nan") and not pd.isna(a["parent_award_id_piid"]) else None)},
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

        description = a["transaction_description"] or a["base_transaction_description"]
        return {
            "record_type": "award",
            "record_id": record_id,
            "type_label": "Direct Award",
            "title": a["award_id_piid"],
            "description": description,
            "models": extract_models(description),
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

        description = s["subaward_description"] or s["prime_award_description"]
        return {
            "record_type": "subaward",
            "record_id": record_id,
            "type_label": "Subaward",
            "title": s["subaward_number"] or record_id,
            "description": description,
            "models": extract_models(description),
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


def build_contact_detail(contact_id):
    c = get_contact(contact_id)
    if c is None:
        return None

    org_key = _contact_entity_key(c)
    org_url = None
    if (c.get("uei") or c.get("organization_name")) and not RECORDS[RECORDS["company_key"] == org_key].empty:
        org_url = encode_key(org_key)

    full_name = f"{c.get('first_name') or ''} {c.get('last_name') or ''}".strip()

    groups = [
        {
            "title": "Contact",
            "fields": [
                _field("Name", full_name),
                _field("Title/Role", c.get("title")),
                _field("Contact Type", c.get("contact_type")),
                _field("Email", c.get("email")),
                _field("Phone", c.get("phone")),
                _field("LinkedIn", c.get("linkedin_url")),
            ],
        },
        {
            "title": "Organization",
            "fields": [
                _field("Organization", c.get("organization_name")),
                _field("UEI", c.get("uei")),
                _field("CAGE", c.get("cage")),
                _field("Agency/Office", c.get("agency_office")),
            ],
        },
        {
            "title": "Provenance",
            "fields": [
                *([_field("Source", c.get("source")), _field("Source URL", c.get("source_url"))] if SHOW_SOURCES else []),
                _field("Verification Date", c.get("verified_at")),
                _field("Data Confidence", c.get("confidence")),
            ],
        },
    ]

    return {
        "id": str(c["id"]),
        "first_name": c.get("first_name") or "",
        "last_name": c.get("last_name") or "",
        "title": full_name or "(unnamed contact)",
        "is_test_data": bool(c.get("is_test_data")),
        "org_url": org_url,
        "organization_name": c.get("organization_name"),
        "groups": groups,
        "linked_records": get_records_for_contact_display(c["id"]),
    }


# ------------------------------------------------------- contract vehicles --
# A "vehicle" is the parent contract (IDV/BPA/GWAC) an order was placed against.
# Orders carry the parent's id, so we can show which vehicles the money flows
# through, which resellers sell on them, and which offices buy from them.

VEHICLE_ORDERS = RECORDS[(RECORDS["record_type"] == "award") & (RECORDS["parent_award_id"] != "")].copy()


def build_vehicle_rows(q: str = None) -> list:
    g = VEHICLE_ORDERS
    if g.empty:
        return []
    top = (
        g.groupby(["parent_award_id", "company_name"])["amount"].sum().reset_index()
        .sort_values("amount", ascending=False).drop_duplicates("parent_award_id")
        .set_index("parent_award_id")["company_name"]
    )
    rows = (
        g.groupby("parent_award_id")
        .agg(orders=("record_id", "nunique"), resellers=("company_key", "nunique"),
             offices=("awarding_office_code", "nunique"), agencies=("awarding_agency_name", "nunique"),
             total=("amount", "sum"), first=("action_date", "min"), last=("action_date", "max"))
        .reset_index()
    )
    rows["top_reseller"] = rows["parent_award_id"].map(top)
    if q:
        ql = q.strip().lower()
        names = g.groupby("parent_award_id")["company_name"].apply(lambda s: " ".join(s.dropna().unique()).lower())
        keep = rows["parent_award_id"].str.lower().str.contains(ql, regex=False) | rows["parent_award_id"].map(names).fillna("").str.contains(ql, regex=False)
        rows = rows[keep]
    rows = rows.sort_values("total", ascending=False)
    out = rows.to_dict("records")
    for r in out:
        r["url"] = encode_key(r["parent_award_id"])
    return out


def build_vehicle_detail(vehicle_id: str):
    orders = VEHICLE_ORDERS[VEHICLE_ORDERS["parent_award_id"] == vehicle_id]
    if orders.empty:
        return None
    resellers = (
        orders.groupby(["company_key", "company_name"])
        .agg(orders=("record_id", "nunique"), total=("amount", "sum"),
             offices=("awarding_office_code", "nunique"))
        .reset_index().sort_values("total", ascending=False)
    )
    resellers["url"] = resellers["company_key"].apply(encode_key)
    offices = (
        orders.groupby(["awarding_office_code", "awarding_office_name", "awarding_agency_name"])
        .agg(orders=("record_id", "nunique"), total=("amount", "sum"),
             resellers=("company_key", "nunique"))
        .reset_index().sort_values("total", ascending=False)
    )
    offices["url"] = offices["awarding_office_code"].astype(str).apply(encode_key)
    subs = SUBAWARDS[SUBAWARDS["prime_award_piid"].isin(orders["record_id"])]
    return {
        "id": vehicle_id,
        "orders": len(orders),
        "total": float(orders["amount"].sum()),
        "first": orders["action_date"].min(),
        "last": orders["action_date"].max(),
        "resellers": resellers.to_dict("records"),
        "offices": offices.to_dict("records"),
        "order_rows": orders.sort_values("amount", ascending=False).head(300).to_dict("records"),
        "orders_truncated": len(orders) > 300,
        "subawards": subs.to_dict("records"),
        "agencies": sorted(a for a in orders["awarding_agency_name"].dropna().unique() if a),
    }


def vehicles_for_company(company_key: str) -> list:
    o = VEHICLE_ORDERS[VEHICLE_ORDERS["company_key"] == company_key]
    if o.empty:
        return []
    rows = (
        o.groupby("parent_award_id")
        .agg(orders=("record_id", "nunique"), total=("amount", "sum"), offices=("awarding_office_code", "nunique"))
        .reset_index().sort_values("total", ascending=False)
    )
    out = rows.to_dict("records")
    for r in out:
        r["url"] = encode_key(r["parent_award_id"])
    return out


@app.route("/vehicles")
def vehicles_view():
    q = request.args.get("q") or None
    rows = build_vehicle_rows(q)
    return render_template(
        "vehicles.html",
        rows=rows[:300],
        shown=min(len(rows), 300),
        total_vehicles=len(rows),
        total_orders=int(sum(r["orders"] for r in rows)),
        total_amount=float(sum(r["total"] for r in rows)),
        q=q or "",
        active="vehicles",
    )


@app.route("/vehicles/<token>")
def vehicle_detail_view(token):
    d = build_vehicle_detail(decode_key(token))
    if d is None:
        abort(404)
    return render_template("vehicle_detail.html", v=d, token=token, active="vehicles")


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
        "contacts": get_contacts_for_office(code),
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


def _to_number(raw):
    """Parse "$1,500" to 1500.0; blank or unparseable gives None (filter ignored)."""
    try:
        return float(str(raw).replace("$", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_record_filters(args) -> dict:
    """Query-string filters shared by the records page and the CSV export,
    returned as keyword arguments for filter_records."""
    role = args.get("role", "awarding")
    if role not in ROLES:
        abort(404)
    category = args.get("category") or None
    equipment = args.get("equipment") or None
    return {
        "role": role,
        "agency": args.get("agency") or None,
        "department": args.get("department") or None,
        "company_q": args.get("company") or None,
        "prime_q": args.get("prime") or None,
        "uei_q": args.get("uei") or None,
        "cage_q": args.get("cage") or None,
        "psc_q": args.get("psc") or None,
        "q": args.get("q") or None,
        "enrichment_only": args.get("enrichment") == "1",
        "year": args.get("year", type=int),
        "amount_min": _to_number(args.get("amount_min")),
        "amount_max": _to_number(args.get("amount_max")),
        "award_type": args.get("award_type") or None,
        "model_q": args.get("model") or None,
        "category": category if category in CATEGORIES else None,
        "equipment": equipment if equipment in EQUIPMENT_TYPES else None,
    }


def active_filter_args(args) -> dict:
    """The non-empty query args to carry into links such as the export."""
    keys = ("role", "agency", "department", "company", "prime", "uei", "cage", "psc", "q", "enrichment",
            "year", "amount_min", "amount_max", "award_type", "model", "category", "equipment")
    return {k: args.get(k) for k in keys if args.get(k)}


@app.route("/records")
def records_view():
    f = parse_record_filters(request.args)
    role, agency, department = f["role"], f["agency"], f["department"]
    company_q, prime_q, uei_q, cage_q, psc_q, q = f["company_q"], f["prime_q"], f["uei_q"], f["cage_q"], f["psc_q"], f["q"]
    enrichment_only = f["enrichment_only"]

    df = filter_records(**f)
    viewed = get_viewed_set()
    enrichment_set = get_enrichment_set()

    total_matches = len(df)
    # The static-site freezer passes limit=all so every record is embedded
    # in the frozen page for the client-side filter script to work with;
    # the live app always uses the normal cap.
    LIMIT = total_matches if request.args.get("limit") == "all" else 250
    df = df.head(LIMIT)

    records = df.to_dict("records")
    for r in records:
        r["is_viewed"] = (r["record_type"], str(r["record_id"])) in viewed
        r["is_enrichment_target"] = (r["record_type"], str(r["record_id"])) in enrichment_set

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
        prime_q=prime_q,
        uei_q=uei_q,
        cage_q=cage_q,
        psc_q=psc_q,
        enrichment_only=enrichment_only,
        q=q,
        year=f["year"],
        amount_min=request.args.get("amount_min", ""),
        amount_max=request.args.get("amount_max", ""),
        award_type=f["award_type"],
        model_q=f["model_q"],
        category=f["category"],
        equipment=f["equipment"],
        year_options=sorted(int(y) for y in RECORDS["year"].dropna().unique()),
        award_type_options=sorted(v for v in RECORDS["award_type"].unique() if v),
        category_options=CATEGORIES,
        equipment_options=EQUIPMENT_TYPES,
        export_args=active_filter_args(request.args),
        agency_names=agency_names,
        dept_names=department_options(role, agency),
        active="records",
    )


# Columns of the CSV export, in order: (header, record field or a callable).
EXPORT_COLUMNS = [
    ("Record Type", lambda r: "Direct Award" if r["record_type"] == "award" else "Subaward"),
    ("Record ID", "display_id"),
    ("Company", "company_name"),
    ("Prime Contractor (subawards)", "prime_name"),
    ("Amount (USD)", None),  # numeric, written separately so it is never quote-prefixed
    ("Action Date", "action_date"),
    ("Year", "year"),
    ("Service Start", "service_start"),
    ("Award Type", "award_type"),
    ("Vehicle (Parent Award)", "parent_award_id"),
    ("Purchase Category", "category_str"),
    ("Equipment", "equipment_str"),
    ("Model Numbers", "models_str"),
    ("Description", "description"),
    ("Awarding Agency", "awarding_agency_name"),
    ("Awarding Sub-Agency", "awarding_sub_agency_name"),
    ("Awarding Office", "awarding_office_name"),
    ("Funding Agency", "funding_agency_name"),
    ("Funding Sub-Agency", "funding_sub_agency_name"),
    ("Funding Office", "funding_office_name"),
    ("PSC Code", "psc_code"),
    ("PSC Description", "psc_description"),
    ("Company UEI", "company_uei"),
    ("Prime UEI", "prime_uei"),
    ("CAGE", "cage_code"),
    ("Contact Phone", "contact_phone"),
]


def _csv_text(value) -> str:
    """Text cell safe to open in a spreadsheet: a leading = + - @ would be run
    as a formula, so those cells get a leading apostrophe."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    text = str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


@app.route("/records/export.csv")
def records_export():
    """Every record matching the same filters as the Search Records page
    (not capped at the 250 shown on screen)."""
    df = filter_records(**parse_record_filters(request.args))
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow([header for header, _ in EXPORT_COLUMNS])
    for r in df.to_dict("records"):
        row = []
        for header, field in EXPORT_COLUMNS:
            if header == "Amount (USD)":
                row.append("" if pd.isna(r["amount"]) else f"{r['amount']:.2f}")
            elif header == "Year":
                row.append("" if pd.isna(r["year"]) else int(r["year"]))
            elif callable(field):
                row.append(_csv_text(field(r)))
            else:
                row.append(_csv_text(r.get(field)))
        writer.writerow(row)
    # BOM so Excel opens it as UTF-8 rather than guessing a code page.
    resp = Response("\ufeff" + out.getvalue(), mimetype="text/csv")
    resp.headers["Content-Disposition"] = (
        f'attachment; filename="qxc-records-{datetime.date.today().isoformat()}.csv"'
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.route("/api/records/<record_type>/<record_id>/viewed", methods=["POST"])
def api_set_viewed(record_type, record_id):
    if record_type not in ("award", "subaward"):
        abort(404)
    payload = request.get_json(silent=True) or {}
    viewed = bool(payload.get("viewed", True))
    set_viewed(record_type, record_id, viewed)
    return jsonify({"viewed": viewed})


@app.route("/api/records/<record_type>/<record_id>/enrichment_target", methods=["POST"])
def api_set_enrichment_target(record_type, record_id):
    if record_type not in ("award", "subaward"):
        abort(404)
    payload = request.get_json(silent=True) or {}
    target = bool(payload.get("target", True))
    set_enrichment_target(record_type, record_id, target)
    return jsonify({"target": target})


@app.route("/record/<record_type>/<token>")
def record_detail_view(record_type, token):
    if record_type not in ("award", "subaward"):
        abort(404)
    record_id = decode_key(token)
    detail = build_record_detail(record_type, record_id)
    if detail is None:
        abort(404)
    detail["viewed"] = (record_type, str(record_id)) in get_viewed_set()
    detail["is_enrichment_target"] = (record_type, str(record_id)) in get_enrichment_set()
    detail["notes"] = get_notes(record_type, record_id)
    detail["enrichment"] = get_record_enrichment(record_type, record_id)
    related_contact_ids = {str(cid) for cid in get_contact_ids_for_record(record_type, record_id)}
    related_contacts = [get_contact(cid) for cid in related_contact_ids]
    detail["related_contacts"] = [c for c in related_contacts if c]
    org_key = decode_key(detail["company_url"])
    detail["org_contacts_for_linking"] = [
        c for c in get_contacts_for_entity(org_key) if str(c["id"]) not in related_contact_ids
    ]
    return render_template("record_detail.html", r=detail, token=token, active=None)


NOTE_TYPES = ("award", "subaward", "office", "awardee", "department", "contact")


@app.route("/notes/<record_type>/<token>/add", methods=["POST"])
def add_note_route(record_type, token):
    if record_type not in NOTE_TYPES:
        abort(404)
    record_id = decode_key(token)
    add_note(record_type, record_id, request.form.get("note_text", ""))
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#notes")


@app.route("/notes/<record_type>/<token>/<note_id>/delete", methods=["POST"])
def delete_note_route(record_type, token, note_id):
    if record_type not in NOTE_TYPES:
        abort(404)
    delete_note(note_id)
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#notes")


@app.route("/contact/<token>")
def contact_detail_view(token):
    contact_id = decode_key(token)
    detail = build_contact_detail(contact_id)
    if detail is None:
        abort(404)
    detail["notes"] = get_notes("contact", str(contact_id))
    raw = get_contact(contact_id)
    return render_template("contact_detail.html", c=detail, raw=raw, token=token, active=None)


@app.route("/contacts/add", methods=["POST"])
def add_contact_route():
    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()
    if not first_name and not last_name:
        return redirect(request.referrer or url_for("index"))
    contact_id = create_contact(request.form, is_test_data=False)
    link_record_type = request.form.get("link_record_type")
    link_record_id = request.form.get("link_record_id")
    if link_record_type and link_record_id:
        link_contact_to_record(contact_id, link_record_type, link_record_id)
    return redirect(url_for("contact_detail_view", token=encode_key(contact_id)))


@app.route("/contact/<token>/edit", methods=["POST"])
def edit_contact_route(token):
    contact_id = decode_key(token)
    first_name = (request.form.get("first_name") or "").strip()
    last_name = (request.form.get("last_name") or "").strip()
    if not first_name and not last_name:
        return redirect(url_for("contact_detail_view", token=token))
    if get_contact(contact_id) is None:
        abort(404)
    update_contact(contact_id, request.form)
    return redirect(url_for("contact_detail_view", token=token))


@app.route("/contact/<token>/delete", methods=["POST"])
def delete_contact_route(token):
    contact_id = decode_key(token)
    if get_contact(contact_id) is not None:
        delete_contact(contact_id)
    return redirect(request.referrer or url_for("index"))


@app.route("/contact/<token>/unlink", methods=["POST"])
def unlink_contact_route(token):
    contact_id = decode_key(token)
    record_type = request.form.get("record_type")
    record_id = request.form.get("record_id")
    if record_type and record_id:
        unlink_contact_from_record(contact_id, record_type, record_id)
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#contacts")


@app.route("/record/<record_type>/<token>/link_contact", methods=["POST"])
def link_contact_to_record_route(record_type, token):
    if record_type not in ("award", "subaward", "office"):
        abort(404)
    record_id = decode_key(token)
    contact_id = request.form.get("contact_id")
    if contact_id:
        link_contact_to_record(contact_id, record_type, record_id)
    return redirect((request.referrer or url_for("index")).split("#")[0] + "#contacts")


@app.route("/record/<record_type>/<token>/enrichment/save", methods=["POST"])
def save_record_enrichment_route(record_type, token):
    if record_type not in ("award", "subaward"):
        abort(404)
    record_id = decode_key(token)
    save_record_enrichment(record_type, record_id, request.form)
    return redirect(url_for("record_detail_view", record_type=record_type, token=token) + "#purchase-info")


@app.route("/record/<record_type>/<token>/enrichment/delete", methods=["POST"])
def delete_record_enrichment_route(record_type, token):
    if record_type not in ("award", "subaward"):
        abort(404)
    record_id = decode_key(token)
    delete_record_enrichment(record_type, record_id)
    return redirect(url_for("record_detail_view", record_type=record_type, token=token) + "#purchase-info")


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
    contacts = []
    if key_token:
        key = decode_key(key_token)
        selected = build_awardee_detail(key)
        if selected is None:
            abort(404)
        note_token = encode_key(key)
        notes = get_notes("awardee", key)
        contacts = get_contacts_for_entity(key)
        selected["vehicles"] = vehicles_for_company(key)

    return render_template(
        "awardees.html",
        rows=rows,
        total_direct=total_direct,
        total_sub=total_sub,
        selected=selected,
        selected_key=key,
        note_token=note_token,
        notes=notes,
        contacts=contacts,
        active="awardees",
    )


# --------------------------------------------------------- AI assistant (off) --
# Read-only lookups the assistant may call. Nothing here writes, and no
# source fields are ever included. See assistant.py and docs/ai-assistant-plan.md.

_ASSISTANT_GROUPS = {
    "agency": "awarding_agency_name", "year": "year", "category": "categories",
    "equipment": "equipment", "award_type": "award_type", "company": "company_name",
}


_ASSISTANT_FILTER_KEYS = {"q", "agency", "department", "company", "year", "amount_min", "amount_max",
                          "category", "equipment", "model"}


def _assistant_filtered(**kw):
    from werkzeug.datastructures import MultiDict

    unknown = set(kw) - _ASSISTANT_FILTER_KEYS
    if unknown:
        raise TypeError(f"unknown filter(s): {', '.join(sorted(unknown))}")
    args = MultiDict({k: str(v) for k, v in kw.items() if v not in (None, "")})
    return filter_records(**parse_record_filters(args))


def assistant_search_records(**kw) -> dict:
    df = _assistant_filtered(**kw)
    rows = [
        {
            "record_id": r["display_id"], "type": r["record_type"], "company": r["company_name"],
            "amount": round(float(r["amount"]), 2), "date": r["action_date"],
            "office": r["awarding_office_name"], "agency": r["awarding_agency_name"],
            "description": (r["description"] or "")[:200], "category": r["category_str"],
            "equipment": r["equipment_str"], "models": r["models_str"], "vehicle": r["parent_award_id"],
        }
        for r in df.head(assistant.MAX_ROWS).to_dict("records")
    ]
    return {"total_matches": int(len(df)), "returned": len(rows), "records": rows}


def assistant_summarize(group_by: str, **kw) -> dict:
    col = _ASSISTANT_GROUPS.get(group_by)
    if col is None:
        return {"error": f"group_by must be one of {sorted(_ASSISTANT_GROUPS)}"}
    df = _assistant_filtered(**kw)
    if col in ("categories", "equipment"):
        df = df.explode(col)
    g = (
        df.groupby(col, dropna=False)["amount"].agg(["count", "sum"]).reset_index()
        .sort_values("sum", ascending=False).head(25)
    )
    return {"group_by": group_by, "groups": [
        {"value": str(r[col]), "records": int(r["count"]), "total": round(float(r["sum"]), 2)} for r in g.to_dict("records")
    ]}


def assistant_get_vehicle(vehicle_id: str) -> dict:
    d = build_vehicle_detail(vehicle_id)
    if d is None:
        return {"error": "no such vehicle"}
    return {
        "vehicle": d["id"], "orders": d["orders"], "total": round(d["total"], 2), "first": d["first"], "last": d["last"],
        "resellers": [{"name": r["company_name"], "orders": r["orders"], "total": round(float(r["total"]), 2)} for r in d["resellers"][:10]],
        "offices": [{"name": o["awarding_office_name"], "orders": o["orders"], "total": round(float(o["total"]), 2)} for o in d["offices"][:10]],
    }


assistant.init(assistant_search_records, assistant_summarize, assistant_get_vehicle)


@app.post("/api/assistant")
def api_assistant():
    """Disabled (404) unless QXC_ASSISTANT_ENABLED=1 and OPENROUTER_API_KEY is set."""
    if not assistant.enabled():
        abort(404)
    payload = request.get_json(silent=True) or {}
    message = (payload.get("message") or "").strip() if isinstance(payload.get("message"), str) else ""
    if not message or len(message) > 2000:
        return jsonify(error="message is required (max 2000 characters)"), 400
    # History comes from the browser, so only plain user/assistant text is accepted.
    history = [
        {"role": m["role"], "content": m["content"][:4000]}
        for m in (payload.get("history") or [])[-10:]
        if isinstance(m, dict) and m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
    ]
    try:
        return jsonify(reply=assistant.chat(message, history=history)["reply"])
    except Exception:
        app.logger.exception("assistant call failed")
        return jsonify(error="The assistant is unavailable right now."), 502


if __name__ == "__main__":
    # Local dev server only. Hosted deployments run gunicorn via
    # deploy/entrypoint.sh and never reach this block.
    app.run(
        debug=os.environ.get("FLASK_DEBUG", "1") == "1",
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "5057")),
    )
