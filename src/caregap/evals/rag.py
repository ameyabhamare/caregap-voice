"""Callback Q&A eval: correctness, refusals and hallucinations per customer, in MLflow.

    make eval-rag CUSTOMER=sunrise

Runs every golden question in ``fixtures/qa/<customer>.yaml`` through the same agent as
the service (no database writes) and scores two things:

- **What the caller hears** (after the grounding check).
- **The raw draft** (as if there were no check), to show what the model would have said.

Each question gets one category:

- correct            answerable and every expected fact is there, or rightly handed off
- over_refusal       answerable, but handed off
- incomplete         answered without a wrong amount, but an expected fact is missing
- wrong_cost_share   an amount from this plan's document, but not the right one
- wrong_plan         an amount only in another plan's document, or another plan named
- fabricated_benefit an amount that is in no plan's document
- missed_refusal     should have been handed off (medical, member-specific, off-topic,
                     not in the document) but was answered

Amounts are dollar figures and percentages. Faithfulness here is amount-level: whether
every amount in an answer is in the retrieved text. Claim-level judging is Phase 8.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import subprocess
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from caregap.outreach.compliance import normalize
from caregap.rag.agent import NOT_FOUND, Answer, CallbackAgent, amounts, detect_language
from caregap.rag.agent import speakable as strip_citations

EXPERIMENT = "rag-evals"
QA_DIR = Path("fixtures/qa")
#: Ways of saying "$0" that count as the fact "$0".
ZERO = ("$0", "no cost", "free", "nothing", "no copay", "don't pay", "do not pay",
        "sin costo", "gratis", "no paga", "sin copago")  # fmt: skip
WRONG = ("fabricated_benefit", "wrong_plan", "wrong_cost_share")


class GoldenItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    question: str
    language: Literal["en", "es"]
    expect: Literal["answer", "unknown", "refuse"]
    route: Literal["benefit", "medical", "member", "off_topic"]
    section: str | None = None
    facts: tuple[str | tuple[str, ...], ...] = ()


class GoldenSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    customer: str
    not_covered: tuple[str, ...] = Field(default=())
    items: tuple[GoldenItem, ...]


def load_golden(customer: str, directory: Path = QA_DIR) -> GoldenSet:
    data = yaml.safe_load((directory / f"{customer}.yaml").read_text())
    return GoldenSet.model_validate(data)


def fact_present(fact: str | tuple[str, ...], text: str) -> bool:
    if isinstance(fact, tuple):
        return any(fact_present(f, text) for f in fact)
    if fact == "$0":
        return any(fact_present(z, text) for z in ZERO[1:]) or "$0" in amounts(text)
    if fact.startswith("$") or fact.endswith("%"):
        return amounts(fact) <= amounts(text)
    return f" {_norm(fact)} " in f" {_norm(text)} "


def _norm(text: str) -> str:
    return normalize(text.replace(",", ""))


@dataclass(frozen=True)
class Documents:
    """Amounts in this plan's document and in every other plan's."""

    own: set[str]
    others: set[str]
    other_names: tuple[str, ...]


@dataclass(frozen=True)
class Verdict:
    category: str
    #: Wrong-fact flags on a missed refusal (it can also invent or mix up plans).
    flags: tuple[str, ...] = ()


def classify(item: GoldenItem, spoken: str | None, docs: Documents) -> Verdict:
    """``spoken`` is the answer the caller heard, or None if they were handed off."""
    if spoken is None:
        return Verdict("over_refusal" if item.expect == "answer" else "correct")
    found = amounts(spoken)
    flags = []
    named = any(f" {normalize(n)} " in f" {normalize(spoken)} " for n in docs.other_names)
    if named or any(a not in docs.own and a in docs.others for a in found):
        flags.append("wrong_plan")
    if any(a not in docs.own and a not in docs.others for a in found):
        flags.append("fabricated_benefit")
    if item.expect != "answer":
        return Verdict("missed_refusal", tuple(flags))
    if all(fact_present(f, spoken) for f in item.facts) and not flags:
        return Verdict("correct")
    if flags:
        return Verdict(flags[0], tuple(flags))
    numeric = [f for f in item.facts if isinstance(f, str) and (f[:1] == "$" or f[-1:] == "%")]
    if numeric and found:
        return Verdict("wrong_cost_share")
    return Verdict("incomplete")


@dataclass(frozen=True)
class Result:
    item: GoldenItem
    answer: Answer
    final: Verdict
    raw: Verdict
    retrieval_hit: bool | None
    #: Every amount in the raw draft is in the retrieved text (None: no draft).
    draft_faithful: bool | None


def evaluate(item: GoldenItem, answer: Answer, docs: Documents) -> Result:
    spoken = answer.text if answer.outcome == "answered" else None
    draft_answer = (
        strip_citations(answer.draft) if answer.draft and NOT_FOUND not in answer.draft else None
    )
    retrieved_amounts = set().union(*(amounts(t) for t in answer.retrieved_text.values()))
    hit = None
    if item.section and answer.retrieved:
        hit = any(r.split("-")[0] == item.section for r in answer.retrieved)
    return Result(
        item=item,
        answer=answer,
        final=classify(item, spoken, docs),
        raw=classify(item, draft_answer, docs),
        retrieval_hit=hit,
        draft_faithful=None if draft_answer is None else amounts(draft_answer) <= retrieved_amounts,
    )


def _rate(n: int, d: int) -> float:
    return n / d if d else 0.0


def summarize(results: list[Result]) -> dict[str, float]:
    answerable = [r for r in results if r.item.expect == "answer"]
    refusals = [r for r in results if r.item.expect != "answer"]
    answered = [r for r in results if r.answer.outcome == "answered"]
    drafted = [r for r in results if r.draft_faithful is not None]
    hits = [r.retrieval_hit for r in results if r.retrieval_hit is not None]
    m: dict[str, float] = {
        "questions": len(results),
        "answerable": len(answerable),
        "answer_accuracy": _rate(sum(r.final.category == "correct" for r in answerable),
                                 len(answerable)),
        "refusal_accuracy": _rate(sum(r.final.category == "correct" for r in refusals),
                                  len(refusals)),
        "over_refusal_rate": _rate(sum(r.final.category == "over_refusal" for r in answerable),
                                   len(answerable)),
        "answered": len(answered),
        # Spoken answers that state a wrong amount or another plan's facts.
        "hallucination_rate": _rate(sum(any(f in WRONG for f in (r.final.category,
                                                                  *r.final.flags))
                                        for r in answered), len(answered)),
        "raw_hallucination_rate": _rate(
            sum(any(f in WRONG for f in (r.raw.category, *r.raw.flags)) for r in drafted),
            len(drafted)),
        "faithfulness": _rate(sum(amounts(r.answer.text) <= set().union(
            *(amounts(t) for t in r.answer.retrieved_text.values())) for r in answered),
            len(answered)),
        "raw_faithfulness": _rate(sum(bool(r.draft_faithful) for r in drafted), len(drafted)),
        "routing_accuracy": _rate(sum(r.answer.route == r.item.route for r in results),
                                  len(results)),
        "retrieval_hit_rate": _rate(sum(hits), len(hits)),
        "language_accuracy": _rate(sum(detect_language(r.answer.text) == r.item.language
                                       for r in results), len(results)),
        "latency_p50_s": statistics.median([r.answer.latency_s for r in results] or [0.0]),
        "latency_p95_s": _p95([r.answer.latency_s for r in results]),
    }  # fmt: skip
    for prefix, key in (("final", "final"), ("raw", "raw")):
        for cat, n in Counter(getattr(r, key).category for r in results).items():
            m[f"{prefix}_{cat}"] = n
    return m


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]


def results_csv(results: list[Result]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "expect", "route_expected", "route", "outcome", "reason", "category",
                "raw_category", "flags", "retrieval_hit", "question", "answer", "draft",
                "problems"])  # fmt: skip
    for r in results:
        w.writerow([
            r.item.id, r.item.expect, r.item.route, r.answer.route, r.answer.outcome,
            r.answer.reason or "", r.final.category, r.raw.category,
            " ".join(r.final.flags), r.retrieval_hit, r.item.question, r.answer.text,
            r.answer.draft or "", "; ".join(r.answer.problems),
        ])  # fmt: skip
    return buf.getvalue()


def report_markdown(customer: str, results: list[Result], m: Mapping[str, float]) -> str:
    cats = ["correct", "over_refusal", "incomplete", *WRONG, "missed_refusal"]
    lines = [
        f"# Callback Q&A hallucination report: {customer}",
        "",
        f"{int(m['questions'])} golden questions ({int(m['answerable'])} answerable).",
        "",
        "| Metric | Value |",
        "|---|---|",
        *(f"| {k} | {m[k]:.1%} |" for k in (
            "answer_accuracy", "refusal_accuracy", "over_refusal_rate", "hallucination_rate",
            "raw_hallucination_rate", "faithfulness", "raw_faithfulness", "routing_accuracy",
            "retrieval_hit_rate", "language_accuracy")),
        f"| latency p50 / p95 | {m['latency_p50_s']:.1f}s / {m['latency_p95_s']:.1f}s |",
        "",
        "## Categories: what the caller heard vs the raw draft",
        "",
        "| Category | Heard | Raw draft |",
        "|---|---|---|",
        *(f"| {c} | {int(m.get(f'final_{c}', 0))} | {int(m.get(f'raw_{c}', 0))} |"
          for c in cats),
        "",
        "## Questions not scored correct",
        "",
        "| ID | Category | Route | Question | Heard | Draft |",
        "|---|---|---|---|---|---|",
    ]  # fmt: skip
    for r in results:
        if r.final.category != "correct":
            cells = [r.item.id, r.final.category, r.answer.route, r.item.question,
                     r.answer.text, r.answer.draft or ""]  # fmt: skip
            lines.append("| " + " | ".join(c.replace("|", "/").replace("\n", " ")
                                           for c in cells) + " |")  # fmt: skip
    return "\n".join(lines) + "\n"


def run(
    agent: CallbackAgent, golden: GoldenSet, docs: Documents,
    log: Callable[[str], None] = print,
) -> list[Result]:  # fmt: skip
    results = []
    for i, item in enumerate(golden.items, 1):
        results.append(evaluate(item, agent.ask(golden.customer, item.question), docs))
        if i % 10 == 0 or i == len(golden.items):
            ok = sum(r.final.category == "correct" for r in results)
            log(f"  {i}/{len(golden.items)} questions, {ok} correct")
    return results


def documents(customer: str, customers: Mapping[str, Any]) -> Documents:
    from caregap.rag.documents import read_pdf

    text = {c: read_pdf(cfg.benefits_document) for c, cfg in customers.items()}
    others = [c for c in customers if c != customer]
    return Documents(
        own=amounts(text[customer]),
        others=set().union(*(amounts(text[c]) for c in others)),
        other_names=tuple(customers[c].plan_name for c in others),
    )


def _git_commit() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
                             capture_output=True, text=True, check=True)  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def main(argv: list[str] | None = None) -> None:
    import mlflow

    from caregap.config import get_settings
    from caregap.customers import load_all
    from caregap.llm.embeddings import LangChainEmbeddings
    from caregap.llm.factory import build_client, build_embeddings
    from caregap.prompts import MlflowPromptStore
    from caregap.rag.store import BenefitRetriever, ChunkStore

    ap = argparse.ArgumentParser(prog="python -m caregap.evals.rag")
    ap.add_argument("--customer", default="sunrise")
    ap.add_argument("-k", type=int, default=8)
    args = ap.parse_args(argv)

    settings = get_settings()
    customers = load_all(settings.customers_dir)
    golden = load_golden(args.customer)
    client = build_client(settings)
    embed = build_embeddings(settings)
    emb = LangChainEmbeddings(embed, purpose="eval-rag")
    chunks = ChunkStore(settings.database_url)
    agent = CallbackAgent(
        client, MlflowPromptStore(settings.mlflow_tracking_uri), customers,
        lambda c: BenefitRetriever(store=chunks, embeddings=emb, customer=c, k=args.k),
        max_output_tokens=2000 if settings.llm_provider == "claude" else 300,
    )  # fmt: skip
    model = f"{client.provider.name}:{client.model}"
    print(f"[eval-rag] {args.customer}: {len(golden.items)} questions, {model}, "
          f"embeddings {embed.model}, k={args.k}")  # fmt: skip
    results = run(agent, golden, documents(args.customer, customers))
    metrics = summarize(results)
    report = report_markdown(args.customer, results, metrics)

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    versions = {k: v for r in results for k, v in r.answer.prompt_versions.items()}
    with mlflow.start_run(run_name=f"{args.customer} {client.provider.name}:{client.model}") as mr:
        mlflow.log_params({
            "customer": args.customer, "provider": client.provider.name,
            "model": client.model, "embed_model": embed.model, "k": args.k,
            "prompt_versions": json.dumps(versions, sort_keys=True),
            "golden_questions": len(golden.items),
        })  # fmt: skip
        mlflow.set_tags({"git_commit": _git_commit(), "suite": "rag"})
        mlflow.log_metrics(metrics)
        mlflow.log_text(report, "report.md")
        mlflow.log_text(results_csv(results), "results.csv")
        mlflow.log_text(json.dumps(metrics, indent=2, sort_keys=True), "summary.json")
        run_id = mr.info.run_id
    print(report)
    print(f"[eval-rag] MLflow run {run_id} in experiment {EXPERIMENT!r}")


if __name__ == "__main__":
    main()
