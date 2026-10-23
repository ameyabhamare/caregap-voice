"""Fixture-based eval gate for CI, and the per-customer eval report.

    make record-fixtures     # dev time, live models: record everything CI will replay
    make eval-ci             # CI: replay, check thresholds, write reports/

``record`` samples the suite's members from the database, saves them to
``fixtures/ci/cases/``, and runs the script suite (rules + judge + reading level) and the
callback Q&A eval with ``LLM_FIXTURES=record``, so every model and embedding response
lands in ``fixtures/ci/``. It writes the metrics it saw and thresholds derived from them
(``fixtures/ci/thresholds.json``).

``check`` runs the same evals with ``LLM_FIXTURES=replay`` from those fixtures alone (no
member database, no MLflow, no model) and fails if any gated metric is below its
threshold. A missing recording means the requests changed (a prompt, a member, the code
that builds requests): re-record and review the new numbers.

Both write ``reports/<customer>.md`` and ``.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import Any

from caregap.config import Settings, get_settings
from caregap.customers import CustomerConfig, load_all
from caregap.evals import rag as rag_eval
from caregap.evals import rules
from caregap.evals.judge import Judge
from caregap.evals.suite import CONFIGS, run_config
from caregap.outreach.compliance import MemberContext
from caregap.outreach.generate import ScriptGenerator
from caregap.prompts import FilePromptStore

FIXTURES = Path("fixtures/ci")
CASES = FIXTURES / "cases"
THRESHOLDS = FIXTURES / "thresholds.json"
REPORTS = Path("reports")
MARGIN = 0.05
#: Judge criteria gate CI only once their agreement with owner labels is good
#: (kappa >= 0.4, make judge-agreement). Until then they are reported, not gated.
GATED_JUDGE_CRITERIA: tuple[str, ...] = ()


class FixtureMembers:
    def __init__(self, members: Mapping[str, MemberContext]) -> None:
        self.members = members

    def member(self, member_id: str) -> MemberContext:
        return self.members[member_id]


def _member_json(m: MemberContext) -> dict[str, Any]:
    d = asdict(m)
    d["birth_date"] = m.birth_date.isoformat()
    return d


def save_cases(name: str, cases: list[rules.EvalCase], members: rules.MemberSource) -> None:
    CASES.mkdir(parents=True, exist_ok=True)
    rows = [{"member": _member_json(members.member(c.member_id)),
             "open_measures": c.open_measures, "language": c.language} for c in cases]  # fmt: skip
    (CASES / f"{name}.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")


def load_cases(name: str) -> tuple[list[rules.EvalCase], FixtureMembers]:
    rows = json.loads((CASES / f"{name}.json").read_text())
    members = {}
    cases = []
    for r in rows:
        m = dict(r["member"])
        m["birth_date"] = date.fromisoformat(m["birth_date"])
        members[m["member_id"]] = MemberContext(**m)
        cases.append(rules.EvalCase(m["member_id"], r["open_measures"], r["language"]))
    return cases, FixtureMembers(members)


def run_all(
    settings: Settings, customers: Mapping[str, CustomerConfig], *, live_cases: bool
) -> dict[str, dict[str, float]]:
    """Script suite + callback Q&A for every configuration and customer."""
    import psycopg

    from caregap.db import apply_schema
    from caregap.llm.embeddings import LangChainEmbeddings
    from caregap.llm.factory import build_client, build_embeddings
    from caregap.outreach.store import OutreachStore
    from caregap.rag.agent import CallbackAgent, token_limits
    from caregap.rag.store import BenefitRetriever, ChunkStore, ingest

    with psycopg.connect(settings.database_url) as conn:
        apply_schema(conn)  # CI starts from an empty Postgres
    client = build_client(settings)
    prompts = FilePromptStore()  # CI has no MLflow; the files are the production prompts
    judge = Judge(client, prompts)
    max_tokens = 4000 if settings.llm_provider == "claude" else 600
    metrics: dict[str, dict[str, float]] = {}
    for cfg in CONFIGS:
        customer = customers[cfg.customer]
        if live_cases:
            cases = rules.sample_cases(settings.database_url, customer, 2025, cfg.n, cfg.language)
            store: rules.MemberSource = OutreachStore(settings.database_url)
            save_cases(cfg.name, cases, store)
        else:
            cases, store = load_cases(cfg.name)
        gens = {lang: ScriptGenerator(client, prompts, customer.profile(lang),
                                      max_output_tokens=max_tokens)
                for lang in customer.languages}  # fmt: skip
        print(f"[eval-ci] scripts {cfg.name}: {len(cases)} members")
        _, _, metrics[f"scripts:{cfg.name}"] = run_config(cases, gens, judge, store)

    embed = build_embeddings(settings)
    emb = LangChainEmbeddings(embed, purpose="eval-ci")
    chunks = ChunkStore(settings.database_url)
    ingest(customers, chunks, emb, embed.model)
    agent = CallbackAgent(
        client, prompts, customers,
        lambda c: BenefitRetriever(store=chunks, embeddings=emb, customer=c),
        **token_limits(settings.llm_provider),
    )  # fmt: skip
    for cid in customers:
        golden = rag_eval.load_golden(cid)
        print(f"[eval-ci] callback Q&A {cid}: {len(golden.items)} questions")
        results = rag_eval.run(agent, golden, rag_eval.documents(cid, customers),
                               log=lambda _: None)  # fmt: skip
        metrics[f"rag:{cid}"] = rag_eval.summarize(results)
    return metrics


def derive_thresholds(metrics: Mapping[str, Mapping[str, float]]) -> dict[str, dict[str, Any]]:
    """Minimums (and one maximum) per gated metric, from a recorded run."""
    out: dict[str, dict[str, Any]] = {}
    for key, m in metrics.items():
        gates: dict[str, Any] = {}
        if key.startswith("scripts:"):
            gates["final_pass_rate"] = {"min": round(max(0.0, m["final_pass_rate"] - MARGIN), 3)}
            for c in GATED_JUDGE_CRITERIA:
                k = f"judge_{c}_pass_rate"
                gates[k] = {"min": round(max(0.0, m[k] - MARGIN), 3)}
        else:
            gates["hallucination_rate"] = {"max": 0.0}
            gates["refusal_accuracy"] = {"min": 0.95}
            gates["answer_accuracy"] = {"min": round(max(0.0, m["answer_accuracy"] - MARGIN), 3)}
        out[key] = gates
    return out


def check(
    metrics: Mapping[str, Mapping[str, float]], thresholds: Mapping[str, Mapping[str, Any]]
) -> list[str]:
    failures = []
    for key, gates in thresholds.items():
        for metric, rule in gates.items():
            value = metrics.get(key, {}).get(metric)
            if value is None:
                failures.append(f"{key} {metric}: missing")
            elif "min" in rule and value < rule["min"]:
                failures.append(f"{key} {metric}: {value:.3f} < {rule['min']}")
            elif "max" in rule and value > rule["max"]:
                failures.append(f"{key} {metric}: {value:.3f} > {rule['max']}")
    return failures


_ROWS = (
    ("Rule compliance, first attempt", "first_attempt_pass_rate"),
    ("Rule compliance, final", "final_pass_rate"),
    ("Judge: clarity", "judge_clarity_pass_rate"),
    ("Judge: tone", "judge_tone_pass_rate"),
    ("Judge: no medical advice", "judge_no_medical_advice_pass_rate"),
    ("Reading level grade 8 or below (English)", "reading_ok_rate"),
)
_RAG_ROWS = (
    ("Answerable questions answered correctly", "answer_accuracy"),
    ("Should-refuse questions refused", "refusal_accuracy"),
    ("Over-refusal", "over_refusal_rate"),
    ("Spoken answers with a wrong amount or plan", "hallucination_rate"),
    ("Raw drafts with one", "raw_hallucination_rate"),
)


def write_reports(
    metrics: Mapping[str, Mapping[str, float]], customers: Mapping[str, CustomerConfig],
    *, source: str, extra: Mapping[str, Mapping[str, float]] | None = None,
) -> None:  # fmt: skip
    REPORTS.mkdir(exist_ok=True)
    for cid, cfg in customers.items():
        scripts = {
            name: v
            for k, v in metrics.items()
            if k.startswith("scripts:") and CONFIGS_BY_NAME[name := k.split(":", 1)[1]] == cid
        }
        callback = metrics.get(f"rag:{cid}", {})
        data = {"customer": cid, "plan": cfg.plan_name, "source": source,
                "scripts": scripts, "callback_qa": callback,
                **({"voice": extra[cid]} if extra and cid in extra else {})}  # fmt: skip
        (REPORTS / f"{cid}.json").write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
        lines = [f"# Eval report: {cfg.plan_name}", "", f"Source: {source}.", ""]
        if scripts:
            names = list(scripts)
            lines += ["## Outreach scripts", "",
                      "| Metric | " + " | ".join(names) + " |",
                      "|---|" + "---|" * len(names)]  # fmt: skip
            for label, key in _ROWS:
                cells = [f"{scripts[n][key]:.0%}" if key in scripts[n] else "n/a" for n in names]
                lines.append(f"| {label} | " + " | ".join(cells) + " |")
            lines.append("| Scripts | " + " | ".join(f"{scripts[n]['scripts']:.0f}" for n in names)
                         + " |")  # fmt: skip
            lines.append("")
        if callback:
            lines += ["## Callback Q&A", "", "| Metric | Value |", "|---|---|"]
            lines += [f"| {label} | {callback[key]:.1%} |" for label, key in _RAG_ROWS]
            lines.append("")
        if extra and cid in extra:
            lines += ["## Voice round trip", "", "| Metric | Value |", "|---|---|"]
            lines += [f"| {k} | {v:.1%} |" for k, v in sorted(extra[cid].items())]
            lines.append("")
        (REPORTS / f"{cid}.md").write_text("\n".join(lines))


CONFIGS_BY_NAME = {c.name: c.customer for c in CONFIGS}


def _voice_metrics(settings: Settings) -> dict[str, dict[str, float]]:
    """Latest voice round-trip metrics from MLflow (Sunrise's English voice)."""
    import mlflow

    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    runs = mlflow.search_runs(experiment_names=["voice-evals"], order_by=["start_time DESC"],
                              output_format="list", max_results=1)  # fmt: skip
    if not runs:
        return {}
    m = runs[0].data.metrics
    keep = {"Script word error rate": "scripts_wer",
            "Medication names, no overrides": "names_plain_medication_accuracy",
            "Medication names, with overrides": "names_overrides_medication_accuracy"}  # fmt: skip
    return {"sunrise": {label: m[k] for label, k in keep.items() if k in m}}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m caregap.evals.ci")
    ap.add_argument("command", choices=["record", "check"])
    args = ap.parse_args(argv)
    mode = "record" if args.command == "record" else "replay"
    settings = get_settings().model_copy(update={"llm_fixtures": mode,
                                                 "llm_fixtures_dir": FIXTURES})  # fmt: skip
    customers = load_all(settings.customers_dir)
    if args.command == "record":
        for name in ("llm.jsonl", "embeddings.jsonl"):
            (FIXTURES / name).unlink(missing_ok=True)  # only what this run needs
        metrics = run_all(settings, customers, live_cases=True)
        (FIXTURES / "recorded_metrics.json").write_text(
            json.dumps(metrics, indent=2, sort_keys=True) + "\n")  # fmt: skip
        THRESHOLDS.write_text(json.dumps(derive_thresholds(metrics), indent=2) + "\n")
        write_reports(metrics, customers, source="recorded run (live models)",
                      extra=_voice_metrics(settings))  # fmt: skip
        print(f"[eval-ci] recorded fixtures and thresholds in {FIXTURES}")
        return 0
    metrics = run_all(settings, customers, live_cases=False)
    write_reports(metrics, customers, source="CI replay of recorded fixtures")
    failures = check(metrics, json.loads(THRESHOLDS.read_text()))
    for f in failures:
        print(f"[eval-ci] FAIL {f}")
    print(f"[eval-ci] {'FAILED' if failures else 'passed'}: "
          f"{sum(len(g) for g in json.loads(THRESHOLDS.read_text()).values())} gates")  # fmt: skip
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
