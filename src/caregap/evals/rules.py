"""Rule-based eval: compliance pass rates for generated scripts, logged to MLflow.

    make eval-rules N=100

Members are a deterministic sample (ordered by a hash of the member ID) of members with
at least one open gap. Each goes through the same generate -> check -> revise flow as
the service, without writing to the review queue.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import statistics
import subprocess
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Protocol

import psycopg

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


def sample_cases(conninfo: str, year: int, n: int) -> list[EvalCase]:
    with psycopg.connect(conninfo) as conn:
        rows = conn.execute(
            """
            SELECT member_id, array_agg(measure ORDER BY measure)
            FROM care_gaps WHERE measurement_year = %s AND status = 'gap'
            GROUP BY member_id ORDER BY md5(member_id) LIMIT %s
            """,
            (year, n),
        ).fetchall()
    return [EvalCase(member_id, list(measures)) for member_id, measures in rows]


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
    return metrics


def results_csv(outcomes: Iterable[ScriptOutcome]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["member_id", "measures", "status", "attempts", "first_attempt_rules",
                     "final_rules", "words", "script"])  # fmt: skip
    for o in outcomes:
        writer.writerow([
            o.member_id, " ".join(o.measures), o.status, len(o.attempts),
            " ".join(o.attempts[0].compliance.rules), " ".join(o.final.rules),
            len(o.script.split()), o.script,
        ])  # fmt: skip
    return buf.getvalue()


def run(
    generator: ScriptGenerator,
    store: MemberSource,
    cases: list[EvalCase],
    log: Callable[[str], None] = print,
) -> tuple[list[ScriptOutcome], int]:
    outcomes: list[ScriptOutcome] = []
    errors = 0
    for i, case in enumerate(cases, 1):
        try:
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
                "prompt_version": versions.get("outreach-script", "?"),
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
    from caregap.llm.factory import build_client
    from caregap.outreach.profile import DEFAULT_PROFILE
    from caregap.prompts import MlflowPromptStore

    parser = argparse.ArgumentParser(prog="python -m caregap.evals.rules")
    parser.add_argument("-n", type=int, default=100)
    parser.add_argument("--year", type=int, default=2025)
    args = parser.parse_args(argv)

    settings = get_settings()
    generator = ScriptGenerator(
        build_client(settings),
        MlflowPromptStore(settings.mlflow_tracking_uri),
        DEFAULT_PROFILE,
        max_output_tokens=4000 if settings.llm_provider == "claude" else 600,
    )
    store = OutreachStore(settings.database_url)
    cases = sample_cases(settings.database_url, args.year, args.n)
    llm = generator.client
    print(f"[eval-rules] {len(cases)} members, {llm.provider.name}:{llm.model}")
    outcomes, errors = run(generator, store, cases)
    metrics = summarize(outcomes, errors)
    run_id = log_to_mlflow(
        settings.mlflow_tracking_uri, generator, outcomes, metrics,
        {"n_requested": args.n, "measurement_year": args.year},
    )  # fmt: skip
    for key in ("first_attempt_pass_rate", "final_pass_rate", "review_rate", "errors"):
        print(f"[eval-rules] {key}: {metrics.get(key, 0):.3f}")
    print(f"[eval-rules] MLflow run {run_id} in experiment {EXPERIMENT!r}")


if __name__ == "__main__":
    main()
