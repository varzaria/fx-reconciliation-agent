"""Human review screen for the reconciliation agent.

Run with:  streamlit run review_app.py

Shows the transactions the agent sent for human review, with its explanation,
suggested fix and the checks it ran. A reviewer approves or overrides each one;
decisions are saved with a timestamp to reviews/ as an audit trail.
"""

import json
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st

ROOT = Path(__file__).parent
RESULTS_DIR = ROOT / "results"
LOG_DIR = ROOT / "logs"
REVIEW_DIR = ROOT / "reviews"
REVIEW_COLUMNS = ["txn_id", "action", "note", "reviewed_at"]

st.set_page_config(page_title="Payment Reconciliation Review", layout="wide")


def stamp_of(path: Path) -> str:
    return path.stem.removeprefix("agent_predictions-")


def run_label(path: Path) -> str:
    """e.g. '20261007-190936 · claude-opus-5-5 · 120 txns'"""
    log_path = LOG_DIR / f"decisions-{stamp_of(path)}.jsonl"
    model = "?"
    if log_path.exists():
        with open(log_path, encoding="utf-8") as f:
            model = json.loads(f.readline()).get("model", "?")
    n = sum(1 for _ in open(path, encoding="utf-8")) - 1
    return f"{stamp_of(path)} · {model} · {n} txns"


@st.cache_data
def load_log(stamp: str) -> dict:
    """Decision log entries keyed by txn_id."""
    path = LOG_DIR / f"decisions-{stamp}.jsonl"
    if not path.exists():
        return {}
    entries = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    return {e["txn_id"]: e for e in entries}


def load_reviews(stamp: str) -> pd.DataFrame:
    path = REVIEW_DIR / f"reviews-{stamp}.csv"
    if not path.exists():
        return pd.DataFrame(columns=REVIEW_COLUMNS)
    return pd.read_csv(path, keep_default_na=False)


def save_review(stamp: str, txn_id: str, action: str, note: str) -> None:
    REVIEW_DIR.mkdir(exist_ok=True)
    path = REVIEW_DIR / f"reviews-{stamp}.csv"
    row = pd.DataFrame([{"txn_id": txn_id, "action": action, "note": note,
                         "reviewed_at": datetime.now().isoformat(timespec="seconds")}])
    row.to_csv(path, mode="a", header=not path.exists(), index=False)


# --- Choose a run --------------------------------------------------------------

runs = sorted(RESULTS_DIR.glob("agent_predictions-*.csv"), reverse=True)
if not runs:
    st.error("No agent runs found. Run `py agent.py` first.")
    st.stop()

with st.sidebar:
    st.header("Run")
    run_path = st.selectbox("Agent run", runs, format_func=run_label)
    dataset = st.selectbox("Dataset", ["data_messy", "data"],
                           help="The dataset the agent run was made on.")
    queue_filter = st.radio("Review queue shows", ["Pending", "All"], horizontal=True)

stamp = stamp_of(run_path)
predictions = pd.read_csv(run_path)
transactions = pd.read_csv(ROOT / dataset / "transactions.csv").set_index("txn_id")
if not predictions["txn_id"].isin(transactions.index).all():
    st.error(f"This run contains transactions that are not in {dataset}/. Pick the dataset the run was made on.")
    st.stop()

log = load_log(stamp)
reviews = load_reviews(stamp)
latest_review = reviews.drop_duplicates("txn_id", keep="last").set_index("txn_id")

model = next(iter(log.values()), {}).get("model", "unknown model")
needs_review = predictions[predictions["needs_human_review"]]
auto_cleared = predictions[~predictions["needs_human_review"]]
reviewed = needs_review["txn_id"].isin(latest_review.index)

# --- Header and summary -----------------------------------------------------

st.title("Payment Reconciliation Review")
st.caption(f"Agent run {stamp} · {model} · {dataset}/")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Transactions", len(predictions))
c2.metric("Auto-cleared by AI", len(auto_cleared), f"{len(auto_cleared) / len(predictions):.0%} of total", delta_color="off")
c3.metric("Sent for review", len(needs_review))
c4.metric("Reviewed", f"{reviewed.sum()} / {len(needs_review)}")
st.progress(reviewed.mean() if len(needs_review) else 1.0)

tab_queue, tab_cleared, tab_history = st.tabs(["Review queue", "Auto-cleared", "Review history"])

# --- Review queue -----------------------------------------------------------

with tab_queue:
    queue = needs_review if queue_filter == "All" else needs_review[~reviewed]
    if queue.empty:
        st.success("All transactions sent for review have been reviewed.")

    for _, pred in queue.iterrows():
        txn_id = pred["txn_id"]
        txn = transactions.loc[txn_id]
        flagged = bool(pred["flagged"])
        label = pred["error_type"] if flagged and isinstance(pred["error_type"], str) else "clear (confirm)"
        colour = "red" if flagged else "orange"

        with st.container(border=True):
            st.markdown(f"#### {txn_id} &nbsp; :{colour}-background[{label}] &nbsp; "
                        f":gray[confidence: {pred['confidence']}]")
            if txn_id in latest_review.index:
                r = latest_review.loc[txn_id]
                st.info(f"**{r['action'].capitalize()}** at {r['reviewed_at']}" + (f" · {r['note']}" if r["note"] else ""))

            left, right = st.columns([2, 3])
            with left:
                st.markdown("**Transaction**")
                st.markdown(
                    f"{txn['date']} · {txn['amount']:,.2f} {txn['currency']} @ {txn['fx_rate']}  \n"
                    f"EUR {txn['amount_eur']:,.2f} · journal {txn['journal_ref'] if isinstance(txn['journal_ref'], str) else '(none)'}  \n"
                    f"Debit {txn['journal_debit_eur']:,.2f} / credit {txn['journal_credit_eur']:,.2f}"
                )
                if "memo" in txn and isinstance(txn["memo"], str):
                    st.caption(f"Memo: {txn['memo']}")
            with right:
                st.markdown("**AI assessment**")
                st.write(pred["reason"])
                if isinstance(pred["suggested_fix"], str) and pred["suggested_fix"].lower() != "none":
                    st.markdown(f"**Suggested fix:** {pred['suggested_fix']}")

            entry = log.get(txn_id)
            if entry:
                with st.expander(f"Checks run by the agent ({len(entry['tool_calls'])})"):
                    for call in entry["tool_calls"]:
                        result = call["result"]
                        if "passed" in result:
                            icon = "✅" if result["passed"] else "❌"
                            st.markdown(f"{icon} `{call['tool']}` {result['reason']}")
                        elif "similar_payments" in result:
                            st.markdown(f"🔎 `{call['tool']}`: {len(result['similar_payments'])} similar payment(s)")
                            for p in result["similar_payments"]:
                                st.caption(f"{p.get('txn_id')} · {p.get('date')} · {p.get('journal_ref')} · {p.get('memo', '')}")
                        else:
                            st.markdown(f"⚠️ `{call['tool']}` {result}")

            note = st.text_input("Reviewer note (optional)", key=f"note-{txn_id}")
            b1, b2, _ = st.columns([1, 1, 4])
            if b1.button("Approve AI decision", key=f"approve-{txn_id}", type="primary"):
                save_review(stamp, txn_id, "approved", note)
                st.rerun()
            if b2.button("Override", key=f"override-{txn_id}"):
                save_review(stamp, txn_id, "overridden", note)
                st.rerun()

# --- Auto-cleared -------------------------------------------------------------

with tab_cleared:
    st.write(f"{len(auto_cleared)} transactions were cleared by the AI without needing review. "
             "Spot-check a sample, especially while the system is new.")
    st.dataframe(auto_cleared[["txn_id", "reason"]].rename(columns={"reason": "AI explanation"}),
                 hide_index=True, use_container_width=True)

# --- Review history -----------------------------------------------------------

with tab_history:
    if reviews.empty:
        st.write("No reviews yet.")
    else:
        st.dataframe(reviews.iloc[::-1], hide_index=True, use_container_width=True)
        st.download_button("Download review log (CSV)", reviews.to_csv(index=False),
                           file_name=f"reviews-{stamp}.csv", mime="text/csv")
