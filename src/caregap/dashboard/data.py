"""Queries behind the dashboard (Phase 10). Plain functions over Postgres, the committed
eval reports and MLflow, so they are tested without Streamlit."""

from __future__ import annotations

import json
from collections.abc import Sequence
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

FUNNEL = ("selected", "preparing", "awaiting_review", "ready", "calling", "retry_wait",
          "completed", "opted_out", "escalated", "failed", "skipped")  # fmt: skip
STAGES = ("generate", "compliance", "tts", "call", "callback_answer")


def _rows(conninfo: str, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        return conn.execute(sql.encode(), params).fetchall()


def campaigns(conninfo: str) -> list[dict[str, Any]]:
    return _rows(
        conninfo,
        "SELECT k.id, k.name, k.customer, k.created_at, count(cm.member_id) AS members"
        " FROM campaigns k LEFT JOIN campaign_members cm ON cm.campaign_id = k.id"
        " GROUP BY k.id ORDER BY k.id DESC",
    )


def funnel(conninfo: str, campaign_id: int) -> dict[str, Any]:
    """Members per state (in funnel order), calls per status, escalations per kind."""
    states = {r["state"]: r["n"] for r in _rows(
        conninfo, "SELECT state, count(*) AS n FROM campaign_members WHERE campaign_id = %s"
        " GROUP BY 1", (campaign_id,))}  # fmt: skip
    calls = {r["status"]: r["n"] for r in _rows(
        conninfo, "SELECT status, count(*) AS n FROM call_attempts WHERE campaign_id = %s"
        " GROUP BY 1 ORDER BY 1", (campaign_id,))}  # fmt: skip
    escalations = {r["kind"]: r["n"] for r in _rows(
        conninfo, "SELECT kind, count(*) AS n FROM escalations WHERE campaign_id = %s"
        " GROUP BY 1 ORDER BY 1", (campaign_id,))}  # fmt: skip
    return {
        "states": {s: states[s] for s in FUNNEL if s in states},
        "calls": calls,
        "escalations": escalations,
    }


def members(conninfo: str, campaign_id: int) -> list[str]:
    return [r["member_id"] for r in _rows(
        conninfo, "SELECT member_id FROM campaign_members WHERE campaign_id = %s"
        " ORDER BY member_id", (campaign_id,))]  # fmt: skip


def cost_per_message(conninfo: str) -> list[dict[str, Any]]:
    """Generated messages per customer and kind, with total and mean cost (Decimal).
    Rows from before Phase 10 have no cost and are left out."""
    return _rows(
        conninfo,
        """
        SELECT customer, 'outreach script' AS kind, count(*) AS messages,
               sum(cost_usd) AS total_usd, avg(cost_usd) AS mean_usd
        FROM outreach_scripts WHERE cost_usd IS NOT NULL GROUP BY customer
        UNION ALL
        SELECT customer, 'callback answer', count(*), sum(cost_usd), avg(cost_usd)
        FROM callback_questions WHERE cost_usd IS NOT NULL GROUP BY customer
        ORDER BY customer, kind
        """,
    )


def cost_by_purpose(conninfo: str, days: int = 30) -> list[dict[str, Any]]:
    """Model spend per day and purpose (the part before the first colon), from the ledger."""
    return _rows(
        conninfo,
        """
        SELECT date_trunc('day', created_at)::date AS day,
               split_part(purpose, ':', 1) AS purpose, provider, count(*) AS calls,
               coalesce(sum(actual_usd), 0) AS usd,
               coalesce(sum(input_tokens), 0) AS input_tokens,
               coalesce(sum(output_tokens), 0) AS output_tokens
        FROM cost_ledger
        WHERE status = 'settled' AND created_at > now() - make_interval(days => %s)
        GROUP BY 1, 2, 3 ORDER BY 1 DESC, 5 DESC, 2
        """,
        (days,),
    )


def budget(conninfo: str, cap_usd: Decimal) -> dict[str, Decimal]:
    row = _rows(
        conninfo,
        "SELECT coalesce(sum(coalesce(actual_usd, estimated_usd)), 0) AS committed"
        " FROM cost_ledger WHERE status IN ('reserved', 'settled')",
    )[0]
    committed = Decimal(row["committed"])
    return {"cap_usd": cap_usd, "committed_usd": committed,
            "remaining_usd": max(Decimal(0), cap_usd - committed)}  # fmt: skip


def latency(conninfo: str, customer: str | None = None) -> list[dict[str, Any]]:
    """p50 and p95 per stage, in milliseconds."""
    rows = _rows(
        conninfo,
        """
        SELECT stage, count(*) AS n,
               percentile_cont(0.5) WITHIN GROUP (ORDER BY ms) AS p50_ms,
               percentile_cont(0.95) WITHIN GROUP (ORDER BY ms) AS p95_ms
        FROM stage_latency WHERE ms IS NOT NULL AND (%s::text IS NULL OR customer = %s)
        GROUP BY stage
        """,
        (customer, customer),
    )
    by_stage = {r["stage"]: r for r in rows}
    return [by_stage[s] for s in STAGES if s in by_stage]


def reports(reports_dir: Path = Path("reports")) -> dict[str, dict[str, Any]]:
    """The committed per-customer eval reports (scripts, callback Q&A, voice/WER)."""
    return {p.stem: json.loads(p.read_text()) for p in sorted(reports_dir.glob("*.json"))}


def eval_pass_rates(report: dict[str, Any]) -> list[dict[str, Any]]:
    """One row per configuration of a customer's report."""
    keys = ("first_attempt_pass_rate", "final_pass_rate", "judge_all_pass_rate")
    return [{"configuration": name, **{k: m.get(k) for k in keys}}
            for name, m in report.get("scripts", {}).items()]  # fmt: skip


def eval_history(tracking_uri: str, limit: int = 20) -> list[dict[str, Any]]:
    """Prompt iterations from the ``eval-suite`` experiment (parent runs), newest first.
    Empty when MLflow is unreachable."""
    from mlflow.tracking import MlflowClient

    from caregap.evals.suite import EXPERIMENT, PRIMARY

    try:
        client = MlflowClient(tracking_uri)
        exp = client.get_experiment_by_name(EXPERIMENT)
        if exp is None:
            return []
        runs = client.search_runs([exp.experiment_id], order_by=["attributes.start_time DESC"],
                                  max_results=200)  # fmt: skip
    except Exception:
        return []
    out = []
    for r in runs:
        if "mlflow.parentRunId" in r.data.tags:
            continue
        out.append({
            "started": r.info.start_time, "run": r.info.run_id[:8],
            "alias": r.data.params.get("alias"), "promoted": r.data.tags.get("promoted"),
            PRIMARY: r.data.metrics.get(PRIMARY),
            "first_attempt_pass_rate": r.data.metrics.get("first_attempt_pass_rate"),
        })  # fmt: skip
        if len(out) == limit:
            break
    return out


def trace_url(ui_url: str, tracking_uri: str, trace_id: str) -> str | None:
    """Link to a trace in the MLflow UI (None when MLflow is unreachable)."""
    from mlflow.tracking import MlflowClient

    from caregap.observability import EXPERIMENT

    try:
        exp = MlflowClient(tracking_uri).get_experiment_by_name(EXPERIMENT)
    except Exception:
        return None
    if exp is None:
        return None
    return (f"{ui_url.rstrip('/')}/#/experiments/{exp.experiment_id}/traces"
            f"?selectedEvaluationId={trace_id}")  # fmt: skip
