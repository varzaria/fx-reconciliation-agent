"""Rule-based reconciliation checks.

Each row check takes one transaction (a pandas row or dict) plus the reference
rates, and returns {"passed": bool, "reason": str}. These functions become the
agent's tools in Step 4, so the reasons are written for a human reviewer.
"""

from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).parent / "data"

RATE_TOLERANCE = 0.02     # max relative difference from the reference rate (2%)
AMOUNT_TOLERANCE = 0.01   # max absolute difference in EUR, to allow for rounding


def load_rates(path: Path = DATA_DIR / "fx_rates.csv") -> dict:
    """Return reference rates as {date: {currency: rate}}, e.g. rates["2025-01-07"]["USD"]."""
    rates = {}
    for r in pd.read_csv(path).itertuples():
        rates.setdefault(r.date, {})[r.currency] = r.rate
    return rates


def result(passed: bool, reason: str = "") -> dict:
    return {"passed": passed, "reason": reason}


def relative_diff(a: float, b: float) -> float:
    """How far a is from b, as a fraction of b (0.03 = 3%)."""
    return abs(a - b) / b


def check_journal_exists(txn, rates) -> dict:
    """Every payment needs a journal reference."""
    # pandas reads an empty cell as NaN, so check for that as well as blank text.
    ref = txn["journal_ref"]
    if pd.isna(ref) or str(ref).strip() == "":
        return result(False, "no journal reference recorded for this payment")
    return result(True)


def check_journal_balance(txn, rates) -> dict:
    """Journal debit and credit must match."""
    diff = abs(txn["journal_debit_eur"] - txn["journal_credit_eur"])
    if diff > AMOUNT_TOLERANCE:
        return result(False, f"journal is unbalanced: debit {txn['journal_debit_eur']:.2f} vs credit {txn['journal_credit_eur']:.2f} EUR (difference {diff:.2f})")
    return result(True)


def check_currency(txn, rates) -> dict:
    """The rate used should not belong to a different currency."""
    for ccy, rate in rates[txn["date"]].items():
        if ccy != txn["currency"] and relative_diff(txn["fx_rate"], rate) <= RATE_TOLERANCE:
            return result(False, f"transaction is in {txn['currency']} but the rate used ({txn['fx_rate']}) matches the {ccy} rate ({rate}) for {txn['date']}")
    return result(True)


def check_fx_rate(txn, rates) -> dict:
    """The rate used should be close to the reference rate for that day."""
    reference = rates[txn["date"]][txn["currency"]]
    diff = relative_diff(txn["fx_rate"], reference)
    if diff > RATE_TOLERANCE:
        return result(False, f"rate used {txn['fx_rate']} differs from the {txn['date']} reference rate {reference} by {diff:.1%}")
    return result(True)


def check_conversion(txn, rates) -> dict:
    """amount x fx_rate should equal amount_eur, within rounding."""
    expected = round(txn["amount"] * txn["fx_rate"], 2)
    diff = abs(expected - txn["amount_eur"])
    if diff > AMOUNT_TOLERANCE:
        return result(False, f"amount_eur is {txn['amount_eur']:.2f} but amount x rate = {expected:.2f} (off by {diff:.2f} EUR)")
    return result(True)


def find_duplicates(df: pd.DataFrame) -> dict:
    """Return {duplicate txn_id: original txn_id} for repeat entries.

    A duplicate has the same date, currency, amount and journal reference as an
    earlier transaction. Only the later copy is flagged; the original is kept.
    """
    key = ["date", "currency", "amount", "journal_ref"]
    ordered = df.sort_values("txn_id")
    first_seen = ordered.drop_duplicates(subset=key, keep="first").set_index(key)["txn_id"]
    repeats = ordered[ordered.duplicated(subset=key, keep="first")]
    return {row.txn_id: first_seen[tuple(row[k] for k in key)] for _, row in repeats.iterrows()}


# Order matters: the first failing check sets the error type. A currency mismatch
# also fails check_fx_rate, so check_currency must run before it.
ROW_CHECKS = [
    ("missing_journal", check_journal_exists),
    ("unbalanced_journal", check_journal_balance),
    ("currency_mismatch", check_currency),
    ("wrong_fx_rate", check_fx_rate),
    ("conversion_mismatch", check_conversion),
]


def run_checks(df: pd.DataFrame, rates: dict) -> pd.DataFrame:
    """Run every check on every transaction. Returns one row per transaction."""
    duplicates = find_duplicates(df)
    rows = []
    for _, txn in df.iterrows():
        row = {"txn_id": txn["txn_id"], "flagged": False, "error_type": "", "reason": ""}
        if txn["txn_id"] in duplicates:
            row.update(flagged=True, error_type="duplicate", reason=f"repeat of {duplicates[txn['txn_id']]}")
        else:
            for error_type, check in ROW_CHECKS:
                outcome = check(txn, rates)
                if not outcome["passed"]:
                    row.update(flagged=True, error_type=error_type, reason=outcome["reason"])
                    break
        rows.append(row)
    return pd.DataFrame(rows)
