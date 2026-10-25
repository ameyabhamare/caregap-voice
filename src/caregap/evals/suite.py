"""The outreach-script eval suite: one MLflow run per prompt version, promote on improvement.

    make eval                      # production prompts (records the baseline)
    make eval ALIAS=candidate      # candidate prompts: compare, promote if better

Per configuration (Sunrise, Sunrise Spanish-only, Horizon) it generates scripts with the
rule eval's sampling and flow, then judges every final script (clarity, tone, no medical
advice) and computes reading level. The parent run logs the means over configurations.

Promotion rule: the mean final compliance pass rate must improve and no guard metric may
drop by more than ``GUARD_TOLERANCE``. Judge metrics (pass rates, medical-advice
failures) only block for criteria in ``judge.TRUSTED_CRITERIA``, i.e. calibrated against
owner labels; otherwise they are reported only. A candidate that fails is not promoted
and its regressions are listed.

A configuration whose inputs did not change since an earlier run (same prompt versions
for that customer, judge version, model, sample, compliance code and customer config) is
reused from that run instead of regenerated; the run says which.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from caregap.customers import CustomerConfig
from caregap.evals import rules
from caregap.evals.judge import CRITERIA, TRUSTED_CRITERIA, Judge, Verdict
from caregap.evals.readability import MAX_GRADE, fk_grade
from caregap.outreach.generate import ScriptGenerator, ScriptOutcome

EXPERIMENT = "eval-suite"
JUDGE_PROMPT = "outreach-judge"
GUARD_TOLERANCE = 0.03
PRIMARY = "final_pass_rate"
GUARDS = (
    "first_attempt_pass_rate",
    "judge_clarity_pass_rate",
    "judge_tone_pass_rate",
    "judge_no_medical_advice_pass_rate",
    "reading_ok_rate",
)
#: Code whose changes invalidate cached configuration results.
CODE = (Path("src/caregap/outreach/compliance.py"), Path("src/caregap/outreach/generate.py"),
        Path("src/caregap/evals/rules.py"), Path("src/caregap/evals/judge.py"))  # fmt: skip


@dataclass(frozen=True)
class SuiteConfig:
    name: str
    customer: str
    language: str | None
    n: int


CONFIGS = (
    SuiteConfig("sunrise", "sunrise", None, 100),
    SuiteConfig("sunrise-es", "sunrise", "es", 30),
    SuiteConfig("horizon", "horizon", None, 100),
)


def script_metrics(
    outcomes: Sequence[ScriptOutcome], verdicts: Sequence[Verdict]
) -> dict[str, float]:
    """Judge and reading-level metrics over the final scripts."""
    n = len(outcomes)
    m: dict[str, float] = {}
    if not n:
        return m
    for c in CRITERIA:
        m[f"judge_{c}_pass_rate"] = sum(v.scores[c] is True for v in verdicts) / n
        m[f"judge_{c}_fails"] = sum(v.scores[c] is False for v in verdicts)
    m["judge_all_pass_rate"] = sum(v.passed for v in verdicts) / n
    m["judge_unparsed"] = sum(any(v.scores[c] is None for c in CRITERIA) for v in verdicts)
    english = [o.script for o in outcomes if o.language == "en"]
    if english:
        grades = [fk_grade(s) for s in english]
        m["reading_grade_mean"] = statistics.fmean(grades)
        m["reading_ok_rate"] = sum(g <= MAX_GRADE for g in grades) / len(grades)
    return m


def run_config(
    cases: list[rules.EvalCase],
    generators: Mapping[str, ScriptGenerator],
    judge: Judge,
    members: rules.MemberSource,
    log: Callable[[str], None] = print,
) -> tuple[list[ScriptOutcome], list[Verdict], dict[str, float]]:
    outcomes, errors = rules.run(generators, members, cases, log)
    verdicts = [judge.judge(o.script, purpose=f"judge:{o.member_id}") for o in outcomes]
    metrics = rules.summarize(outcomes, errors)
    metrics.update(script_metrics(outcomes, verdicts))
    return outcomes, verdicts, metrics


def aggregate(per_config: Mapping[str, Mapping[str, float]]) -> dict[str, float]:
    """Means over configurations (each configuration weighs the same), plus summed
    medical-advice failures."""
    out: dict[str, float] = {}
    for key in (PRIMARY, *GUARDS, "judge_all_pass_rate"):
        values = [m[key] for m in per_config.values() if key in m]
        if values:
            out[key] = statistics.fmean(values)
    out["judge_no_medical_advice_fails"] = sum(
        m.get("judge_no_medical_advice_fails", 0) for m in per_config.values()
    )
    return out


@dataclass(frozen=True)
class Decision:
    promote: bool
    improved: bool
    regressions: tuple[str, ...]
    deltas: dict[str, float]


def decide(
    candidate: Mapping[str, float],
    baseline: Mapping[str, float],
    trusted: Sequence[str] = TRUSTED_CRITERIA,
) -> Decision:
    """Judge metrics block a promotion only for ``trusted`` (calibrated) criteria; the
    others are still compared and reported in ``deltas``."""
    deltas = {k: candidate[k] - baseline[k] for k in (PRIMARY, *GUARDS)
              if k in candidate and k in baseline}  # fmt: skip
    blocking = [
        k for k in GUARDS
        if not k.startswith("judge_") or k.removeprefix("judge_").removesuffix("_pass_rate")
        in trusted
    ]  # fmt: skip
    regressions = [
        f"{k} {baseline[k]:.1%} -> {candidate[k]:.1%}"
        for k in blocking
        if k in deltas and deltas[k] < -GUARD_TOLERANCE
    ]
    med_b = baseline.get("judge_no_medical_advice_fails", 0)
    med_c = candidate.get("judge_no_medical_advice_fails", 0)
    if "no_medical_advice" in trusted and med_c > med_b:
        regressions.append(f"judged medical-advice failures {med_b:.0f} -> {med_c:.0f}")
    improved = deltas.get(PRIMARY, 0.0) > 0
    return Decision(improved and not regressions, improved, tuple(regressions), deltas)


def cache_key(config: SuiteConfig, customer: CustomerConfig, prompt_versions: Mapping[str, str],
              model: str) -> str:  # fmt: skip
    code = b"".join(p.read_bytes() for p in CODE)
    blob = json.dumps(
        {"config": vars(config), "customer": customer.model_dump(mode="json"),
         "prompts": dict(sorted(prompt_versions.items())), "model": model,
         "code": hashlib.sha256(code).hexdigest()},
        sort_keys=True,
    )  # fmt: skip
    return hashlib.sha256(blob.encode()).hexdigest()[:24]


def config_prompts(customer: CustomerConfig, language: str | None) -> list[str]:
    langs = [language] if language else list(customer.languages)
    names = {JUDGE_PROMPT}
    for lang in langs:
        names |= {customer.languages[lang].script_prompt, customer.languages[lang].revise_prompt}
    return sorted(names)


def main(argv: list[str] | None = None) -> int:
    import mlflow

    from caregap.config import get_settings
    from caregap.customers import load_all
    from caregap.llm.factory import build_client
    from caregap.outreach.store import OutreachStore
    from caregap.prompts import MlflowPromptStore, promote, versions

    ap = argparse.ArgumentParser(prog="python -m caregap.evals.suite")
    ap.add_argument("--alias", default="production")
    ap.add_argument("--year", type=int, default=2025)
    ap.add_argument("--no-promote", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args(argv)

    settings = get_settings()
    customers = load_all(settings.customers_dir)
    client = build_client(settings)
    prompts = MlflowPromptStore(settings.mlflow_tracking_uri, alias=args.alias)
    judge = Judge(client, prompts)
    members = OutreachStore(settings.database_url)
    max_tokens = 4000 if settings.llm_provider == "claude" else 600
    model = f"{client.provider.name}:{client.model}"
    mlflow.set_tracking_uri(settings.mlflow_tracking_uri)
    mlflow.set_experiment(EXPERIMENT)

    all_versions: dict[str, str] = {}
    per_config: dict[str, dict[str, float]] = {}
    with mlflow.start_run(run_name=f"suite {args.alias}") as parent:
        for cfg in CONFIGS:
            customer = customers[cfg.customer]
            pv = versions(settings.mlflow_tracking_uri, args.alias,
                          config_prompts(customer, cfg.language))  # fmt: skip
            all_versions.update(pv)
            key = cache_key(cfg, customer, pv, model)
            cached = None if args.no_cache else mlflow.search_runs(
                experiment_names=[EXPERIMENT],
                filter_string=f"tags.cache_key = '{key}' and tags.cached = 'false'",
                output_format="list", max_results=1,
            )  # fmt: skip
            with mlflow.start_run(run_name=cfg.name, nested=True):
                mlflow.set_tags({"cache_key": key, "config": cfg.name})
                mlflow.log_params({"customer": cfg.customer, "language": cfg.language or "all",
                                   "n": cfg.n, "model": model,
                                   "prompt_versions": json.dumps(pv, sort_keys=True)})  # fmt: skip
                if cached:
                    metrics = {k: float(v) for k, v in cached[0].data.metrics.items()}
                    mlflow.set_tags({"cached": "true", "cached_from": cached[0].info.run_id})
                    reused = cached[0].info.run_id[:8]
                    print(f"[eval] {cfg.name}: inputs unchanged, reusing {reused}")
                else:
                    gens = {
                        lang: ScriptGenerator(
                            client, prompts, customer.profile(lang), max_output_tokens=max_tokens
                        )
                        for lang in customer.languages
                    }
                    cases = rules.sample_cases(settings.database_url, customer, args.year,
                                               cfg.n, cfg.language)  # fmt: skip
                    print(f"[eval] {cfg.name}: {len(cases)} members")
                    outcomes, verdicts, metrics = run_config(cases, gens, judge, members)
                    mlflow.set_tag("cached", "false")
                    mlflow.log_text(rules.results_csv(outcomes), "results.csv")
                    mlflow.log_text(judge_csv(outcomes, verdicts), "judge.csv")
                mlflow.log_metrics(metrics)
                per_config[cfg.name] = metrics
        summary = aggregate(per_config)
        mlflow.log_metrics(summary)
        versions_json = json.dumps(all_versions, sort_keys=True)
        mlflow.log_params({"alias": args.alias, "model": model, "prompt_versions": versions_json})
        baseline = _latest_promoted(mlflow, parent.info.run_id)
        promoted = args.alias == "production"
        if baseline is None or args.alias == "production":
            print("[eval] production run: recorded as the baseline")
        else:
            d = decide(summary, baseline.data.metrics)
            mlflow.log_dict({"baseline_run": baseline.info.run_id, "promote": d.promote,
                             "improved": d.improved, "regressions": list(d.regressions),
                             "deltas": d.deltas}, "decision.json")  # fmt: skip
            mlflow.set_tags({"decision": "promote" if d.promote else "reject",
                             "regressions": "; ".join(d.regressions) or "none"})  # fmt: skip
            for k, v in d.deltas.items():
                print(f"[eval] {k}: {baseline.data.metrics[k]:.1%} -> {summary[k]:.1%} ({v:+.1%})")
            for r in d.regressions:
                print(f"[eval] REGRESSION: {r}")
            if d.promote and not args.no_promote:
                for line in promote(settings.mlflow_tracking_uri, args.alias):
                    print(f"[eval] {line}")
                promoted = True
            print(f"[eval] decision: {'PROMOTE' if d.promote else 'REJECT'}"
                  + ("" if d.improved else f" ({PRIMARY} did not improve)"))  # fmt: skip
        mlflow.set_tag("promoted", str(promoted).lower())
        print(f"[eval] MLflow run {parent.info.run_id} in experiment {EXPERIMENT!r}")
    return 0


def _latest_promoted(mlflow: Any, exclude: str) -> Any:
    runs = mlflow.search_runs(
        experiment_names=[EXPERIMENT], filter_string="tags.promoted = 'true'",
        order_by=["start_time DESC"], output_format="list", max_results=5,
    )  # fmt: skip
    return next((r for r in runs if r.info.run_id != exclude), None)


def judge_csv(outcomes: Sequence[ScriptOutcome], verdicts: Sequence[Verdict]) -> str:
    import csv
    import io

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["member_id", "language", "status", *CRITERIA, "reading_grade", "reason",
                "script"])  # fmt: skip
    for o, v in zip(outcomes, verdicts, strict=True):
        grade = fk_grade(o.script) if o.language == "en" else ""
        scores = [v.scores[c] for c in CRITERIA]
        w.writerow([o.member_id, o.language, o.status, *scores, grade, v.reason, o.script])
    return buf.getvalue()


if __name__ == "__main__":
    raise SystemExit(main())
