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
    df = df.dropna(subset=["award_id_piid"])
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


def load_subawards(matched_award_ids: set) -> pd.DataFrame:
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
    df["matched_award_id_piid"] = df["prime_award_piid"].where(
        df["prime_award_piid"].isin(matched_award_ids)
    )
    df["period_of_performance_start_date"] = pd.to_datetime(
        df["period_of_performance_start_date"], errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    df.insert(0, "subaward_id", [f"sub-{i}" for i in range(len(df))])
    return df


def main():
    DB_PATH.parent.mkdir(exist_ok=True)
    awards = load_awards()
    subawards = load_subawards(set(awards["award_id_piid"]))

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

    matched = (subawards["matched_award_id_piid"].notna()).sum()
    print(f"Loaded {len(awards)} awards ({awards['transaction_count'].sum()} transactions)")
    print(f"Loaded {len(subawards)} subawards ({matched} matched to a direct award in our dataset)")
    print(f"Wrote {DB_PATH}")


if __name__ == "__main__":
    main()
