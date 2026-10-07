# FX Reconciliation Agent

An LLM agent that checks cross-currency payment transactions against reference FX rates and journal entries, flags discrepancies, explains them, and routes them to a human reviewer.

## Why

While processing payments at Allianz I found a recurring FX-rate mismatch pattern that caused rework between teams. This project explores how an AI agent could catch that class of error automatically, while keeping a human in the loop and every decision traceable.

## Status

- [x] Synthetic data generator with planted errors
- [x] Rule-based check functions (`checks.py`; `py evaluate.py` scores them: 50/50 caught, 0 false alarms)
- [x] LLM agent using the checks as tools (`agent.py`)
- [x] Evaluation against the answer key (`evaluate.py`; results below)
- [x] Streamlit review interface (`streamlit run review_app.py`)

## Data

`py generate_data.py` writes to `data/`:

| File | Contents |
| --- | --- |
| `transactions.csv` | 500 transactions to check |
| `fx_rates.csv` | Reference daily rates (EUR per 1 unit of foreign currency) |
| `answer_key.csv` | The ~10% of transactions with planted errors, and the error type |

Planted error types: wrong FX rate, currency mismatch, conversion mismatch, missing journal, unbalanced journal, duplicate.

## Setup

```
py -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   # then add your API key
py generate_data.py
```

## Results

Measured on the messy dataset (`data_messy/`): 120 transactions with free-text memos, including cases that exact rules get wrong. Generate it with `py generate_data.py --messy`.

| Case | Transactions | Rule-based checks | AI agent (Claude Opus 5.5) | AI agent (Claude Haiku 4.5) |
| --- | --- | --- | --- | --- |
| Clean | 93 | 93 | 93 | 93 |
| Standard errors | 12 | 12 | 12 | 12 |
| Near-duplicates (same invoice re-entered under a new date and journal ref) | 6 | **0** | 6 | 5 |
| Approved exceptions (contract rate explained in memo) | 6 | **0** (false alarms) | 6 | 6 |
| False contract claims (memo rate doesn't match) | 3 | 3 | 3 | 1 |
| **Total correct** | **120** | **108 (90%)** | **120 (100%)** | **117 (97.5%)** |
| Cost per 1,000 transactions | | free | ~$33 | ~$7 |

**Findings**

- **The AI earns its place on the messy cases.** Rules missed every near-duplicate (double payments) and raised a false alarm on every legitimate contract rate. The agent handled all 12 by reading memos and searching for similar payments.
- **Human review drops from 100% to 23% of transactions.** The Opus agent sent 28 of 120 transactions to a person: every flagged error plus the contract-rate exceptions, which policy says a human must confirm. The other 77% were cleared with a logged, explained decision.
- **The cheaper model is not "good enough" here.** Haiku costs ~5x less, but it silently cleared one double payment. Its two other misses were still routed to human review. For payments, one silent miss outweighs the saving, so Opus is the recommended model; Haiku could do a first pass on low-value transactions.
- **On clean, rule-friendly data the rules alone score 500/500.** Don't use AI where rules already work: the agent calls the rules as tools and adds judgement, explanations and suggested fixes on top.

**Estimating savings**

These are scenarios, not measured results. The only measured input is the agent's review rate: 92 of 120 transactions (77%) were cleared without needing a person. Hours saved per week = weekly volume × minutes per manual check × 77% ÷ 60.

| Transactions per week | 3 min per check | 5 min per check | 10 min per check | AI cost per week (Opus) |
| --- | --- | --- | --- | --- |
| 200 | 8 h | 13 h | 26 h | ~$7 |
| 1,000 | 38 h | 64 h | 128 h | ~$33 |
| 5,000 | 192 h | 319 h | 639 h | ~$165 |

Assumes cleared transactions need no further checking; in practice a team would spot-check a sample of them at first, so real savings start lower and grow as trust builds. The first step with any client is to measure their actual volume and time per check.

**Design choices**

- Tools take only a transaction ID and look up the numbers themselves, so the model cannot misread or invent figures.
- Every decision is logged with the checks run and their results (`logs/`), for audit.
- Company policy (contract-rate exceptions, which entry of a duplicate pair to flag) lives in the system prompt in plain English, where a finance team can read and change it.
