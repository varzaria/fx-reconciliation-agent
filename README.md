# FX Reconciliation Agent

An LLM agent that checks cross-currency payment transactions against reference FX rates and journal entries, flags discrepancies, explains them, and routes them to a human reviewer.

## Why

While processing payments at Allianz I found a recurring FX-rate mismatch pattern that caused rework between teams. This project explores how an AI agent could catch that class of error automatically, while keeping a human in the loop and every decision traceable.

## Status

- [x] Synthetic data generator with planted errors
- [x] Rule-based check functions (`checks.py`; `py evaluate.py` scores them: 50/50 caught, 0 false alarms)
- [x] LLM agent using the checks as tools (`agent.py`)
- [ ] Evaluation against the answer key
- [ ] Streamlit review interface

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

_To be added after evaluation._
