"""AI reconciliation agent.

For each transaction, Claude calls the rule-based checks from checks.py as tools,
then returns a structured review: status, error type, plain-English explanation,
suggested fix, and whether a human must review it. Every decision is logged.

Usage:
  py agent.py                     # 10-transaction sample (about half with planted errors)
  py agent.py --sample 30         # bigger sample
  py agent.py --all               # all 500 transactions (check the cost estimate first)
  py agent.py --model claude-haiku-4-5
"""

import argparse
import json
import random
from datetime import datetime
from pathlib import Path

import anthropic
import pandas as pd
from dotenv import load_dotenv

import checks

ROOT = Path(__file__).parent
LOG_DIR = ROOT / "logs"
RESULTS_DIR = ROOT / "results"

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TURNS = 8  # safety limit on tool-call rounds per transaction

# USD per million tokens (input, output), for the cost estimate printed after each run.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
}

ERROR_TYPES = ["missing_journal", "unbalanced_journal", "currency_mismatch",
               "wrong_fx_rate", "conversion_mismatch", "duplicate"]

SYSTEM_PROMPT = """You are a reconciliation analyst reviewing cross-currency payments for a finance team.

For the transaction you are given, run the checks you need using the tools provided. The tools look up the transaction's data themselves, so pass only its txn_id. You can call several tools at once.

Some transactions carry a free-text memo. Read it: it can explain a legitimate exception or point to a problem the checks miss.
- Duplicates are not always exact copies. Use find_similar_payments to look for the same payment entered again under a different date or journal reference; the same supplier invoice paid twice is a duplicate. Flag only the later entry (the higher txn_id); the earliest entry is the original.
- Company policy: a rate agreed under a Treasury forward contract is valid even if it differs from the market reference rate, as long as the rate used matches the contract rate stated in the memo exactly. Then the transaction is clear, but set needs_human_review to true so someone confirms the contract. If the rate used does not match the stated contract rate, the rate is wrong.

Then decide:
- status: "clear" if every check passes, otherwise "flagged".
- error_type: the single most specific problem, or "none". If both the currency check and the FX-rate check fail, the cause is currency_mismatch (the rate came from the wrong currency).
- explanation: one or two sentences a finance colleague can act on, quoting the key figures. Base it only on what the tools returned.
- suggested_fix: the concrete correction, or "none".
- needs_human_review: true for every flagged transaction, and for any case you are unsure about.
- confidence: how sure you are of the error type."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "txn_id": {"type": "string"},
        "status": {"type": "string", "enum": ["clear", "flagged"]},
        "error_type": {"type": "string", "enum": ["none", *ERROR_TYPES]},
        "explanation": {"type": "string"},
        "suggested_fix": {"type": "string"},
        "needs_human_review": {"type": "boolean"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
    },
    "required": ["txn_id", "status", "error_type", "explanation", "suggested_fix",
                 "needs_human_review", "confidence"],
    "additionalProperties": False,
}


def make_tool(name: str, description: str) -> dict:
    return {
        "name": name,
        "description": description,
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"txn_id": {"type": "string", "description": "e.g. TXN00042"}},
            "required": ["txn_id"],
            "additionalProperties": False,
        },
    }


TOOLS = [
    make_tool("check_journal_exists", "Checks the payment has a journal reference."),
    make_tool("check_journal_balance", "Checks the journal debit equals the journal credit."),
    make_tool("check_currency", "Checks whether the FX rate used actually belongs to a different currency on that date."),
    make_tool("check_fx_rate", "Checks the FX rate used is within 2% of the reference rate for that date and currency."),
    make_tool("check_conversion", "Checks amount x fx_rate equals the recorded EUR amount."),
    make_tool("check_duplicate", "Checks whether this payment is an exact copy of an earlier transaction (same date, currency, amount and journal reference)."),
    make_tool("find_similar_payments", "Lists other payments with the same currency and amount within 7 days, with their memos, to spot payments entered twice in a slightly different form."),
]


class Toolbox:
    """Runs the checks from checks.py on behalf of the agent."""

    ROW_CHECKS = {
        "check_journal_exists": checks.check_journal_exists,
        "check_journal_balance": checks.check_journal_balance,
        "check_currency": checks.check_currency,
        "check_fx_rate": checks.check_fx_rate,
        "check_conversion": checks.check_conversion,
    }

    def __init__(self, transactions: pd.DataFrame, data_dir: Path):
        self.rows = transactions.set_index("txn_id", drop=False)
        self.rates = checks.load_rates(data_dir / "fx_rates.csv")
        self.duplicates = checks.find_duplicates(transactions)

    def find_similar_payments(self, txn_id: str) -> dict:
        txn = self.rows.loc[txn_id]
        dates = pd.to_datetime(self.rows["date"])
        similar = self.rows[
            (self.rows["txn_id"] != txn_id)
            & (self.rows["currency"] == txn["currency"])
            & ((self.rows["amount"] - txn["amount"]).abs() <= 0.01)
            & ((dates - pd.to_datetime(txn["date"])).abs() <= pd.Timedelta(days=7))
        ]
        columns = [c for c in ["txn_id", "date", "amount", "journal_ref", "memo"] if c in similar.columns]
        return {"similar_payments": json.loads(similar[columns].to_json(orient="records"))}

    def run(self, name: str, txn_id: str) -> dict:
        if txn_id not in self.rows.index:
            raise KeyError(f"unknown txn_id {txn_id}")
        if name == "find_similar_payments":
            return self.find_similar_payments(txn_id)
        if name == "check_duplicate":
            if txn_id in self.duplicates:
                return checks.result(False, f"repeat of {self.duplicates[txn_id]}")
            return checks.result(True)
        return self.ROW_CHECKS[name](self.rows.loc[txn_id], self.rates)


def review_transaction(client, model: str, toolbox: Toolbox, txn: pd.Series) -> tuple[dict, dict]:
    """Run the agent loop for one transaction. Returns (decision, log entry)."""
    messages = [{"role": "user", "content": f"Review this transaction:\n{txn.to_json()}"}]
    output_config = {"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}}
    if not model.startswith("claude-haiku"):
        output_config["effort"] = "low"  # simple task; low effort keeps cost down

    log = {"txn_id": txn["txn_id"], "model": model, "tool_calls": [],
           "input_tokens": 0, "output_tokens": 0}

    for _ in range(MAX_TURNS):
        response = client.messages.create(
            model=model,
            max_tokens=16000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            output_config=output_config,
            messages=messages,
        )
        log["input_tokens"] += response.usage.input_tokens
        log["output_tokens"] += response.usage.output_tokens

        if response.stop_reason == "end_turn":
            text = next(b.text for b in response.content if b.type == "text")
            decision = json.loads(text)
            log["decision"] = decision
            return decision, log

        if response.stop_reason != "tool_use":
            raise RuntimeError(f"unexpected stop_reason: {response.stop_reason}")

        # Run every tool Claude asked for and send all the results back in one message.
        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            try:
                outcome = toolbox.run(block.name, block.input["txn_id"])
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": json.dumps(outcome)})
            except Exception as e:
                outcome = {"error": str(e)}
                tool_results.append({"type": "tool_result", "tool_use_id": block.id,
                                     "content": f"Error: {e}", "is_error": True})
            log["tool_calls"].append({"tool": block.name, "result": outcome})
        messages.append({"role": "user", "content": tool_results})

    raise RuntimeError(f"no decision after {MAX_TURNS} turns")


def pick_sample(transactions: pd.DataFrame, data_dir: Path, n: int, seed: int = 7) -> pd.DataFrame:
    """About half answer-key cases (errors and tricky cases), half clean, so a small run tests both."""
    key = pd.read_csv(data_dir / "answer_key.csv")
    with_errors = transactions[transactions["txn_id"].isin(key["txn_id"])]
    clean = transactions[~transactions["txn_id"].isin(key["txn_id"])]
    n_err = min(n // 2, len(with_errors))
    sample = pd.concat([with_errors.sample(n_err, random_state=seed),
                        clean.sample(n - n_err, random_state=seed)])
    return sample.sample(frac=1, random_state=seed)  # shuffle


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--sample", type=int, default=10, help="number of transactions to review (default 10)")
    parser.add_argument("--all", action="store_true", help="review all transactions")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Claude model (default {DEFAULT_MODEL})")
    parser.add_argument("--data", default="data", help="dataset folder: data (default) or data_messy")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    client = anthropic.Anthropic()

    data_dir = ROOT / args.data
    transactions = pd.read_csv(data_dir / "transactions.csv")
    batch = transactions if args.all else pick_sample(transactions, data_dir, args.sample)
    toolbox = Toolbox(transactions, data_dir)

    LOG_DIR.mkdir(exist_ok=True)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = LOG_DIR / f"decisions-{stamp}.jsonl"

    rows, total_in, total_out = [], 0, 0
    print(f"Reviewing {len(batch)} transactions with {args.model}\n")
    with open(log_path, "w", encoding="utf-8") as log_file:
        for i, (_, txn) in enumerate(batch.iterrows(), 1):
            try:
                decision, log = review_transaction(client, args.model, toolbox, txn)
            except Exception as e:
                print(f"[{i}/{len(batch)}] {txn['txn_id']}  ERROR: {e}")
                log_file.write(json.dumps({"txn_id": txn["txn_id"], "error": str(e)}) + "\n")
                continue
            log_file.write(json.dumps(log) + "\n")
            total_in += log["input_tokens"]
            total_out += log["output_tokens"]
            flagged = decision["status"] == "flagged"
            rows.append({
                "txn_id": txn["txn_id"],
                "flagged": flagged,
                "error_type": "" if decision["error_type"] == "none" else decision["error_type"],
                "reason": decision["explanation"],
                "suggested_fix": decision["suggested_fix"],
                "needs_human_review": decision["needs_human_review"],
                "confidence": decision["confidence"],
            })
            label = decision["error_type"] if flagged else "clear"
            print(f"[{i}/{len(batch)}] {txn['txn_id']}  {label:<20} {decision['explanation']}")

    out_path = RESULTS_DIR / f"agent_predictions-{stamp}.csv"
    pd.DataFrame(rows).to_csv(out_path, index=False)

    price_in, price_out = PRICES.get(args.model, (0, 0))
    cost = total_in / 1e6 * price_in + total_out / 1e6 * price_out
    print(f"\nTokens: {total_in:,} in / {total_out:,} out  ->  about ${cost:.2f}")
    if not args.all and len(batch):
        print(f"Estimated cost for all {len(transactions)} transactions: about ${cost / len(batch) * len(transactions):.2f}")
    print(f"Predictions: {out_path.relative_to(ROOT)}")
    print(f"Decision log: {log_path.relative_to(ROOT)}")
    print(f"Score it with: py evaluate.py --agent {out_path.relative_to(ROOT)} --data {args.data}")


if __name__ == "__main__":
    main()
