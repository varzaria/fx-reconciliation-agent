"""Human review screen for the reconciliation agent.

Run with:  streamlit run review_app.py

Shows the transactions the agent sent for human review, one row each, with the
AI's verdict. Details (explanation, suggested fix, the checks the agent ran)
open underneath. A reviewer approves or overrides each one; decisions are saved
with who, when and an optional note to reviews/ as an audit trail.
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
REVIEW_COLUMNS = ["txn_id", "action", "note", "reviewed_at", "reviewed_by"]

st.set_page_config(page_title="Payment Reconciliation Review", page_icon="💱", layout="wide")
st.markdown("""<style>
.block-container {padding-top: 2rem;}
/* Approve buttons green, Override buttons red (Streamlit tags each button's container with its key) */
[class*="st-key-approve-"] button {background-color: #1e9e55; border-color: #1e9e55; color: white;}
[class*="st-key-approve-"] button:hover {background-color: #178244; border-color: #178244; color: white;}
[class*="st-key-override-"] button {background-color: #d64545; border-color: #d64545; color: white;}
[class*="st-key-override-"] button:hover {background-color: #b53434; border-color: #b53434; color: white;}
</style>""", unsafe_allow_html=True)


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
    df = pd.read_csv(path, keep_default_na=False)
    return df.reindex(columns=REVIEW_COLUMNS, fill_value="")  # older review files have no reviewed_by


def save_review(stamp: str, txn_id: str, action: str, note: str, reviewer: str) -> None:
    REVIEW_DIR.mkdir(exist_ok=True)
    path = REVIEW_DIR / f"reviews-{stamp}.csv"
    existing = load_reviews(stamp)
    row = pd.DataFrame([{"txn_id": txn_id, "action": action, "note": note,
                         "reviewed_at": datetime.now().isoformat(timespec="seconds"), "reviewed_by": reviewer}])
    pd.concat([existing, row]).to_csv(path, index=False)


def is_text(value) -> bool:
    return isinstance(value, str) and value.strip() != ""


# --- Sidebar ---------------------------------------------------------------------

runs = sorted(RESULTS_DIR.glob("agent_predictions-*.csv"), reverse=True)
if not runs:
    st.error("No agent runs found. Run `py agent.py` first.")
    st.stop()


def default_run(paths: list[Path]) -> int:
    """Open on the largest run made with Opus (the recommended model), else the newest run."""
    def size_if_opus(path: Path) -> int:
        label = run_label(path)
        return int(label.split(" · ")[-1].split()[0]) if "opus" in label else -1
    best = max(range(len(paths)), key=lambda i: (size_if_opus(paths[i]), -i))
    return best if size_if_opus(paths[best]) >= 0 else 0

with st.sidebar:
    st.header("💱 Payment Review")
    st.caption("AI reconciliation agent · human review")
    reviewer = st.text_input("Your name", value="Alexandru Varzari", help="Recorded with every decision.")
    run_path = st.selectbox("Agent run", runs, index=default_run(runs), format_func=run_label)
    dataset = st.selectbox("Dataset", ["data_messy", "data"], help="The dataset the agent run was made on.")
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
predictions["amount_eur"] = predictions["txn_id"].map(transactions["amount_eur"])
# Priority order for reviewers: actual errors first, largest first; then cases the AI cleared but wants confirmed.
needs_review = (predictions[predictions["needs_human_review"]]
                .sort_values(["flagged", "amount_eur"], ascending=[False, False]))
auto_cleared = predictions[~predictions["needs_human_review"]]
reviewed = needs_review["txn_id"].isin(latest_review.index)
value_flagged = predictions.loc[predictions["flagged"], "amount_eur"].sum()

# --- Header and summary ------------------------------------------------------------

st.subheader("Payment Reconciliation Review")
st.caption(f"Agent run {stamp} · {model} · {dataset}/")

m = st.columns(5)
m[0].metric("Transactions", len(predictions))
m[1].metric("Auto-cleared by AI", len(auto_cleared), f"{len(auto_cleared) / len(predictions):.0%} of total", delta_color="off")
m[2].metric("Sent for review", len(needs_review))
m[3].metric("Payments flagged", f"€{value_flagged:,.0f}", f"{int(predictions['flagged'].sum())} errors found", delta_color="off",
            help="Total EUR value of the transactions the AI found errors in")
m[4].metric("Reviewed", f"{reviewed.sum()} / {len(needs_review)}")
st.progress(reviewed.mean() if len(needs_review) else 1.0)

tab_queue, tab_cleared, tab_history = st.tabs([
    f"Review queue ({int((~reviewed).sum())})", f"Auto-cleared ({len(auto_cleared)})", f"Review history ({len(reviews)})"])

QUEUE_COLUMNS = [2.2, 2.2, 2.2, 1.2, 2.8, 1.3, 1.3]  # transaction, amount, AI verdict, confidence, note, approve, override


def show_details(pred, txn) -> None:
    """Everything behind the AI's verdict: the transaction, its explanation and the checks it ran."""
    left, right = st.columns([2, 3])
    with left:
        journal = txn["journal_ref"] if is_text(txn["journal_ref"]) else "(none)"
        st.markdown(f"**Transaction**  \n{txn['date']} · {txn['amount']:,.2f} {txn['currency']} @ {txn['fx_rate']}  \n"
                    f"EUR {txn['amount_eur']:,.2f} · journal {journal}  \n"
                    f"Debit {txn['journal_debit_eur']:,.2f} / credit {txn['journal_credit_eur']:,.2f}")
        if "memo" in txn and is_text(txn["memo"]):
            st.caption(f"Memo: {txn['memo']}")
    with right:
        st.markdown(f"**AI assessment**  \n{pred['reason']}")
        if is_text(pred["suggested_fix"]) and pred["suggested_fix"].lower() != "none":
            st.markdown(f"**Suggested fix:** {pred['suggested_fix']}")
    entry = log.get(pred["txn_id"])
    if entry:
        st.markdown(f"**Checks run by the agent ({len(entry['tool_calls'])})**")
        for call in entry["tool_calls"]:
            result = call["result"]
            if "passed" in result:
                st.caption(f"{'✅' if result['passed'] else '❌'} `{call['tool']}` {result['reason']}")
            elif "similar_payments" in result:
                st.caption(f"🔎 `{call['tool']}`: {len(result['similar_payments'])} similar payment(s)")
                for p in result["similar_payments"]:
                    st.caption(f"    {p.get('txn_id')} · {p.get('date')} · {p.get('journal_ref')} · {p.get('memo', '')}")
            else:
                st.caption(f"⚠️ `{call['tool']}` {result}")


# --- Review queue --------------------------------------------------------------------

with tab_queue:
    queue = needs_review if queue_filter == "All" else needs_review[~reviewed]
    if queue.empty:
        st.success("All transactions sent for review have been reviewed.")
    else:
        header = st.columns(QUEUE_COLUMNS)
        for col, label in zip(header, ["Transaction", "Amount", "AI verdict", "Confidence", "Note", "", ""]):
            col.caption(label)

    for _, pred in queue.iterrows():
        txn_id = pred["txn_id"]
        txn = transactions.loc[txn_id]
        flagged = bool(pred["flagged"])
        verdict = (f":red-badge[{pred['error_type'].replace('_', ' ')}]" if flagged and is_text(pred["error_type"])
                   else ":orange-badge[clear: confirm]")
        with st.container(border=True):
            c = st.columns(QUEUE_COLUMNS, vertical_alignment="center")
            c[0].markdown(f"**{txn_id}**  \n:gray[{txn['date']}]")
            c[1].markdown(f"**{txn['amount']:,.2f} {txn['currency']}**  \n:gray[EUR {txn['amount_eur']:,.2f}]")
            c[2].markdown(verdict, help=pred["reason"])
            c[3].markdown(f":gray[{pred['confidence']}]")
            if txn_id in latest_review.index:
                r = latest_review.loc[txn_id]
                done = f"**{r['action'].capitalize()}**" + (f" by {r['reviewed_by']}" if r["reviewed_by"] else "")
                c[4].markdown(done + (f"  \n:gray[{r['note']}]" if r["note"] else ""))
            else:
                note = c[4].text_input("Note", key=f"note-{txn_id}", placeholder="Note (optional)", label_visibility="collapsed")
                if c[5].button("Approve", key=f"approve-{txn_id}", use_container_width=True, help="Accept the AI's verdict"):
                    save_review(stamp, txn_id, "approved", note, reviewer)
                    st.rerun()
                if c[6].button("Override", key=f"override-{txn_id}", use_container_width=True, help="The AI got this wrong"):
                    if note.strip():
                        save_review(stamp, txn_id, "overridden", note, reviewer)
                        st.rerun()
                    else:  # overriding a control needs a recorded reason
                        st.warning("Add a note explaining why the AI is wrong. Overrides need a reason for the audit trail.")
            with st.expander("Details: AI explanation and checks"):
                show_details(pred, txn)

# --- Auto-cleared ----------------------------------------------------------------------

with tab_cleared:
    st.caption("These transactions passed every check and were cleared without anyone looking at them. "
               "Open a few each day to confirm the AI got them right. 🔎 = today's suggested spot-check sample.")
    if not auto_cleared.empty:
        # A small sample that changes daily, shown first, so it's clear where to start.
        sample_ids = set(auto_cleared.sample(min(3, len(auto_cleared)), random_state=datetime.now().toordinal())["txn_id"])
        ordered = auto_cleared.assign(spot=auto_cleared["txn_id"].isin(sample_ids)).sort_values("spot", ascending=False, kind="stable")
        for _, pred in ordered.iterrows():
            txn = transactions.loc[pred["txn_id"]]
            label = (f"{'🔎 ' if pred['spot'] else ''}{pred['txn_id']} · {txn['date']} · "
                     f"{txn['amount']:,.2f} {txn['currency']} (EUR {txn['amount_eur']:,.2f})")
            with st.expander(label):
                show_details(pred, txn)

# --- Review history --------------------------------------------------------------------

with tab_history:
    if reviews.empty:
        st.caption("No reviews yet.")
    else:
        st.dataframe(reviews.iloc[::-1][["reviewed_at", "action", "reviewed_by", "txn_id", "note"]],
                     hide_index=True, use_container_width=True)
        st.download_button("Download review log (CSV)", reviews.to_csv(index=False),
                           file_name=f"reviews-{stamp}.csv", mime="text/csv")
