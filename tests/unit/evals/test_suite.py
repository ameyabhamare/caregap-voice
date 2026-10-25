from __future__ import annotations

from caregap.evals.judge import Verdict
from caregap.evals.suite import (
    CONFIGS,
    aggregate,
    cache_key,
    config_prompts,
    decide,
    script_metrics,
)
from caregap.outreach.compliance import ComplianceResult
from caregap.outreach.generate import Attempt, ScriptOutcome
from tests.helpers import SUNRISE

BASE = {
    "final_pass_rate": 0.60, "first_attempt_pass_rate": 0.30, "judge_clarity_pass_rate": 0.9,
    "judge_tone_pass_rate": 0.9, "judge_no_medical_advice_pass_rate": 0.95,
    "reading_ok_rate": 0.8, "judge_no_medical_advice_fails": 2,
}  # fmt: skip


def test_promote_only_on_improvement_without_regressions() -> None:
    better = {**BASE, "final_pass_rate": 0.70}
    assert decide(better, BASE).promote
    assert not decide(BASE, BASE).promote  # equal is not an improvement
    small_dip = {**better, "judge_tone_pass_rate": 0.88}  # within tolerance
    assert decide(small_dip, BASE).promote
    big_dip = {**better, "reading_ok_rate": 0.70}
    d = decide(big_dip, BASE)
    assert not d.promote and d.improved and d.regressions == ("reading_ok_rate 80.0% -> 70.0%",)
    more_advice = {**better, "judge_no_medical_advice_fails": 3}
    assert decide(more_advice, BASE).promote  # judge not calibrated: reported, not blocking
    trusted = decide(more_advice, BASE, trusted=("no_medical_advice",))
    assert "medical-advice" in trusted.regressions[0] and not trusted.promote


def test_uncalibrated_judge_metrics_are_reported_but_never_block() -> None:
    better = {**BASE, "final_pass_rate": 0.70, "judge_clarity_pass_rate": 0.50}
    d = decide(better, BASE)
    assert d.promote and d.regressions == ()
    assert round(d.deltas["judge_clarity_pass_rate"], 2) == -0.40
    blocked = decide(better, BASE, trusted=("clarity",))
    assert blocked.regressions == ("judge_clarity_pass_rate 90.0% -> 50.0%",)


def outcome(script: str, lang: str = "en") -> ScriptOutcome:
    a = Attempt(script, ComplianceResult(), 1, 1, 0.1)
    return ScriptOutcome("m", ("awv",), lang, "compliant", (a,), {}, "p", "m")


def verdict(*scores: bool | None) -> Verdict:
    return Verdict(dict(zip(("clarity", "tone", "no_medical_advice"), scores, strict=True)),
                   "", "", "1")  # fmt: skip


def test_script_metrics_and_aggregate() -> None:
    outs = [outcome("Hi Ava. Please call your doctor."), outcome("Hola Ava.", "es")]
    m = script_metrics(outs, [verdict(True, True, True), verdict(True, None, False)])
    assert m["judge_all_pass_rate"] == 0.5
    assert (m["judge_tone_pass_rate"], m["judge_unparsed"]) == (0.5, 1)
    assert m["judge_no_medical_advice_fails"] == 1
    assert m["reading_ok_rate"] == 1.0  # English scripts only
    a = {"final_pass_rate": 0.4, "judge_no_medical_advice_fails": 1}
    b = {"final_pass_rate": 0.8, "judge_no_medical_advice_fails": 2}
    agg = aggregate({"a": a, "b": b})
    assert abs(agg["final_pass_rate"] - 0.6) < 1e-9
    assert agg["judge_no_medical_advice_fails"] == 3


def test_cache_key_changes_with_any_input() -> None:
    cfg = CONFIGS[0]
    pv = {"outreach-script": "1"}
    key = cache_key(cfg, SUNRISE, pv, "ollama:qwen")
    assert key == cache_key(cfg, SUNRISE, pv, "ollama:qwen")
    assert key != cache_key(cfg, SUNRISE, {"outreach-script": "2"}, "ollama:qwen")
    assert key != cache_key(cfg, SUNRISE, pv, "claude:opus")
    assert key != cache_key(CONFIGS[1], SUNRISE, pv, "ollama:qwen")
    assert config_prompts(SUNRISE, "es") == [
        "outreach-judge", "outreach-script-es", "outreach-script-revise-es",
    ]  # fmt: skip
    assert "outreach-script" in config_prompts(SUNRISE, None)
