"""CareGap Voice dashboard: campaign funnel, eval pass rates, cost, latency, and an audit
trace per member. Run with ``make dashboard`` (http://localhost:8501); reads Postgres,
the committed eval reports and MLflow. Synthetic data only."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st

from caregap import audit
from caregap.config import get_settings
from caregap.customers import load_all
from caregap.dashboard import data


def usd(value: Any) -> str:
    return "-" if value is None else f"${Decimal(value):,.4f}"


def pct(value: Any) -> str:
    return "-" if value is None else f"{value:.1%}"


def campaign_tab(db: str) -> None:
    rows = data.campaigns(db)
    if not rows:
        st.info("No campaigns yet: `make run-campaign`.")
        return
    labels = {f"#{r['id']} {r['name']} ({r['customer']}, {r['members']} members)": r["id"]
              for r in rows}  # fmt: skip
    cid = labels[st.selectbox("Campaign", list(labels), key="funnel_campaign")]
    f = data.funnel(db, cid)
    st.subheader("Members by state")
    st.bar_chart(pd.Series(f["states"], name="members"), horizontal=True)
    left, right = st.columns(2)
    left.subheader("Calls")
    left.table(pd.Series(f["calls"], name="calls", dtype="int64"))
    right.subheader("Escalations")
    right.table(pd.Series(f["escalations"], name="escalations", dtype="int64"))


def evals_tab(tracking_uri: str) -> None:
    reports = data.reports()
    for customer, report in reports.items():
        st.subheader(f"{report.get('plan', customer)}")
        st.caption(f"Source: {report.get('source', '')}")
        rates = pd.DataFrame(data.eval_pass_rates(report)).set_index("configuration")
        st.dataframe(rates.map(pct))
        qa, voice = report.get("callback_qa", {}), report.get("voice", {})
        cols = st.columns(4)
        cols[0].metric("Callback hallucination rate", pct(qa.get("hallucination_rate")))
        cols[1].metric("Callback answer accuracy", pct(qa.get("answer_accuracy")))
        cols[2].metric("Script WER", pct(voice.get("Script word error rate")))
        cols[3].metric("Drug names heard right (overrides)",
                       pct(voice.get("Medication names, with overrides")))  # fmt: skip
    st.subheader("Prompt iterations (MLflow eval-suite)")
    history = data.eval_history(tracking_uri)
    if history:
        df = pd.DataFrame(history)
        df["started"] = pd.to_datetime(df["started"], unit="ms")
        st.dataframe(df, hide_index=True)
    else:
        st.caption("MLflow unreachable or no eval runs yet.")


def cost_tab(db: str, cap: Decimal) -> None:
    b = data.budget(db, cap)
    cols = st.columns(3)
    cols[0].metric("Budget (LLM_BUDGET_USD)", usd(b["cap_usd"]))
    cols[1].metric("Committed", usd(b["committed_usd"]))
    cols[2].metric("Remaining", usd(b["remaining_usd"]))
    st.subheader("Cost per generated message")
    per_message = data.cost_per_message(db)
    if per_message:
        df = pd.DataFrame(per_message)
        for c in ("total_usd", "mean_usd"):
            df[c] = df[c].map(usd)
        st.dataframe(df, hide_index=True)
    else:
        st.caption("No messages generated since Phase 10.")
    st.subheader("Model calls by day and purpose (last 30 days)")
    by_purpose = data.cost_by_purpose(db)
    if by_purpose:
        df = pd.DataFrame(by_purpose)
        df["usd"] = df["usd"].map(usd)
        st.dataframe(df, hide_index=True)


def latency_tab(db: str, customers: list[str]) -> None:
    customer = st.selectbox("Customer", ["all", *customers], key="latency_customer")
    rows = data.latency(db, None if customer == "all" else customer)
    if not rows:
        st.caption("No measurements yet.")
        return
    df = pd.DataFrame(rows).set_index("stage")
    st.dataframe(df.style.format({"p50_ms": "{:,.0f}", "p95_ms": "{:,.0f}"}))
    st.bar_chart(df[["p50_ms", "p95_ms"]], stack=False, horizontal=True)
    st.caption("call = call queued to final Twilio status (campaign clock).")


def trace_tab(db: str, tracking_uri: str) -> None:
    rows = data.campaigns(db)
    if not rows:
        st.info("No campaigns yet.")
        return
    cid = st.selectbox("Campaign", [r["id"] for r in rows], key="trace_campaign")
    member = st.selectbox("Member", data.members(db, cid), key="trace_member")
    t = audit.timeline(db, cid, member) if member else {}
    if not t:
        return
    m = t["member"]
    st.markdown(f"**{m['customer']}** member `{m['member_id']}`: state **{m['state']}**"
                + (f" ({m['reason']})" if m["reason"] else "")
                + f", {m['calls_made']} call(s)")  # fmt: skip
    st.markdown("Gaps: " + ", ".join(f"{g['measure']} *{g['status']}*" for g in t["gaps"]))
    for e in t["events"]:
        with st.expander(f"{e['at']:%H:%M:%S}  {e['event']}  ({e['actor']})"):
            refs = {k: e[k] for k in audit.REFS if e[k] is not None and k != "trace_id"}
            st.json({**refs, **e["payload"]})
            if e["trace_id"]:
                url = data.trace_url(tracking_uri, tracking_uri, e["trace_id"])
                st.markdown(f"MLflow trace [`{e['trace_id']}`]({url})" if url
                            else f"MLflow trace `{e['trace_id']}`")  # fmt: skip


def main() -> None:
    # Fail fast instead of retrying for minutes when MLflow is down.
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_MAX_RETRIES", "0")
    os.environ.setdefault("MLFLOW_HTTP_REQUEST_TIMEOUT", "5")
    settings = get_settings()
    db, mlflow_uri = settings.database_url, settings.mlflow_tracking_uri
    st.set_page_config(page_title="CareGap Voice", layout="wide")
    st.title("CareGap Voice")
    st.caption("Synthetic data only.")
    tabs = st.tabs(["Campaigns", "Evals", "Cost", "Latency", "Trace a member"])
    with tabs[0]:
        campaign_tab(db)
    with tabs[1]:
        evals_tab(mlflow_uri)
    with tabs[2]:
        cost_tab(db, settings.llm_budget_usd)
    with tabs[3]:
        latency_tab(db, sorted(load_all(settings.customers_dir)))
    with tabs[4]:
        trace_tab(db, mlflow_uri)


main()
