"""
Freezes the live Flask app into static HTML for a temporary Firebase Hosting
demo link. Firebase Hosting only serves static files, so this snapshot loses:

  - Live search/filtering on Search Records (the form is still there and
    submitting it won't error, but query-string filters are ignored by a
    static host - you'll just see the same unfiltered page).
  - The "Mark viewed" write API (nothing to receive the request), so those
    buttons are visually disabled in the frozen output.

Every office and awardee detail page is still frozen individually and fully
click-through, since those are real distinct paths, not query strings.

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
    "live search, filtering, and “mark viewed” require running the app "
    "locally.</div>"
)
DEMO_STYLE = "<style>.viewed-toggle{pointer-events:none;opacity:.5;}</style></head>"


def freeze(client, path: str, out_file: Path) -> bool:
    resp = client.get(path)
    if resp.status_code != 200:
        print(f"  SKIP {path} -> {resp.status_code}")
        return False
    html = resp.data.decode("utf-8")
    html = html.replace("<body>", "<body>\n" + DEMO_BANNER, 1)
    html = html.replace("</head>", DEMO_STYLE, 1)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")
    return True


def main():
    if OUT.exists():
        shutil.rmtree(OUT)
    OUT.mkdir(parents=True)

    client = appmodule.app.test_client()

    pages = [
        ("/", "index.html"),
        ("/records", "records.html"),
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

    print(f"Freezing {len(pages)} pages...")
    ok = 0
    for path, rel in pages:
        if freeze(client, path, OUT / rel):
            ok += 1

    shutil.copytree(ROOT / "app" / "static", OUT / "static")

    print(f"Done: {ok}/{len(pages)} pages written to {OUT}")


if __name__ == "__main__":
    main()
