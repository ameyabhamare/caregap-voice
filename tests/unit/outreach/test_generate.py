from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.llm.base import LLMClient
from caregap.outreach.compliance import MemberContext
from caregap.outreach.generate import COMPLIANT, NEEDS_REVIEW, ScriptGenerator, clean
from caregap.prompts import FilePromptStore
from tests.helpers import P

from ..llm.fakes import FakeProvider

MEMBER = MemberContext("m-1", "Ava", "Welch179", date(1955, 4, 2), "555-0142", 70)
GOOD = (
    f"Hello Ava, please book a yearly wellness visit with your doctor. {P.disclaimer} {P.opt_out}"
)
BAD = "Hello Ava Welch, your diabetes needs attention."


def _generator(replies: list[str]) -> tuple[ScriptGenerator, FakeProvider]:
    provider = FakeProvider(replies)
    client = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    return ScriptGenerator(client, FilePromptStore(), P), provider


def test_compliant_first_try_makes_one_call() -> None:
    gen, provider = _generator([GOOD])
    out = gen.generate(MEMBER, ["awv"])
    assert out.status == COMPLIANT
    assert len(out.attempts) == 1
    assert len(provider.calls) == 1
    assert out.prompt_versions == {"outreach-script": "file"}


def test_failed_first_attempt_is_revised_with_feedback() -> None:
    gen, provider = _generator([BAD, GOOD])
    out = gen.generate(MEMBER, ["awv"])
    assert out.status == COMPLIANT
    assert len(out.attempts) == 2
    assert out.script == GOOD
    revise_call = provider.calls[1]
    assert [m.role for m in revise_call][-2:] == ["assistant", "user"]
    assert revise_call[-2].content == BAD
    assert "diagnosis_term" in revise_call[-1].content
    assert "member_phi" in revise_call[-1].content
    assert set(out.prompt_versions) == {"outreach-script", "outreach-script-revise"}


def test_two_failures_go_to_review_and_stop() -> None:
    gen, provider = _generator([BAD, BAD])
    out = gen.generate(MEMBER, ["awv"])
    assert out.status == NEEDS_REVIEW
    assert len(provider.calls) == 2
    assert not out.final.passed


def test_only_allowlisted_member_fields_reach_the_model() -> None:
    gen, provider = _generator([GOOD])
    gen.generate(MEMBER, ["eye_exam", "statin"])
    sent = " ".join(m.content for m in provider.calls[0])
    assert "Ava" in sent
    for secret in ("Welch", "555-0142", "1955", "m-1"):
        assert secret not in sent
    assert P.measure_descriptions["eye_exam"] in sent


def test_measure_selection_caps_and_orders() -> None:
    gen, _ = _generator([])
    assert gen.select_measures(["statin", "awv", "eye_exam"]) == ("awv", "eye_exam")
    with pytest.raises(ValueError, match="no description"):
        gen.select_measures(["flu_shot"])
    with pytest.raises(ValueError, match="no open gaps"):
        gen.generate(MEMBER, [])


def test_clean_strips_wrapping_quotes() -> None:
    assert clean('  "Hello there."  ') == "Hello there."
    assert clean('"Hello" she said') == '"Hello" she said'


def test_generic_profile_never_sends_the_name() -> None:
    profile = P.model_copy(
        update={"allowed_member_fields": (), "script_prompt": "outreach-script-generic"}
    )
    generic = f"Hello, a yearly wellness visit is due. {P.disclaimer} {P.opt_out}"
    provider = FakeProvider([generic])
    client = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    out = ScriptGenerator(client, FilePromptStore(), profile).generate(MEMBER, ["awv"])
    assert out.status == COMPLIANT
    assert out.prompt_versions == {"outreach-script-generic": "file"}
    assert "Ava" not in " ".join(m.content for m in provider.calls[0])

    # A script that uses the name anyway fails: the name is PHI for this customer.
    provider = FakeProvider([GOOD, GOOD])
    client = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    out = ScriptGenerator(client, FilePromptStore(), profile).generate(MEMBER, ["awv"])
    assert out.status == NEEDS_REVIEW
    assert "member_phi" in out.final.rules


def test_spanish_profile_uses_spanish_prompts_and_records_the_language() -> None:
    es = P.model_copy(
        update={"language": "es", "script_prompt": "outreach-script-es",
                "revise_prompt": "outreach-script-revise-es"}
    )  # fmt: skip
    provider = FakeProvider(["Hola Ava, sobre su diabetes.", GOOD])
    client = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    out = ScriptGenerator(client, FilePromptStore(), es).generate(MEMBER, ["awv"])
    assert out.language == "es"
    assert set(out.prompt_versions) == {"outreach-script-es", "outreach-script-revise-es"}
    assert "Nombre de pila del miembro: Ava" in provider.calls[0][1].content
