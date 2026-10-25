from __future__ import annotations

import csv
import io
from collections.abc import Callable
from datetime import date
from decimal import Decimal

import pytest

from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.evals.rules import EvalCase, results_csv, run, summarize
from caregap.llm.base import LLMClient, Message, ProviderError
from caregap.outreach.compliance import MemberContext
from caregap.outreach.generate import ScriptGenerator
from caregap.prompts import FilePromptStore
from tests.helpers import P

from ..llm.fakes import FakeProvider

GOOD = f"Hi Ava, please book a yearly wellness visit with your doctor. {P.disclaimer} {P.opt_out}"
NO_OPT_OUT = f"Hi Ava, please book a yearly wellness visit with your doctor. {P.disclaimer}"


class FakeStore:
    def member(self, member_id: str) -> MemberContext:
        return MemberContext(member_id, "Ava", "Welch179", date(1955, 4, 2), "555-0142", 70)


def _gen(reply: list[str] | Callable[[list[Message]], str]) -> ScriptGenerator:
    provider = FakeProvider(reply)
    return ScriptGenerator(
        LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5"))), FilePromptStore(), P
    )


def test_summary_rates_and_rule_counts() -> None:
    # m1 passes first try; m2 fails then passes; m3 fails twice (missing opt-out).
    def reply(messages: list[Message]) -> str:
        user = messages[1].content
        revising = len(messages) > 2
        if "m3" in user or ("m2" in user and not revising):
            return NO_OPT_OUT
        return GOOD

    gen = _gen(reply)
    # Put the member id where the fake can see it (given_name renders into the prompt).
    cases = [EvalCase(m, ["awv"]) for m in ("m1", "m2", "m3")]

    class Store(FakeStore):
        def member(self, member_id: str) -> MemberContext:
            return MemberContext(member_id, member_id, None, date(1955, 4, 2), None, 70)

    outcomes, errors = run({"en": gen}, Store(), cases, log=lambda _: None)
    m = summarize(outcomes, errors)
    assert m["scripts"] == 3
    assert m["first_attempt_pass_rate"] == pytest.approx(1 / 3)
    assert m["final_pass_rate"] == pytest.approx(2 / 3)
    assert m["revised_rate"] == pytest.approx(2 / 3)
    assert m["review_rate"] == pytest.approx(1 / 3)
    assert m["model_calls"] == 5
    assert m["first_attempt_fail_opt_out"] == 2
    assert m["final_fail_opt_out"] == 1


def test_provider_errors_are_counted_not_fatal() -> None:
    def boom(_: list[Message]) -> str:
        raise ProviderError("down")

    case = EvalCase("m1", ["awv"])
    outcomes, errors = run({"en": _gen(boom)}, FakeStore(), [case], log=lambda _: None)
    assert (outcomes, errors) == ([], 1)
    assert summarize(outcomes, errors) == {"scripts": 0, "errors": 1}


def test_results_csv_has_one_row_per_script() -> None:
    case = EvalCase("m1", ["awv"])
    outcomes, _ = run({"en": _gen([GOOD])}, FakeStore(), [case], log=lambda _: None)
    rows = list(csv.DictReader(io.StringIO(results_csv(outcomes))))
    assert rows[0]["status"] == "compliant"
    assert rows[0]["script"] == GOOD
