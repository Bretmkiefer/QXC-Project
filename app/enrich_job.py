"""
Scheduled job: find contacts for the highest-value offices and companies.
See docs/contact-enrichment.md. Separate from the award-data refresh.

    python app/enrich_job.py --dry-run                 # list who it would work on; no model calls, no cost
    python app/enrich_job.py --max-usd 5 --max-targets 40

Needs, from the environment (Secret Manager on the hosted job):
    OPENROUTER_API_KEY     never in the repo
    QXC_ASSISTANT_MODEL    the OpenRouter model id (start with anthropic/claude-haiku-5.5)
Storage follows the app: Firestore on Cloud Run (QXC_USE_FIRESTORE=1), else the local SQLite file.

What it does for each target, highest value first:
  1. asks the model to find people using web search (never from memory),
  2. downloads each page the model cites and checks the model's quoted text is really there,
  3. keeps only the details that text supports, runs automatic checks, and
  4. saves the contact as UNVERIFIED. Clients cannot see it until a person verifies it.
It stops at the spending cap, and writes one log line per target.
"""
import argparse
import datetime
import json
import os
import sys
import time
import uuid

import app as qxc
import enrichment as en


def log(event: str, **fields):
    print(json.dumps({"event": event, **fields}, default=str), flush=True)


def build_targets(kinds=("office", "company")) -> list:
    """Offices and companies together, most valuable first."""
    targets = []
    if "office" in kinds:
        rows, _, _ = qxc.build_offices_rows("awarding")
        for o in rows:
            code, name = str(o.get("code") or ""), str(o.get("name") or "")
            if code and name and code != "nan":
                targets.append({"key": f"office:{code}", "kind": "office", "code": code, "name": name,
                                "agency": str(o.get("agency") or ""),
                                "value": float(o["direct_total"] or 0) + float(o["subaward_total"] or 0)})
    if "company" in kinds:
        cage = {}
        for uei, c in zip(qxc.AWARDS["recipient_uei"], qxc.AWARDS["recipient_cage_code"]):
            if uei and c:
                cage[uei] = c
        rows = qxc.build_awardees_rows()[0]
        for r in rows:
            ek, name = str(r["entity_key"]), str(r.get("display_name") or "")
            uei = ek[4:] if ek.startswith("uei:") else ""
            if name:
                targets.append({"key": f"company:{ek}", "kind": "company", "entity_key": ek, "name": name, "uei": uei,
                                "cage": cage.get(uei, ""), "value": float(r.get("combined_total") or 0)})
    return sorted(targets, key=lambda t: t["value"], reverse=True)


def existing_index() -> dict:
    """What is already stored, in every status (rejected ones too, so a person's
    'no' is not asked again)."""
    emails, names = set(), set()
    for c in qxc.get_all_contacts(include_unverified=True):
        if c.get("email"):
            emails.add(str(c["email"]).lower())
        who = f"{c.get('first_name', '')} {c.get('last_name', '')}".strip()
        for org in (c.get("organization_name"), c.get("agency_office")):
            if who and org:
                names.add(f"{who}|{org}".lower())
    return {"emails": emails, "names": names}


def verified_count(t: dict) -> int:
    if t["kind"] == "office":
        return len(qxc.get_contacts_for_office(t["code"]))
    return len(qxc.get_contacts_for_entity(t["entity_key"]))


def pick(targets, recent: set, min_existing: int, min_value: float):
    for t in targets:
        if t["value"] < min_value or t["key"] in recent:
            continue
        if verified_count(t) >= min_existing:
            continue
        yield t


def run(argv=None, transport=None, fetch=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-usd", type=float, default=float(os.environ.get("QXC_ENRICH_MAX_USD", 2.0)), help="spending cap for this run")
    ap.add_argument("--max-targets", type=int, default=int(os.environ.get("QXC_ENRICH_MAX_TARGETS", 40)))
    ap.add_argument("--max-minutes", type=int, default=50)
    ap.add_argument("--reserve-usd", type=float, default=0.04, help="don't start a target with less than this left")
    ap.add_argument("--recheck-days", type=int, default=90, help="skip targets worked on within this many days")
    ap.add_argument("--min-existing", type=int, default=2, help="skip targets that already have this many verified contacts")
    ap.add_argument("--min-value", type=float, default=0.0)
    ap.add_argument("--only", choices=["office", "company"], help="work on one kind only")
    ap.add_argument("--dry-run", action="store_true", help="list the targets and stop: no model calls, no cost")
    ap.add_argument("--no-save", action="store_true",
                    help="trial run: call the model and run every check but save and log NOTHING (costs real money, "
                         "so use a small --max-usd and --max-targets). Shows what would be kept and why the rest was rejected")
    args = ap.parse_args(argv)

    run_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S-") + uuid.uuid4().hex[:6]
    model, key = os.environ.get("QXC_ASSISTANT_MODEL", ""), os.environ.get("OPENROUTER_API_KEY", "")
    kinds = (args.only,) if args.only else ("office", "company")
    targets = list(pick(build_targets(kinds), qxc.recent_enrichment_targets(args.recheck_days), args.min_existing, args.min_value))
    log("start", run_id=run_id, model=model or None, max_usd=args.max_usd, candidates=len(targets), dry_run=args.dry_run,
        store="firestore" if qxc.USE_FIRESTORE else "sqlite")

    if args.dry_run:
        for t in targets[: args.max_targets]:
            log("would_search", target=t["key"], name=t["name"], kind=t["kind"], value=round(t["value"]))
        return 0
    if not (model and key) and transport is None:
        log("config_error", reason="OPENROUTER_API_KEY and QXC_ASSISTANT_MODEL must both be set")
        return 2

    budget, existing, started = en.Budget(args.max_usd), existing_index(), time.time()
    done = saved_total = 0
    for t in targets:
        if done >= args.max_targets:
            log("stop", reason="max_targets")
            break
        if time.time() - started > args.max_minutes * 60:
            log("stop", reason="max_minutes")
            break
        if not budget.allows(args.reserve_usd):
            log("stop", reason="spending_cap", spent_usd=round(budget.spent, 4), cap_usd=budget.max_usd)
            break

        def save(fields, evidence, _t=t):
            if args.no_save:
                log("would_save", target=_t["key"], name=f"{fields['first_name']} {fields['last_name']}", title=fields["title"],
                    confidence=fields["confidence"], checks=evidence["checks"], page=evidence["url"])
                return "not-saved"
            cid = qxc.create_enrichment_contact(fields, evidence)
            if _t["kind"] == "office":
                qxc.link_contact_to_record(cid, "office", _t["code"])
            return cid

        kw = {"fetch": fetch} if fetch else {}
        res = en.enrich_target(t, model, key, existing, save, run_id, transport=transport, **kw)
        budget.spend(res["cost"])
        done += 1
        saved_total += len(res["saved"])
        if not args.no_save:
          qxc.log_enrichment({"run_id": run_id, "target_key": t["key"], "kind": t["kind"], "target_name": t["name"], "value": t["value"],
                              "status": res["status"], "saved": len(res["saved"]), "cost_usd": res["cost"], "detail": res["detail"]})
        log("target", target=t["key"], name=t["name"], status=res["status"], saved=len(res["saved"]),
            cost_usd=round(res["cost"], 5), spent_usd=round(budget.spent, 4), detail=res["detail"])
    log("finish", run_id=run_id, targets=done, contacts_saved=saved_total, spent_usd=round(budget.spent, 4), cap_usd=budget.max_usd)
    return 0


if __name__ == "__main__":
    sys.exit(run())
