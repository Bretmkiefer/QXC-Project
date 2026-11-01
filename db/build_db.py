"""
Builds db/qxc.db from the raw checkpoint-1 CSVs in data/raw/.

Transactions file is transaction-level (one row per modification/action on an
award), so it's aggregated up to one row per award_id_piid:
  - total_federal_action_obligation is SUMMED across all transactions (this is
    the actual amount obligated; can include negative de-obligations).
  - potential_total_value_of_award / current_total_value_of_award /
    base_and_all_options_value are point-in-time snapshot fields that already
    include prior modifications, so we take the value from the LATEST
    transaction rather than summing it.
  - total_outlayed_amount_for_overall_award is intentionally dropped per
    project instructions (unreliable).

Subawards file is already one row per subaward; loaded close to as-is, with a
matched_award_id_piid column added when the subaward's prime award is present
in our own transactions dataset (expected to be uncommon).
"""
import sqlite3
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TRANSACTIONS_CSV = ROOT / "data" / "raw" / "prime_transactions_checkpoint_1.csv"
SUBAWARDS_CSV = ROOT / "data" / "raw" / "subawards_checkpoint_1.csv"
DB_PATH = ROOT / "db" / "qxc.db"


def assign_award_keys(df: pd.DataFrame) -> pd.Series:
    """One unique key per distinct award.

    An award id (PIID) is not unique across the source: the same id is
    sometimes issued to different companies under different parent contracts
    (about a dozen cases). Rows belong to the same award when they share the
    PIID, the recipient and the parent contract; later modifications of one
    award carry the same three. A row with a blank parent joins the
    recipient's only parent.

    The first award seen (in file order) keeps the plain PIID as its key and
    any further awards get PIID~2, PIID~3, ... This keeps every existing
    record id unchanged, and because the source file only ever grows by
    appending, an award's key never changes later.
    """
    piid = df["award_id_piid"].astype(str)
    uei = df["recipient_uei"].fillna("").astype(str).str.upper().str.strip()
    parent = df["parent_award_id_piid"].fillna("").astype(str).str.strip()
    keys = piid.copy()
    for p, idx in df.groupby(piid, sort=False).groups.items():
        if len(idx) < 2:
            continue
        # a blank parent joins the only non-blank parent seen for that recipient
        parents_by_uei = {}
        for i in idx:
            if parent[i]:
                parents_by_uei.setdefault(uei[i], []).append(parent[i])
        ordinals, assigned = {}, []
        for i in idx:  # idx is in file order
            par = parent[i] or (parents_by_uei.get(uei[i]) or [""])[0]
            ident = (uei[i], par)
            if ident not in ordinals:
                ordinals[ident] = len(ordinals) + 1
            assigned.append(ordinals[ident])
        if len(ordinals) > 1:
            for i, n in zip(idx, assigned):
                keys[i] = p if n == 1 else f"{p}~{n}"
    return keys


def load_awards() -> pd.DataFrame:
    cols = [
        "award_id_piid",
        "parent_award_id_piid",
        "federal_action_obligation",
        "potential_total_value_of_award",
        "current_total_value_of_award",
        "base_and_all_options_value",
        "action_date",
        "period_of_performance_start_date",
        "awarding_agency_name",
        "awarding_sub_agency_name",
        "awarding_office_code",
        "awarding_office_name",
        "funding_agency_name",
        "funding_sub_agency_name",
        "funding_office_code",
        "funding_office_name",
        "recipient_uei",
        "recipient_name",
        "recipient_parent_name",
        "recipient_state_code",
        "recipient_city_name",
        "recipient_phone_number",
        "cage_code",
        "naics_code",
        "naics_description",
        "product_or_service_code",
        "product_or_service_code_description",
        "award_type",
        "type_of_contract_pricing_code",
        "transaction_description",
        "prime_award_base_transaction_description",
    ]
    df = pd.read_csv(TRANSACTIONS_CSV, usecols=cols, low_memory=False)
    df = df.rename(columns={"cage_code": "recipient_cage_code"})
    df["federal_action_obligation"] = pd.to_numeric(
        df["federal_action_obligation"], errors="coerce"
    ).fillna(0.0)
    for c in [
        "potential_total_value_of_award",
        "current_total_value_of_award",
        "base_and_all_options_value",
    ]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["action_date"] = pd.to_datetime(df["action_date"], errors="coerce")
    df["period_of_performance_start_date"] = pd.to_datetime(
        df["period_of_performance_start_date"], errors="coerce"
    )
    df = df.dropna(subset=["award_id_piid"]).reset_index(drop=True)
    # award_id_piid becomes the unique award key (see assign_award_keys);
    # award_piid keeps the real award id for display and exports.
    df["award_piid"] = df["award_id_piid"]
    df["award_id_piid"] = assign_award_keys(df)
    df = df.sort_values("action_date")

    grouped = df.groupby("award_id_piid", as_index=False)

    sums = grouped["federal_action_obligation"].sum().rename(
        columns={"federal_action_obligation": "total_federal_action_obligation"}
    )
    counts = grouped.size().rename(columns={"size": "transaction_count"})
    first_dates = grouped["action_date"].min().rename(
        columns={"action_date": "first_action_date"}
    )
    last_dates = grouped["action_date"].max().rename(
        columns={"action_date": "latest_action_date"}
    )
    # Earliest period-of-performance start seen across an award's
    # transactions - the closest proxy in this data to "date placed in
    # service" (actual install/in-service dates aren't in a procurement
    # dataset; this is when contract performance was scheduled to begin).
    pop_start = grouped["period_of_performance_start_date"].min()
    latest_rows = df.groupby("award_id_piid", as_index=False).last()

    latest_rows = latest_rows.drop(
        columns=["federal_action_obligation", "action_date", "period_of_performance_start_date"]
    )

    awards = (
        latest_rows.merge(sums, on="award_id_piid")
        .merge(counts, on="award_id_piid")
        .merge(first_dates, on="award_id_piid")
        .merge(last_dates, on="award_id_piid")
        .merge(pop_start, on="award_id_piid")
    )
    awards["first_action_date"] = awards["first_action_date"].dt.strftime("%Y-%m-%d")
    awards["latest_action_date"] = awards["latest_action_date"].dt.strftime("%Y-%m-%d")
    awards["period_of_performance_start_date"] = awards["period_of_performance_start_date"].dt.strftime("%Y-%m-%d")
    awards = awards.rename(
        columns={"prime_award_base_transaction_description": "base_transaction_description"}
    )
    return awards


def load_subawards(awards: pd.DataFrame) -> pd.DataFrame:
    cols = [
        "prime_award_piid",
        "prime_award_amount",
        "prime_award_awarding_agency_name",
        "prime_award_awarding_sub_agency_name",
        "prime_award_awarding_office_code",
        "prime_award_awarding_office_name",
        "prime_award_funding_agency_name",
        "prime_award_funding_sub_agency_name",
        "prime_award_funding_office_code",
        "prime_award_funding_office_name",
        "prime_award_period_of_performance_start_date",
        "prime_awardee_uei",
        "prime_awardee_name",
        "prime_award_base_transaction_description",
        "subaward_type",
        "subaward_number",
        "subaward_amount",
        "subaward_action_date",
        "subawardee_uei",
        "subawardee_name",
        "subawardee_state_code",
        "subawardee_city_name",
        "subaward_description",
    ]
    df = pd.read_csv(SUBAWARDS_CSV, usecols=cols, low_memory=False)
    df["subaward_amount"] = pd.to_numeric(df["subaward_amount"], errors="coerce").fillna(0.0)
    df["prime_award_amount"] = pd.to_numeric(df["prime_award_amount"], errors="coerce")
    df = df.rename(
        columns={
            "prime_award_awarding_agency_name": "awarding_agency_name",
            "prime_award_awarding_sub_agency_name": "awarding_sub_agency_name",
            "prime_award_awarding_office_code": "awarding_office_code",
            "prime_award_awarding_office_name": "awarding_office_name",
            "prime_award_funding_agency_name": "funding_agency_name",
            "prime_award_funding_sub_agency_name": "funding_sub_agency_name",
            "prime_award_funding_office_code": "funding_office_code",
            "prime_award_funding_office_name": "funding_office_name",
            "prime_award_base_transaction_description": "prime_award_description",
            "prime_award_period_of_performance_start_date": "period_of_performance_start_date",
        }
    )
    df["matched_award_id_piid"] = match_prime_awards(df, awards)
    df["period_of_performance_start_date"] = pd.to_datetime(
        df["period_of_performance_start_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    # Ids come from the row's position in the file, assigned BEFORE duplicates
    # are dropped, so every kept row keeps the id it always had (notes and
    # links refer to these ids).
    df.insert(0, "subaward_id", [f"sub-{i}" for i in range(len(df))])

    # The same subaward can appear twice in the source (identical prime,
    # subaward number, subawardee, date and amount). Keep the first so its
    # dollars are not counted twice.
    dup_key = ["prime_award_piid", "subaward_number", "subawardee_uei", "subaward_action_date", "subaward_amount"]
    before = len(df)
    df = df.drop_duplicates(subset=dup_key, keep="first")
    if len(df) != before:
        print(f"build_db: dropped {before - len(df)} duplicate subaward rows")
    return df


def match_prime_awards(subs: pd.DataFrame, awards: pd.DataFrame) -> pd.Series:
    """Award key of each subaward's prime award, or None when it is not in
    our direct-award data. Where an award id was reused by several companies,
    the subaward's prime awardee picks the right one."""
    by_piid, by_piid_uei = {}, {}
    for key, piid, uei in zip(awards["award_id_piid"], awards["award_piid"], awards["recipient_uei"].fillna("")):
        by_piid.setdefault(piid, []).append(key)
        by_piid_uei[(piid, str(uei).upper().strip())] = key

    def match(piid, uei):
        hit = by_piid_uei.get((piid, str(uei).upper().strip()) if isinstance(uei, str) else (piid, ""))
        if hit:
            return hit
        keys = by_piid.get(piid)
        return keys[0] if keys and len(keys) == 1 else None  # ambiguous id: don't guess

    return pd.Series([match(p, u) for p, u in zip(subs["prime_award_piid"], subs["prime_awardee_uei"])], index=subs.index)


def main():
    DB_PATH.parent.mkdir(exist_ok=True)
    awards = load_awards()
    subawards = load_subawards(awards)

    with sqlite3.connect(DB_PATH) as conn:
        awards.to_sql("awards", conn, if_exists="replace", index=False)
        subawards.to_sql("subawards", conn, if_exists="replace", index=False)

        for col in ["awarding_office_code", "funding_office_code", "recipient_uei", "recipient_name"]:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_awards_{col} ON awards({col})")
        for col in [
            "awarding_office_code",
            "funding_office_code",
            "subawardee_uei",
            "subawardee_name",
            "matched_award_id_piid",
        ]:
            conn.execute(f"CREATE INDEX IF NOT EXISTS idx_subawards_{col} ON subawards({col})")

        # Not dropped/replaced on rebuild, so read state survives re-running
        # this script against refreshed source CSVs.
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

    matched = (subawards["matched_award_id_piid"].notna()).sum()
    print(f"Loaded {len(awards)} awards ({awards['transaction_count'].sum()} transactions)")
    print(f"Loaded {len(subawards)} subawards ({matched} matched to a direct award in our dataset)")
    print(f"Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
