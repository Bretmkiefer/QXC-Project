# QXC Equipment Intelligence — Checkpoint 1

Local proof-of-concept for Phase 1: a SQLite warehouse + Flask front end over
the Cisco-equipment prime transaction and subaward checkpoint-1 CSVs.

## Setup

```
pip install -r requirements.txt
```

Place the two source CSVs in `data/raw/` (already done for this checkout):

- `prime_transactions_checkpoint_1.csv`
- `subawards_checkpoint_1.csv`

## Build the database

```
python db/build_db.py
```

Writes `db/qxc.db`. Re-run any time the source CSVs change.

## Run the app

```
python app/app.py
```

Then open http://127.0.0.1:5057

## What's in Checkpoint 1

- **Offices** (`/offices/awarding`, `/offices/funding`) — spend listing by
  office, direct-award and subaward dollars shown separately to avoid
  double-counting. Click into an office to see its direct awardees and, nested
  underneath, any subawardees traced to that office (including subawards whose
  prime contract isn't in our direct-award dataset).
- **Awardees** (`/awardees`) — every company that received either a direct
  award or a subaward, with both totals combined (a contract can be counted
  twice by design, per project scope). Click into a company to see the offices
  it worked for directly and, separately, who it subcontracted under.

## Data notes

- `total_outlayed_amount_for_overall_award` is dropped from the transactions
  file per project instructions — it isn't reliable.
- Transaction-level rows are aggregated to one row per award: obligated
  dollars are **summed**, while snapshot fields like potential/current total
  value are taken from the **latest** transaction (they're cumulative, not
  additive).
- Only 2 of ~800 subawards match a prime award already in our direct-award
  dataset, consistent with the low overlap Kyle flagged going in.
