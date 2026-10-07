"""Score reconciliation results against the answer key.

Run `py evaluate.py` to score the rule-based checks. In Step 5 the same
score() function grades the AI agent, so the two can be compared directly.
"""

import argparse

import pandas as pd

from checks import DATA_DIR, load_rates, run_checks


def score(predictions: pd.DataFrame, answer_key: pd.DataFrame) -> dict:
    """predictions needs columns txn_id, flagged, error_type."""
    merged = predictions.merge(answer_key, on="txn_id", how="left", suffixes=("", "_true"))
    has_error = merged["error_type_true"].notna()

    caught = merged[has_error & merged["flagged"]]
    missed = merged[has_error & ~merged["flagged"]]
    false_alarms = merged[~has_error & merged["flagged"]]
    wrong_label = caught[caught["error_type"] != caught["error_type_true"]]

    print(f"Planted errors caught:  {len(caught)} / {has_error.sum()}")
    print(f"  ...with correct type: {len(caught) - len(wrong_label)} / {len(caught)}")
    print(f"Missed errors:          {len(missed)}")
    print(f"False alarms:           {len(false_alarms)} (clean transactions flagged)")

    print("\nBy error type (caught / planted):")
    for error_type, group in merged[has_error].groupby("error_type_true"):
        print(f"  {error_type:<20} {group['flagged'].sum()} / {len(group)}")

    for label, rows in [("Missed", missed), ("Wrong type", wrong_label), ("False alarms", false_alarms)]:
        if len(rows):
            print(f"\n{label}:")
            print(rows[["txn_id", "error_type", "error_type_true", "reason"]].to_string(index=False))

    return {
        "caught": len(caught),
        "planted": int(has_error.sum()),
        "correct_type": len(caught) - len(wrong_label),
        "missed": len(missed),
        "false_alarms": len(false_alarms),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score reconciliation results against the answer key.")
    parser.add_argument("--agent", help="path to an agent_predictions CSV from agent.py (default: score the rule-based checks)")
    args = parser.parse_args()

    answer_key = pd.read_csv(DATA_DIR / "answer_key.csv")
    if args.agent:
        predictions = pd.read_csv(args.agent)
        print(f"AI agent ({args.agent}, {len(predictions)} transactions)\n")
        score(predictions, answer_key)
        return

    transactions = pd.read_csv(DATA_DIR / "transactions.csv")
    predictions = run_checks(transactions, load_rates())

    print("Rule-based checks\n")
    score(predictions, answer_key)

    print("\nSample of flagged transactions:")
    print(predictions[predictions["flagged"]].head(6)[["txn_id", "error_type", "reason"]].to_string(index=False))


if __name__ == "__main__":
    main()
