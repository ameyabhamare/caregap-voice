"""Rule-based eval: compliance pass rates for generated scripts, logged to MLflow.

    make eval-rules N=100 CUSTOMER=sunrise [LANGUAGE=es]

Members are a deterministic sample (ordered by a hash of the member ID) of the customer's
members with at least one open gap in the customer's measures, optionally only members
whose script language is LANGUAGE. Each goes through the same generate -> check ->
revise flow as the service, in the member's language, without writing to the review
queue.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

from caregap.customers import CustomerConfig
from caregap.llm.base import ProviderError
from caregap.outreach.compliance import MemberContext
from caregap.outreach.generate import COMPLIANT, ScriptGenerator, ScriptOutcome
from caregap.outreach.store import OutreachStore

EXPERIMENT = "rule-evals"


class MemberSource(Protocol):
    def member(self, member_id: str) -> MemberContext: ...


@dataclass(frozen=True)
class EvalCase:
    member_id: str
    open_measures: list[str]
    #: Script language (the member's, if the customer supports it).
    language: str = "en"


def sample_cases(
    conninfo: str, customer: CustomerConfig, year: int, n: int, language: str | None = None
) -> list[EvalCase]:
    lo, hi = customer.members.bounds()
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute(
            """
            SELECT g.member_id, array_agg(g.measure ORDER BY g.measure), m.language
            FROM care_gaps g JOIN members m USING (member_id)
            WHERE g.measurement_year = %(year)s AND g.status = 'gap'
              AND g.measure = ANY(%(measures)s)
              AND date_part('year', age(make_date(%(year)s, 12, 31), m.birth_date))
                  BETWEEN %(lo)s AND %(hi)s
            GROUP BY g.member_id, m.language ORDER BY md5(g.member_id)
            """,
            {"year": year, "measures": list(customer.measures), "lo": lo, "hi": hi},
        ).fetchall()
    cases = [
        EvalCase(member_id, list(measures), customer.language_for(lang))
        for member_id, measures, lang in rows
    ]
    return [c for c in cases if language in (None, c.language)][:n]


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, round(q * (len(ordered) - 1)))]


def summarize(outcomes: list[ScriptOutcome], errors: int = 0) -> dict[str, float]:
    """Aggregate metrics. Rates are over scripts that produced an outcome."""
    n = len(outcomes)
    metrics: dict[str, float] = {"scripts": n, "errors": errors}
    if n == 0:
        return metrics
    first_pass = sum(o.attempts[0].compliance.passed for o in outcomes)
    final_pass = sum(o.status == COMPLIANT for o in outcomes)
    calls = [a for o in outcomes for a in o.attempts]
    latencies = [a.latency_s for a in calls]
    words = [len(o.script.split()) for o in outcomes]
    metrics.update(
        first_attempt_pass_rate=first_pass / n,
        final_pass_rate=final_pass / n,
        revised_rate=sum(len(o.attempts) > 1 for o in outcomes) / n,
        review_rate=(n - final_pass) / n,
        model_calls=len(calls),
        words_mean=statistics.fmean(words),
        words_p95=_percentile([float(w) for w in words], 0.95),
        latency_p50_s=_percentile(latencies, 0.50),
        latency_p95_s=_percentile(latencies, 0.95),
        output_tokens_mean=statistics.fmean(a.output_tokens for a in calls),
    )
    first_rules = Counter(r for o in outcomes for r in o.attempts[0].compliance.rules)
    final_rules = Counter(r for o in outcomes for r in o.final.rules)
    for rule, count in first_rules.items():
        metrics[f"first_attempt_fail_{rule}"] = count
    for rule, count in final_rules.items():
        metrics[f"final_fail_{rule}"] = count
    languages = Counter(o.language for o in outcomes)
    if len(languages) > 1:
        for lang, count in languages.items():
            mine = [o for o in outcomes if o.language == lang]
            metrics[f"scripts_{lang}"] = count
            metrics[f"final_pass_rate_{lang}"] = sum(o.status == COMPLIANT for o in mine) / count
    return metrics


def results_csv(outcomes: Iterable[ScriptOutcome]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["member_id", "language", "measures", "status", "attempts",
                     "first_attempt_rules", "final_rules", "words", "script"])  # fmt: skip
    for o in outcomes:
        writer.writerow([
            o.member_id, o.language, " ".join(o.measures), o.status, len(o.attempts),
            " ".join(o.attempts[0].compliance.rules), " ".join(o.final.rules),
            len(o.script.split()), o.script,
        ])  # fmt: skip
    return buf.getvalue()


def run(
    generators: Mapping[str, ScriptGenerator],
    store: MemberSource,
    cases: list[EvalCase],
    log: Callable[[str], None] = print,
) -> tuple[list[ScriptOutcome], int]:
    outcomes: list[ScriptOutcome] = []
    errors = 0
    for i, case in enumerate(cases, 1):
        try:
            generator = generators[case.language]
            outcomes.append(generator.generate(store.member(case.member_id), case.open_measures))
        except ProviderError as exc:
            errors += 1
            log(f"  {case.member_id}: provider error: {exc}")
        if i % 10 == 0 or i == len(cases):
            passed = sum(o.status == COMPLIANT for o in outcomes)
            log(f"  {i}/{len(cases)} scripts, {passed} compliant, {errors} errors")
    return outcomes, errors


def _git_commit() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True, text=True, check=True,
        )  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def log_to_mlflow(
    tracking_uri: str,
    generator: ScriptGenerator,
    customer: str,
    outcomes: list[ScriptOutcome],
    metrics: dict[str, float],
    params: dict[str, Any],
) -> str:
    import mlflow

    mlflow.set_tracking_uri(tracking_uri)
    mlflow.set_experiment(EXPERIMENT)
    versions = outcomes[0].prompt_versions if outcomes else {}
    run_name = f"{generator.client.provider.name}:{generator.client.model}"
    with mlflow.start_run(run_name=run_name) as run:
        mlflow.log_params(
            {
                **params,
                "provider": generator.client.provider.name,
                "model": generator.client.model,
                "prompt_version": versions.get(generator.profile.script_prompt, "?"),
                "prompt_versions": json.dumps(
                    {k: v for o in outcomes for k, v in o.prompt_versions.items()},
                    sort_keys=True,
                ),
                "customer": customer,
                "plan": generator.profile.plan_name,
                "max_words": generator.profile.max_words,
                "max_output_tokens": generator.max_output_tokens,
            }
        )
        mlflow.set_tags({"git_commit": _git_commit(), "suite": "rules"})
        mlflow.log_metrics(metrics)
        mlflow.log_text(results_csv(outcomes), "results.csv")
        mlflow.log_text(json.dumps(metrics, indent=2, sort_keys=True), "summary.json")
        return str(run.info.run_id)


def main(argv: list[str] | None = None) -> None:
    from caregap.config import get_settings
    from caregap.customers import load_all
    from caregap.llm.factory import build_client
    from caregap.prompts import MlflowPromptStore

    parser = argparse.ArgumentParser(prog="python -m caregap.evals.rules")
    parser.add_argument("-n", type=int, default=100)
    parser.add_argument("--year", type=int, default=2025)
    parser.add_argument("--customer", default="sunrise")
    parser.add_argument("--language", default=None, help="only members scripted in this language")
    args = parser.parse_args(argv)

    settings = get_settings()
    customer = load_all(settings.customers_dir)[args.customer]
    client = build_client(settings)
    prompts = MlflowPromptStore(settings.mlflow_tracking_uri)
    max_tokens = 4000 if settings.llm_provider == "claude" else 600
    generators = {
        lang: ScriptGenerator(client, prompts, customer.profile(lang), max_output_tokens=max_tokens)
        for lang in customer.languages
    }
    store = OutreachStore(settings.database_url)
    cases = sample_cases(settings.database_url, customer, args.year, args.n, args.language)
    langs = dict(Counter(c.language for c in cases))
    print(
        f"[eval-rules] {customer.id}: {len(cases)} members {langs}, "
        f"{client.provider.name}:{client.model}"
    )
    outcomes, errors = run(generators, store, cases)
    metrics = summarize(outcomes, errors)
    main_lang = args.language or customer.default_language
    run_id = log_to_mlflow(
        settings.mlflow_tracking_uri, generators[main_lang], customer.id, outcomes, metrics,
        {"n_requested": args.n, "measurement_year": args.year,
         "language_filter": args.language or "all", "languages": json.dumps(langs)},
    )  # fmt: skip
    for key in ("first_attempt_pass_rate", "final_pass_rate", "review_rate", "errors"):
        print(f"[eval-rules] {key}: {metrics.get(key, 0):.3f}")
    print(f"[eval-rules] MLflow run {run_id} in experiment {EXPERIMENT!r}")


if __name__ == "__main__":
    main()
