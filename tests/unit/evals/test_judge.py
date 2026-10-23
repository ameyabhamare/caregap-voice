from __future__ import annotations

from decimal import Decimal

import pytest

from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.evals.judge import CRITERIA, Judge, parse
from caregap.evals.readability import fk_grade, reading_level_ok, syllables
from caregap.llm.base import LLMClient
from caregap.prompts import FilePromptStore
from tests.unit.llm.fakes import FakeProvider


@pytest.mark.parametrize(
    ("word", "n"),
    [("the", 1), ("visit", 2), ("yearly", 2), ("doctor", 2), ("medication", 4),
     ("simple", 2), ("advice", 2)],
)  # fmt: skip
def test_syllables(word: str, n: int) -> None:
    assert syllables(word) == n


def test_reading_level() -> None:
    easy = "Hi Ava. It is time for your eye exam. Please call your doctor."
    hard = (
        "Comprehensive ophthalmological evaluations facilitate the identification of "
        "deteriorating retinal microvasculature, necessitating considerable deliberation."
    )
    assert fk_grade(easy) < 4 < 12 < fk_grade(hard)
    assert reading_level_ok(easy, "en") is True
    assert reading_level_ok(hard, "en") is False
    assert reading_level_ok("Hola Ava.", "es") is None
    assert fk_grade("") == 0.0


def test_parse_json_and_fallbacks() -> None:
    scores, reason = parse(
        'Sure: {"clarity": "pass", "tone": "FAIL", "no_medical_advice": "pass", '
        '"reason": "a bit pushy"}'
    )
    assert scores == {"clarity": True, "tone": False, "no_medical_advice": True}
    assert reason == "a bit pushy"
    scores, _ = parse("clarity: pass\ntone - fail\nno_medical_advice: pass")
    assert scores == {"clarity": True, "tone": False, "no_medical_advice": True}
    scores, _ = parse("I think it is fine.")
    assert scores == dict.fromkeys(CRITERIA)


def test_judge_is_metered_and_never_passes_what_it_cannot_read() -> None:
    provider = FakeProvider(['{"clarity": "pass", "tone": "pass", "no_medical_advice": "pass"}',
                             "looks good"])  # fmt: skip
    ledger = CostLedger(InMemoryLedgerStore(), Decimal("5"))
    judge = Judge(LLMClient(provider, ledger), FilePromptStore())
    v = judge.judge("Hi Ava, please book your eye exam.")
    assert v.passed and v.prompt_version == "file"
    assert "Hi Ava, please book your eye exam." in provider.calls[0][-1].content
    assert not judge.judge("x").passed
