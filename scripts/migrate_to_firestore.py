"""
One-off migration: copy user-entered data from the local SQLite file
(db/qxc.db) into the hosted Firestore database used by the Cloud Run deploy.

What moves (the same collections app/app.py writes when USE_FIRESTORE is on):
  contacts, contact_records, record_enrichment, notes, viewed_records,
  enrichment_targets.
The award/subaward tables are NOT touched; those ship in the container image.

Safe by default
  * Dry run unless --apply is passed. A dry run reads Firestore (to see what is
    already there) but writes nothing. Use --offline to skip even that and
    plan against an empty Firestore (no credentials needed).
  * Never overwrites or deletes anything already in Firestore. Existing
    documents win; a re-run after a successful run plans zero writes.
  * Rows flagged is_test_data, and notes starting with "Test note", are
    skipped unless you pass --include-test.

De-duplication
  * Contacts: matched on (email, UEI) when there is an email, otherwise on
    (first name, last name, organization, UEI), all case-insensitive. The same
    person under two different UEIs stays as two contacts, as in the app.
    A matched contact reuses the Firestore id, so record links still point at
    the right document. New contacts get the id "local-<sqlite id>".
  * Links, enrichment, viewed and targets: deterministic document ids, same as
    the app, so an existing document is simply skipped.
  * Notes: matched on (record_type, record_id, text).

Usage (from the project root, after `gcloud auth application-default login`
with the kiefer.xyz account):
    pip install google-cloud-firestore
    python scripts/migrate_to_firestore.py --project quantum-513214
    python scripts/migrate_to_firestore.py --project quantum-513214 --apply
"""
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = ROOT / "db" / "qxc.db"
BATCH_LIMIT = 400  # Firestore caps a batch at 500 operations

CONTACT_FIELDS = (
    "first_name", "last_name", "title", "organization_name", "uei", "cage",
    "agency_office", "email", "phone", "linkedin_url", "contact_type",
    "source", "source_url", "verified_at", "confidence",
)
ENRICHMENT_FIELDS = ("quantity", "equipment_details", "source", "source_url", "verified_at", "confidence")


def norm(value) -> str:
    return (value or "").strip().lower()


def contact_key(c: dict) -> tuple:
    if norm(c.get("email")):
        return ("email", norm(c.get("email")), norm(c.get("uei")))
    return (
        "name",
        norm(c.get("first_name")),
        norm(c.get("last_name")),
        norm(c.get("organization_name")),
        norm(c.get("uei")),
    )


def ts(value):
    """SQLite stores datetime('now') as UTC text; Firestore wants a datetime."""
    if not value:
        return datetime.now(timezone.utc)
    return datetime.strptime(value[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def read_local(db_path: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row

    def rows(table):
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")] if exists else []

    data = {t: rows(t) for t in (
        "contacts", "contact_records", "record_enrichment", "notes", "viewed_records", "enrichment_targets",
    )}
    conn.close()
    return data


def read_existing(client) -> dict:
    """Snapshot of the Firestore collections we may collide with."""
    if client is None:
        return {"contacts": {}, "notes": set(), "docs": {}}
    contacts = {}
    for doc in client.collection("contacts").stream():
        contacts[contact_key(doc.to_dict())] = doc.id
    notes = {
        (d.get("record_type"), str(d.get("record_id")), (d.get("note_text") or "").strip())
        for d in (doc.to_dict() for doc in client.collection("notes").stream())
    }
    docs = {
        name: {doc.id for doc in client.collection(name).stream()}
        for name in ("contact_records", "record_enrichment", "viewed_records", "enrichment_targets")
    }
    return {"contacts": contacts, "notes": notes, "docs": docs}


def build_plan(local: dict, existing: dict, include_test: bool) -> dict:
    """Pure function: returns writes to make and everything skipped, with reasons."""
    plan = {"writes": [], "skipped": []}  # writes: (collection, doc_id|None, payload)

    def skip(kind, label, reason):
        plan["skipped"].append((kind, label, reason))

    # contacts -> map local sqlite id to the Firestore doc id its links should use
    id_map = {}
    seen_keys = dict(existing["contacts"])
    for c in local["contacts"]:
        label = f"{c.get('first_name') or ''} {c.get('last_name') or ''} <{c.get('email') or 'no email'}>".strip()
        key = contact_key(c)
        if c.get("is_test_data") and not include_test:
            skip("contact", label, "flagged is_test_data")
            continue
        if key in seen_keys:
            id_map[str(c["id"])] = seen_keys[key]
            skip("contact", label, f"already in Firestore as {seen_keys[key]}")
            continue
        doc_id = f"local-{c['id']}"
        seen_keys[key] = doc_id
        id_map[str(c["id"])] = doc_id
        payload = {k: (c.get(k) or "") for k in CONTACT_FIELDS}
        payload.update(
            is_test_data=bool(c.get("is_test_data")),
            created_at=ts(c.get("created_at")),
            updated_at=ts(c.get("updated_at")),
        )
        plan["writes"].append(("contacts", doc_id, payload))

    have = existing["docs"]
    for link in local["contact_records"]:
        fs_contact = id_map.get(str(link["contact_id"]))
        label = f"contact {link['contact_id']} -> {link['record_type']}:{link['record_id']}"
        if fs_contact is None:
            skip("link", label, "its contact was skipped")
            continue
        doc_id = f"{fs_contact}_{link['record_type']}_{link['record_id']}"
        if doc_id in have.get("contact_records", set()):
            skip("link", label, "already in Firestore")
            continue
        have.setdefault("contact_records", set()).add(doc_id)
        plan["writes"].append((
            "contact_records", doc_id,
            {"contact_id": fs_contact, "record_type": link["record_type"], "record_id": str(link["record_id"]),
             "linked_at": ts(link.get("linked_at"))},
        ))

    for e in local["record_enrichment"]:
        doc_id = f"{e['record_type']}_{e['record_id']}"
        if doc_id in have.get("record_enrichment", set()):
            skip("enrichment", doc_id, "already in Firestore (not overwritten)")
            continue
        payload = {k: (e.get(k) or "") for k in ENRICHMENT_FIELDS}
        payload.update(record_type=e["record_type"], record_id=str(e["record_id"]),
                       created_at=ts(e.get("created_at")), updated_at=ts(e.get("updated_at")))
        plan["writes"].append(("record_enrichment", doc_id, payload))

    seen_notes = set(existing["notes"])
    for n in local["notes"]:
        text = (n["note_text"] or "").strip()
        label = f"{n['record_type']}:{n['record_id']} \"{text[:50]}\""
        if text.lower().startswith("test note") and not include_test:
            skip("note", label, "looks like a test note")
            continue
        key = (n["record_type"], str(n["record_id"]), text)
        if key in seen_notes:
            skip("note", label, "already in Firestore")
            continue
        seen_notes.add(key)
        plan["writes"].append((
            "notes", None,
            {"record_type": n["record_type"], "record_id": str(n["record_id"]), "note_text": text,
             "created_at": ts(n.get("created_at"))},
        ))

    for coll, rows, ts_field in (
        ("viewed_records", local["viewed_records"], "viewed_at"),
        ("enrichment_targets", local["enrichment_targets"], "added_at"),
    ):
        for r in rows:
            doc_id = f"{r['record_type']}_{r['record_id']}"
            if doc_id in have.get(coll, set()):
                skip(coll, doc_id, "already in Firestore")
                continue
            plan["writes"].append((
                coll, doc_id,
                {"record_type": r["record_type"], "record_id": str(r["record_id"]), ts_field: ts(r.get(ts_field))},
            ))
    return plan


def apply_plan(client, writes):
    batch, n = client.batch(), 0
    for coll, doc_id, payload in writes:
        ref = client.collection(coll).document(doc_id) if doc_id else client.collection(coll).document()
        batch.create(ref, payload)  # create() fails if the doc exists: never overwrites
        n += 1
        if n % BATCH_LIMIT == 0:
            batch.commit()
            batch = client.batch()
    batch.commit()


def summarize(plan: dict, local: dict, verbose: bool):
    from collections import Counter

    print("\nLocal rows read:", {k: len(v) for k, v in local.items()})
    print("Would write:    ", dict(Counter(w[0] for w in plan["writes"])) or "nothing")
    print("Skipped:        ", dict(Counter(s[0] for s in plan["skipped"])) or "nothing")
    for kind, label, reason in plan["skipped"]:
        if verbose or kind in ("note", "enrichment") or "test" in reason:
            print(f"  skip {kind}: {label}  ({reason})")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--project", help="GCP project id (e.g. quantum-513214)")
    ap.add_argument("--database", default="(default)", help="Firestore database name")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="SQLite file to read")
    ap.add_argument("--apply", action="store_true", help="actually write to Firestore (default is a dry run)")
    ap.add_argument("--offline", action="store_true", help="don't contact Firestore; plan against an empty one")
    ap.add_argument("--include-test", action="store_true", help="also migrate is_test_data rows and 'Test note' notes")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every skipped row")
    args = ap.parse_args()

    if args.apply and args.offline:
        sys.exit("--apply and --offline can't be combined: apply needs to see what is already in Firestore.")
    if not args.offline and not args.project:
        sys.exit("--project is required unless --offline is given.")

    local = read_local(args.db)
    client = None
    if not args.offline:
        try:
            from google.cloud import firestore
        except ImportError:
            sys.exit("Install the client first: pip install google-cloud-firestore")
        client = firestore.Client(project=args.project, database=args.database)
        print(f"Connected to Firestore project={args.project} database={args.database}")
    else:
        print("OFFLINE: not contacting Firestore; assuming it is empty.")

    plan = build_plan(local, read_existing(client), args.include_test)
    summarize(plan, local, args.verbose)

    if not args.apply:
        print("\nDry run only. Nothing was written. Re-run with --apply to write.")
        return
    apply_plan(client, plan["writes"])
    print(f"\nWrote {len(plan['writes'])} documents.")


if __name__ == "__main__":
    main()
