from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from caregap.evals import ci
from caregap.evals.rules import EvalCase
from caregap.evals.suite import CONFIGS
from caregap.outreach.compliance import MemberContext
from tests.helpers import SUNRISE

METRICS = {
    "scripts:sunrise": {"final_pass_rate": 0.68, "judge_tone_pass_rate": 0.9, "scripts": 100},
    "rag:sunrise": {"answer_accuracy": 0.79, "hallucination_rate": 0.0,
                    "refusal_accuracy": 1.0, "over_refusal_rate": 0.21,
                    "raw_hallucination_rate": 0.0},
}  # fmt: skip


def test_thresholds_and_check() -> None:
    t = ci.derive_thresholds(METRICS)
    assert t["scripts:sunrise"] == {"final_pass_rate": {"min": 0.63}}  # judge not gated yet
    assert t["rag:sunrise"]["hallucination_rate"] == {"max": 0.0}
    assert ci.check(METRICS, t) == []
    worse = {**METRICS, "rag:sunrise": {**METRICS["rag:sunrise"], "hallucination_rate": 0.02}}
    assert ci.check(worse, t) == ["rag:sunrise hallucination_rate: 0.020 > 0.0"]
    assert ci.check({}, t)[0].endswith("missing")


def test_cases_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci, "CASES", tmp_path)
    m = MemberContext("m-1", "Ava", "Synth", date(1950, 1, 2), "555-0100", 75, "es")
    ci.save_cases("x", [EvalCase("m-1", ["awv"], "es")], ci.FixtureMembers({"m-1": m}))
    cases, members = ci.load_cases("x")
    assert cases == [EvalCase("m-1", ["awv"], "es")]
    assert members.member("m-1") == m


def test_reports_per_customer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ci, "REPORTS", tmp_path)
    ci.write_reports(METRICS, {"sunrise": SUNRISE}, source="test",
                     extra={"sunrise": {"Script word error rate": 0.023}})  # fmt: skip
    data = json.loads((tmp_path / "sunrise.json").read_text())
    assert data["scripts"]["sunrise"]["final_pass_rate"] == 0.68
    assert data["voice"]["Script word error rate"] == 0.023
    md = (tmp_path / "sunrise.md").read_text()
    assert "| Rule compliance, final | 68% |" in md
    assert "| Spoken answers with a wrong amount or plan | 0.0% |" in md


def test_committed_thresholds_gate_every_configuration() -> None:
    path = Path("fixtures/ci/thresholds.json")
    if not path.exists():
        pytest.skip("fixtures not recorded yet")
    t = json.loads(path.read_text())
    assert {f"scripts:{c.name}" for c in CONFIGS} | {"rag:sunrise", "rag:horizon"} == set(t)
