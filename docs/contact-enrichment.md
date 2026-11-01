# Automated contact enrichment

A scheduled background job that finds contacts for the highest-value offices
and companies. It is **separate from the award-data refresh** (see
`data-refresh.md`): different job, different schedule, different service
account, and it never touches the award data. Internal document; it does not
name any source of the award data.

## What it does

For each target, most valuable first (offices and companies ranked together by
dollars):

1. **Search.** One model call through OpenRouter with the web search tool
   switched on. The prompt requires searching for every answer and forbids
   answering from memory.
2. **Proof.** The model must return, per person, the page it found them on and
   the exact text it copied from that page. The job then **downloads that page
   itself** and looks for the text. A claim survives only if:
   - the page was one the search actually returned,
   - the page can be fetched safely,
   - the quoted text is really on the page, and
   - the person's first and last name are in the quote.
   Each detail (email, phone, title) is kept only if it appears in the quote;
   otherwise that detail is dropped. If no email, phone or title is supported,
   **nothing is saved**.
3. **Automatic checks** (a guide for the reviewer, not proof): is the email in
   the quote, does the email domain match the company (or end in .gov/.mil for
   an office), does the page's domain match, is the mailbox personal rather
   than `info@`/`sales@`. These set a confidence flag: High / Medium / Low.
4. **Save as unverified.** The contact is stored in the contacts table (keyed
   by UEI and CAGE for companies; linked to the office for offices) with
   `verification_status = unverified`, the confidence flag, and the evidence
   (page address, quoted text, check results).
5. **Log.** One line per target (to the job log and the `enrichment_log`
   table/collection): what was searched, how many saved, cost, why anything
   was rejected. The same log stops the job re-searching a target for 90 days.

Nothing is ever verified automatically.

## Who sees what

- **Clients see only verified contacts.** Unverified and rejected contacts are
  filtered out of every page (office pages, company pages, record pages, the
  contact page itself). Contacts entered by hand before this feature have no
  status and count as verified.
- The team reviews the queue at **Review** (top bar, edit login only): Verify
  makes a contact visible to clients; Reject hides it for good and stops the
  job proposing the same person again. The view-only login gets 403 on the page
  and on the buttons and does not see the link.
- **Sources are never shown.** The page address and quoted text are kept in the
  database but the app does not display them (the same switch that hides every
  other source field). For review, either read them in the Firestore console,
  or run an internal copy of the app with `QXC_SHOW_SOURCES=1`, which adds the
  page and quote to the Review table. Do not give clients an edit login: the
  edit role is the team role.
- **No contact export exists.** The existing CSV export contains award data and
  the company phone from the award record only. A contacts export stays unbuilt
  until contact quality is good.

## Running it

```bash
python app/enrich_job.py --dry-run                                  # who it would work on; free, no model call
python app/enrich_job.py --no-save --max-usd 0.25 --max-targets 3   # trial: real model, every check, saves and logs nothing
python app/enrich_job.py                                            # the real run (what the schedule runs)
```

Environment (Secret Manager on the hosted job, never the repo):

| Variable | Meaning |
|---|---|
| `OPENROUTER_API_KEY` | the OpenRouter key |
| `QXC_ASSISTANT_MODEL` | model id; start with `anthropic/claude-haiku-5.5` |
| `QXC_ENRICH_MAX_USD` | spending cap per run (default 2) |
| `QXC_ENRICH_MAX_TARGETS` | targets per run (default 40) |
| `QXC_ENRICH_PRICE_*`, `QXC_ENRICH_MAX_SEARCHES` | cost model used only when OpenRouter does not report a cost |

Controls: `--max-usd` is a hard cap checked before every target (a call can
overshoot by at most one target's cost, and the job refuses to start a target
unless `--reserve-usd` is left); `--max-minutes`; `--recheck-days`;
`--min-existing` (skip targets that already have enough verified contacts);
`--only office|company`.

## Safety design

- **The model's memory is never evidence.** Only text the job itself finds on a
  downloaded page counts. A model that reports zero searches has its answer
  discarded.
- **The model controls the URLs, so every fetch is treated as hostile.** Only
  plain http(s) on ports 80/443; the host must resolve and **every** address
  must be public (no localhost, private ranges, link-local, or the cloud
  metadata server); no embedded credentials; redirects are followed by hand
  (at most 3), re-checking each hop; text/HTML only, 1.5 MB cap, 15 s timeout.
  Residual risk: the host is resolved once to check and again to connect, so a
  DNS answer that changes in between could slip through; the job's service
  account is deliberately narrow (Firestore and one secret) to limit what that
  could reach.
- **Untrusted text.** Search results and page text may contain instructions;
  the prompt says to ignore them, the model has no tools except search, and
  nothing it returns is acted on except through the proof rules above.
- **Cost.** Hard cap per run, per-target search limit, no retries on a failed
  call, one JSON log line per target.
- **Duplicates.** A person already on file in any status (including rejected)
  is skipped, matched by email or by name plus organization.

## What has and has not been verified

Tested with a simulated model and simulated web pages, on both the local
database and a Firestore stand-in (84 checks): fabricated quotes, pages the
search did not return, unreachable pages, private/metadata/redirect URLs,
unsupported details, duplicates, spending cap, log memory, no-save trial,
client invisibility, review/verify/reject, and the view-only role.

**Not yet run against the real OpenRouter service.** The request shape follows
OpenRouter's documentation for its `openrouter:web_search` tool, but the
documentation does not fully specify the response (citation fields, whether a
cost is reported alongside server tools, how it combines with other tools).
The code accepts the documented shapes and falls back to a token-and-search
cost estimate. Do the `--no-save` trial first and read the log: it shows what
the model returned, what each check decided, and the cost per target. Expect
to tune the prompt and the match rules from that, and treat the first
contacts as a quality test, not a release.

## Hosting

`deploy/terraform/jobs.tf.example` sketches the Cloud Run job (same image as
the app, command `python /app/app/enrich_job.py`), its own service account
(Firestore + the OpenRouter secret only), and a weekly Cloud Scheduler cron.
Unvalidated; for the infrastructure owner to adapt and apply.
