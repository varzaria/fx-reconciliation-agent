"""Generate synthetic cross-currency transactions with planted errors.

Outputs (in data/):
  fx_rates.csv        reference FX rates per day (the "source of truth")
  transactions.csv    transactions the agent will check
  answer_key.csv      which transactions contain a planted error, and what kind

Rates are quoted as EUR per 1 unit of foreign currency, so
amount_eur = amount * fx_rate.
"""

import random
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

SEED = 42
N_TRANSACTIONS = 500
ERROR_RATE = 0.10
START_DATE = date(2025, 1, 6)
N_DAYS = 60

# Approximate base rates (EUR per 1 unit), varied a little each day.
BASE_RATES = {
    "USD": 0.92,
    "GBP": 1.18,
    "CHF": 1.05,
    "PLN": 0.23,
    "RON": 0.20,
    "JPY": 0.0061,
}

ERROR_TYPES = [
    "wrong_fx_rate",        # rate deviates from the reference rate for that day
    "currency_mismatch",    # rate used belongs to a different currency
    "conversion_mismatch",  # amount_eur != amount * fx_rate
    "missing_journal",      # no journal reference
    "unbalanced_journal",   # journal debit != credit
    "duplicate",            # same payment entered twice under a new ID
]

DATA_DIR = Path(__file__).parent / "data"


def make_fx_rates(rng: random.Random) -> pd.DataFrame:
    rows = []
    for d in range(N_DAYS):
        day = START_DATE + timedelta(days=d)
        for ccy, base in BASE_RATES.items():
            rate = base * (1 + rng.uniform(-0.015, 0.015))
            rows.append({"date": day.isoformat(), "currency": ccy, "rate": round(rate, 6)})
    return pd.DataFrame(rows)


def make_clean_transaction(i: int, rng: random.Random, rates: dict) -> dict:
    day = START_DATE + timedelta(days=rng.randrange(N_DAYS))
    ccy = rng.choice(list(BASE_RATES))
    amount = round(rng.uniform(50, 25_000) / (BASE_RATES[ccy] if ccy == "JPY" else 1), 2)
    rate = rates[(day.isoformat(), ccy)]
    amount_eur = round(amount * rate, 2)
    return {
        "txn_id": f"TXN{i:05d}",
        "date": day.isoformat(),
        "currency": ccy,
        "amount": amount,
        "fx_rate": rate,
        "amount_eur": amount_eur,
        "journal_ref": f"JE{rng.randrange(100000, 999999)}",
        "journal_debit_eur": amount_eur,
        "journal_credit_eur": amount_eur,
    }


def plant_error(txn: dict, error_type: str, rng: random.Random, rates: dict) -> None:
    """Mutate txn in place so it contains exactly one error of error_type."""
    if error_type == "wrong_fx_rate":
        # Off by 3-8% in either direction, with amounts recomputed so only the rate is wrong.
        factor = 1 + rng.choice([-1, 1]) * rng.uniform(0.03, 0.08)
        txn["fx_rate"] = round(txn["fx_rate"] * factor, 6)
        txn["amount_eur"] = round(txn["amount"] * txn["fx_rate"], 2)
        txn["journal_debit_eur"] = txn["journal_credit_eur"] = txn["amount_eur"]
    elif error_type == "currency_mismatch":
        other = rng.choice([c for c in BASE_RATES if c != txn["currency"]])
        txn["fx_rate"] = rates[(txn["date"], other)]
        txn["amount_eur"] = round(txn["amount"] * txn["fx_rate"], 2)
        txn["journal_debit_eur"] = txn["journal_credit_eur"] = txn["amount_eur"]
    elif error_type == "conversion_mismatch":
        txn["amount_eur"] = round(txn["amount_eur"] * rng.uniform(1.01, 1.10), 2)
        txn["journal_debit_eur"] = txn["journal_credit_eur"] = txn["amount_eur"]
    elif error_type == "missing_journal":
        txn["journal_ref"] = ""
    elif error_type == "unbalanced_journal":
        txn["journal_credit_eur"] = round(txn["journal_debit_eur"] - rng.uniform(1, 500), 2)
    else:
        raise ValueError(f"unknown error type: {error_type}")


def main() -> None:
    rng = random.Random(SEED)
    DATA_DIR.mkdir(exist_ok=True)

    fx = make_fx_rates(rng)
    rates = {(r.date, r.currency): r.rate for r in fx.itertuples()}

    n_errors = int(N_TRANSACTIONS * ERROR_RATE)
    n_duplicates = n_errors // len(ERROR_TYPES)
    n_clean = N_TRANSACTIONS - n_duplicates

    txns = [make_clean_transaction(i + 1, rng, rates) for i in range(n_clean)]
    answer_key = []

    # Plant non-duplicate errors in randomly chosen transactions.
    other_types = [t for t in ERROR_TYPES if t != "duplicate"]
    for txn in rng.sample(txns, n_errors - n_duplicates):
        error_type = rng.choice(other_types)
        plant_error(txn, error_type, rng, rates)
        answer_key.append({"txn_id": txn["txn_id"], "error_type": error_type})

    # Duplicates: copy clean transactions under a new ID.
    erroneous = {a["txn_id"] for a in answer_key}
    clean = [t for t in txns if t["txn_id"] not in erroneous]
    for j, original in enumerate(rng.sample(clean, n_duplicates)):
        dup = dict(original, txn_id=f"TXN{n_clean + j + 1:05d}")
        txns.append(dup)
        answer_key.append({"txn_id": dup["txn_id"], "error_type": "duplicate"})

    rng.shuffle(txns)
    pd.DataFrame(txns).to_csv(DATA_DIR / "transactions.csv", index=False)
    fx.to_csv(DATA_DIR / "fx_rates.csv", index=False)
    key = pd.DataFrame(answer_key).sort_values("txn_id")
    key.to_csv(DATA_DIR / "answer_key.csv", index=False)

    print(f"Wrote {len(txns)} transactions, {len(key)} with planted errors:")
    print(key["error_type"].value_counts().to_string())


if __name__ == "__main__":
    main()
