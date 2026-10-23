from __future__ import annotations

from decimal import Decimal

import pytest
from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.customers import load_all
from caregap.llm.base import LLMClient, Message
from caregap.prompts import FilePromptStore
from caregap.rag.agent import (
    CallbackAgent,
    amounts,
    citations,
    detect_language,
    grounding_problems,
    parse_route,
    speakable,
)
from caregap.rag.store import chunk_document
from tests.unit.llm.fakes import FakeProvider

CUSTOMERS = load_all()
S4 = "Doctor visits\nSpecialist visit: $35 copay. A referral from your PCP is required."
S6 = "Eye care\nEyewear allowance: up to $150 every year toward glasses or contact lenses."


class FixedRetriever(BaseRetriever):
    docs: list[Document]

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        return self.docs


def agent(*replies: str) -> tuple[CallbackAgent, FakeProvider]:
    provider = FakeProvider(list(replies))
    docs = [Document(S4, metadata={"id": "S4-1"}), Document(S6, metadata={"id": "S6-1"})]
    return (
        CallbackAgent(
            LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5"))),
            FilePromptStore(),
            CUSTOMERS,
            lambda _: FixedRetriever(docs=docs),
        ),
        provider,
    )


def test_amounts_are_normalized() -> None:
    assert amounts("$4,900.00, $35 and 40 por ciento; 33 % or 2.5 times") == {
        "$4900", "$35", "40%", "33%",
    }  # fmt: skip
    assert amounts("Tier 1, 555-0110, 24 trips") == set()


@pytest.mark.parametrize(
    ("text", "lang"),
    [
        ("¿Cuánto es el copago?", "es"),
        ("Cuanto cuesta la sala de emergencias", "es"),
        ("What is the copay for a specialist?", "en"),
        ("Is la Quinta covered?", "en"),
    ],
)
def test_detect_language(text: str, lang: str) -> None:
    assert detect_language(text) == lang


def test_grounding_problems() -> None:
    retrieved = {"S4-1": S4, "S6-1": S6}
    assert grounding_problems("A specialist visit is $35 [S4-1].", retrieved, []) == []
    assert grounding_problems("It is $35.", retrieved, [])[0] == "no citation"
    assert "not in the cited text" in grounding_problems("It is $70 [S4-1].", retrieved, [])[0]
    # The amount is retrieved but not in the chunk the answer cites.
    assert grounding_problems("Glasses: $150 [S4-1].", retrieved, [])
    assert "not retrieved" in grounding_problems("It is $35 [S9-1].", retrieved, [])[0]
    named = grounding_problems("$35 [S4-1], like Horizon ACA Marketplace.", retrieved,
                               ["Horizon ACA Marketplace"])  # fmt: skip
    assert named == ["names another plan: ['Horizon ACA Marketplace']"]


def test_citations_route_and_speech() -> None:
    assert citations("a [S4-1] b [ S6-1 ] c [S4-1]") == ["S4-1", "S6-1"]
    assert speakable("It is $35 [S4-1]. Glasses get $150 [S6-1].") == (
        "It is $35. Glasses get $150."
    )
    assert parse_route(" Benefit.") == "benefit"
    assert parse_route("off-topic") == "off_topic"
    assert parse_route("I am not sure") == "unclear"


def test_benefit_question_is_answered_from_the_document() -> None:
    a, provider = agent("benefit", "A specialist visit costs $35 [S4-1].")
    out = a.ask("sunrise", "What is my copay to see a specialist?")
    assert (out.outcome, out.route, out.text) == ("answered", "benefit",
                                                  "A specialist visit costs $35.")  # fmt: skip
    assert out.citations == ("S4-1",)
    assert out.prompt_versions == {"callback-route": "file", "callback-answer": "file"}
    answer_prompt = provider.calls[1][0].content
    assert "[S4-1] Doctor visits" in answer_prompt and "Sunrise Medicare Advantage" in answer_prompt


@pytest.mark.parametrize(
    ("replies", "reason"),
    [
        (["medical"], "medical"),
        (["member"], "member"),
        (["off_topic"], "off_topic"),
        (["hmm"], "unclear_route"),
        (["benefit", "NOT_FOUND"], "not_found"),
        (["benefit", "It costs $70 [S4-1]."], "ungrounded"),
    ],
)
def test_everything_else_is_handed_off(replies: list[str], reason: str) -> None:
    a, _ = agent(*replies)
    out = a.ask("sunrise", "a question")
    sunrise = CUSTOMERS["sunrise"].languages["en"]
    expected = sunrise.qa_medical if reason == "medical" else sunrise.qa_handoff
    assert (out.outcome, out.reason, out.text) == ("handoff", reason, expected)
    if reason == "ungrounded":
        assert out.draft == "It costs $70 [S4-1]." and out.problems


def test_answer_language_follows_the_question_if_the_customer_speaks_it() -> None:
    a, provider = agent("medical", "benefit", "NOT_FOUND")
    es = a.ask("sunrise", "¿Debo dejar de tomar mi medicamento?")
    assert (es.language, es.text) == ("es", CUSTOMERS["sunrise"].languages["es"].qa_medical)
    en = a.ask("horizon", "¿Cuánto es el copago?")  # Horizon has no Spanish
    assert en.language == "en"
    assert "English" in provider.calls[2][0].content


def test_route_prompt_sees_only_the_question() -> None:
    a, provider = agent("medical")
    a.ask("sunrise", "Should I stop my statin?")
    sent: list[Message] = provider.calls[0]
    assert sent[-1].content == "Should I stop my statin?"


def test_chunking_keeps_section_ids_and_headings() -> None:
    text = "1. About\nPlan facts.\n2. Costs\nPremium: $0.\n"
    chunks = chunk_document("sunrise", text)
    assert [(c.id, c.citation, c.text) for c in chunks] == [
        ("sunrise:S1-1", "S1-1", "About\nPlan facts."),
        ("sunrise:S2-1", "S2-1", "Costs\nPremium: $0."),
    ]
