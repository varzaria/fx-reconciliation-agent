"""Score reconciliation results against the answer key.

  py evaluate.py                                   # rule-based checks, standard data
  py evaluate.py --data data_messy                 # rule-based checks, messy data
  py evaluate.py --agent results\agent_predictions-....csv [--data data_messy]
"""

import argparse
from pathlib import Path

import pandas as pd

from checks import load_rates, run_checks

ROOT = Path(__file__).parent


def score(predictions: pd.DataFrame, answer_key: pd.DataFrame) -> dict:
    """predictions needs columns txn_id, flagged, error_type.

    Answer-key rows with a blank error_type are tricky cases that are NOT errors
    (e.g. approved exceptions); flagging them counts as a false alarm.
    """
    merged = predictions.merge(answer_key, on="txn_id", how="left", suffixes=("", "_true"))
    has_error = merged["error_type_true"].notna()
    flagged = merged["flagged"].astype(bool)

    caught = merged[has_error & flagged]
    missed = merged[has_error & ~flagged]
    false_alarms = merged[~has_error & flagged]
    wrong_label = caught[caught["error_type"] != caught["error_type_true"]]
    correct = (flagged == has_error) & (~has_error | (merged["error_type"] == merged["error_type_true"]))

    print(f"Accuracy:               {correct.sum()} / {len(merged)} transactions handled correctly")
    print(f"Planted errors caught:  {len(caught)} / {has_error.sum()}")
    print(f"  ...with correct type: {len(caught) - len(wrong_label)} / {len(caught)}")
    print(f"Missed errors:          {len(missed)}")
    print(f"False alarms:           {len(false_alarms)} (non-errors flagged)")

    if "case" in merged.columns:
        print("\nBy case (handled correctly / total):")
        cases = merged["case"].fillna("clean")
        for case in ["clean", "standard", "near_duplicate", "approved_exception", "false_contract_claim"]:
            mask = cases == case
            if mask.any():
                print(f"  {case:<22} {correct[mask].sum()} / {mask.sum()}")
    else:
        print("\nBy error type (caught / planted):")
        for error_type, group in merged[has_error].groupby("error_type_true"):
            print(f"  {error_type:<20} {group['flagged'].sum()} / {len(group)}")

    for label, rows in [("Missed", missed), ("Wrong type", wrong_label), ("False alarms", false_alarms)]:
        if len(rows):
            print(f"\n{label}:")
            print(rows[["txn_id", "error_type", "error_type_true", "reason"]].to_string(index=False))

    return {
        "transactions": len(merged),
        "correct": int(correct.sum()),
        "caught": len(caught),
        "planted": int(has_error.sum()),
        "correct_type": len(caught) - len(wrong_label),
        "missed": len(missed),
        "false_alarms": len(false_alarms),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Score reconciliation results against the answer key.")
    parser.add_argument("--agent", help="path to an agent_predictions CSV from agent.py (default: score the rule-based checks)")
    parser.add_argument("--data", default="data", help="dataset folder: data (default) or data_messy")
    args = parser.parse_args()

    data_dir = ROOT / args.data
    answer_key = pd.read_csv(data_dir / "answer_key.csv")
    if args.agent:
        predictions = pd.read_csv(args.agent)
        print(f"AI agent ({args.agent}, {len(predictions)} transactions)\n")
        score(predictions, answer_key)
        return

    transactions = pd.read_csv(data_dir / "transactions.csv")
    predictions = run_checks(transactions, load_rates(data_dir / "fx_rates.csv"))

    print(f"Rule-based checks on {args.data}/\n")
    score(predictions, answer_key)


if __name__ == "__main__":
    main()
