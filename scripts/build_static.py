"""
Freezes the live Flask app into static HTML for a temporary Firebase Hosting
demo link. Firebase Hosting only serves static files, so this snapshot loses:

  - The "Mark viewed" write API and the notes add/delete forms (nothing to
    receive the request), so those controls are visually disabled in the
    frozen output. Any notes already added before the freeze are still shown.
  - The Awarding/Funding role toggle on Search Records and Home only works
    for whichever role got frozen (awarding) - switching roles re-serves the
    same frozen page rather than the other role's data, since query strings
    don't affect which static file a host serves.

Search Records and Home's Agency/Department/Sort/Company/Search controls DO
work, though: Search Records is frozen with every record embedded
(limit=all) and a `window.QXC_STATIC` flag that switches app.js from
"submit a form to the server" to "filter the already-embedded rows/cards in
the browser" - see initRecordsFilter()/initHomeFilter() in app/static/app.js.

Every office, awardee, individual award/subaward, and department detail page
is frozen individually and fully click-through, since those are real
distinct paths, not query strings.

Usage: python scripts/build_static.py
Output: public/ (Firebase Hosting's default publish directory)
"""
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))

import app as appmodule  # noqa: E402

OUT = ROOT / "public"

DEMO_BANNER = (
    '<div style="background:#0a1930;color:#cfe0ee;font-size:12px;'
    'padding:9px 20px;text-align:center;font-family:Inter,-apple-system,'
    'Segoe UI,sans-serif;">Static preview snapshot for review &mdash; '
    "“mark viewed” and notes are read-only here (no backend to save to); "
    "run the app locally for those.</div>"
)
DEMO_STYLE = (
    "<style>.viewed-toggle,.note-form textarea,.note-form button,.note-delete"
    "{pointer-events:none;opacity:.5;}</style>"
    "<script>window.QXC_STATIC = true;</script></head>"
)


def freeze(client, path: str, out_file: Path) -> bool:
    resp = client.get(path)
    if resp.status_code != 200:
        print(f"  SKIP {path} -> {resp.status_code}")
        return False
    html = resp.data.decode("utf-8")
    html = html.replace("<body>", "<body>\n" + DEMO_BANNER, 1)
    html = html.replace("</head>", DEMO_STYLE, 1)
    html = html.replace(
        'placeholder="Add a note&hellip;"',
        'placeholder="Notes are read-only in this preview - run the app locally to add one" readonly',
    )
    html = html.replace(">Add Note<", ">Disabled in Preview<")
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    return True


def main():
    # Overwrite in place rather than rmtree - OneDrive keeps a sync lock on
    # this folder's subdirectories right after a big batch of file changes,
    # which makes a full delete-then-recreate unreliable here.
    OUT.mkdir(parents=True, exist_ok=True)

    client = appmodule.app.test_client()

    pages = [
        ("/", "index.html"),
        # limit=all embeds every record (not just the top 250 by amount) so
        # the client-side filter script in app.js has the full dataset to
        # work with once it's running in the browser.
        ("/records?limit=all", "records.html"),
        ("/awardees", "awardees.html"),
    ]

    for role in appmodule.ROLES:
        pages.append((f"/offices/{role}", f"offices/{role}.html"))
        rows, _, _ = appmodule.build_offices_rows(role)
        for r in rows:
            pages.append((f"/offices/{role}/{r['url_code']}", f"offices/{role}/{r['url_code']}.html"))

    awardee_rows, _, _ = appmodule.build_awardees_rows()
    for r in awardee_rows:
        pages.append((f"/awardees/{r['url_key']}", f"awardees/{r['url_key']}.html"))

    # Individual award/subaward detail pages - one per row in RECORDS.
    for _, row in appmodule.RECORDS.iterrows():
        rtype = row["record_type"]
        token = appmodule.encode_key(str(row["record_id"]))
        pages.append((f"/record/{rtype}/{token}", f"record/{rtype}/{token}.html"))

    # Department rollup pages - one per (role, agency, sub-agency) combo that
    # actually has data.
    for role in appmodule.ROLES:
        agency_c = f"{role}_agency_name"
        sub_agency_c = f"{role}_sub_agency_name"
        combos = appmodule.RECORDS[[agency_c, sub_agency_c]].dropna().drop_duplicates()
        for _, combo in combos.iterrows():
            agency, department = combo[agency_c], combo[sub_agency_c]
            if not agency or not department:
                continue
            a_token = appmodule.encode_key(str(agency))
            d_token = appmodule.encode_key(str(department))
            pages.append(
                (f"/department/{role}/{a_token}/{d_token}", f"department/{role}/{a_token}/{d_token}.html")
            )

    print(f"Freezing {len(pages)} pages...")
    ok = 0
    for path, rel in pages:
        if freeze(client, path, OUT / rel):
            ok += 1

    shutil.copytree(ROOT / "app" / "static", OUT / "static", dirs_exist_ok=True)

    print(f"Done: {ok}/{len(pages)} pages written to {OUT}")


if __name__ == "__main__":
    main()
