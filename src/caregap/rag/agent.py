"""Callback Q&A agent: route, retrieve, answer, check, or hand off to a person.

1. **Route** (model): benefit | medical | member | off_topic. Only benefit questions go on.
2. **Retrieve** (LangChain retriever): top-k chunks of the caller's own plan's document.
3. **Answer** (model): from those chunks only, citing chunk IDs, or ``NOT_FOUND``.
4. **Check** (no model): at least one citation, every citation is a retrieved chunk, every
   dollar amount and percentage appears in a cited chunk, no other plan is named.
   A reply that fails is never spoken: the caller is handed off instead.

The agent does no I/O beyond the model and the retriever, so the eval runs it as is.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever

from caregap.customers import CustomerConfig
from caregap.llm.base import LLMClient
from caregap.outreach.compliance import normalize
from caregap.prompts import PromptStore

ROUTE_PROMPT = "callback-route"
ANSWER_PROMPT = "callback-answer"
ROUTES = ("benefit", "medical", "member", "off_topic")
NOT_FOUND = "NOT_FOUND"
LANGUAGE_NAMES = {"en": "English", "es": "Spanish"}

Outcome = Literal["answered", "handoff"]

_SPANISH = (
    "el la los las del mi mis para por que qué es un una cuánto "
    "cuanto cuál cual cómo como debo tengo puedo cuesta copago plan está sala ayuda pagar"
)
_SPANISH_WORDS = frozenset(_SPANISH.split())
_AMOUNT = re.compile(
    r"\$\s?\d[\d,]*(?:\.\d+)?|\b\d+(?:[.,]\d+)?\s?(?:%|por\s?ciento|percent)", re.IGNORECASE
)
_CITATION = re.compile(r"\[\s*(S\d+-\d+)\s*\]")


def detect_language(text: str) -> str:
    """``es`` for Spanish questions, else ``en``. Deterministic: inverted punctuation,
    accented letters or two common Spanish words."""
    if any(c in text for c in "¿¡ñáéíóú"):
        return "es"
    words = re.findall(r"[a-záéíóúñ]+", text.lower())
    return "es" if sum(w in _SPANISH_WORDS for w in words) >= 2 else "en"


def amounts(text: str) -> set[str]:
    """Dollar amounts and percentages, normalized: ``$4,900.00`` -> ``$4900``,
    ``40 por ciento`` -> ``40%``."""
    found = set()
    for raw in _AMOUNT.findall(text):
        a = re.sub(r"\s|,(?=\d{3}\b)", "", raw.lower()).rstrip(",")
        a = re.sub(r"(por\s?ciento|percent)$", "%", a)
        a = re.sub(r"\.0+(?=%?$)", "", a)
        found.add(a)
    return found


def citations(text: str) -> list[str]:
    return list(dict.fromkeys(_CITATION.findall(text)))


def speakable(text: str) -> str:
    """The answer as the caller hears it: citations removed."""
    return re.sub(r"\s+([.,;:!?])", r"\1", _CITATION.sub("", text)).strip()


def grounding_problems(
    draft: str, retrieved: Mapping[str, str], other_plans: list[str]
) -> list[str]:
    problems = []
    cited = citations(draft)
    if not cited:
        problems.append("no citation")
    if unknown := [c for c in cited if c not in retrieved]:
        problems.append(f"cites chunks that were not retrieved: {unknown}")
    supported = set().union(*(amounts(retrieved[c]) for c in cited if c in retrieved))
    if unsupported := sorted(amounts(draft) - supported):
        problems.append(f"amounts not in the cited text: {unsupported}")
    named = [p for p in other_plans if f" {normalize(p)} " in f" {normalize(draft)} "]
    if named:
        problems.append(f"names another plan: {named}")
    return problems


@dataclass(frozen=True)
class Answer:
    customer: str
    question: str
    language: str
    route: str
    outcome: Outcome
    #: Why the caller was handed off (route name, unclear_route, not_found, ungrounded).
    reason: str | None
    #: What the caller hears.
    text: str
    #: The model's answer before the check (None if routing stopped it).
    draft: str | None = None
    citations: tuple[str, ...] = ()
    retrieved: tuple[str, ...] = ()
    retrieved_text: Mapping[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()
    prompt_versions: Mapping[str, str] = field(default_factory=dict)
    model: str = ""
    latency_s: float = 0.0


class CallbackAgent:
    def __init__(
        self,
        llm: LLMClient,
        prompts: PromptStore,
        customers: Mapping[str, CustomerConfig],
        retriever_for: Callable[[str], BaseRetriever],
        *,
        max_output_tokens: int = 300,
    ) -> None:
        self.llm = llm
        self.prompts = prompts
        self.customers = dict(customers)
        self.retriever_for = retriever_for
        self.max_output_tokens = max_output_tokens

    def ask(self, customer: str, question: str) -> Answer:
        started = time.monotonic()
        cfg = self.customers[customer]
        language = cfg.language_for(detect_language(question))
        lang = cfg.languages[language]
        versions: dict[str, str] = {}

        def done(**kw: object) -> Answer:
            return Answer(
                customer=customer, question=question, language=language,
                prompt_versions=versions, model=self.llm.model,
                latency_s=time.monotonic() - started, **kw,  # type: ignore[arg-type]
            )  # fmt: skip

        route_prompt = self.prompts.get(ROUTE_PROMPT)
        versions[ROUTE_PROMPT] = route_prompt.version
        reply = self.llm.complete(
            route_prompt.render(question=question), max_output_tokens=10,
            purpose=f"callback-route:{customer}",
        ).text  # fmt: skip
        route = parse_route(reply)
        if route != "benefit":
            text = lang.qa_medical if route == "medical" else lang.qa_handoff
            reason = route if route in ROUTES else "unclear_route"
            return done(route=route, outcome="handoff", reason=reason, text=text)

        docs: list[Document] = self.retriever_for(customer).invoke(question)
        retrieved = {d.metadata["id"]: d.page_content for d in docs}
        context = "\n\n".join(f"[{i}] {t}" for i, t in retrieved.items())
        answer_prompt = self.prompts.get(ANSWER_PROMPT)
        versions[ANSWER_PROMPT] = answer_prompt.version
        draft = self.llm.complete(
            answer_prompt.render(
                plan_name=cfg.plan_name, language_name=LANGUAGE_NAMES[language],
                context=context, question=question,
            ),
            max_output_tokens=self.max_output_tokens, purpose=f"callback-answer:{customer}",
        ).text.strip()  # fmt: skip
        common = {"route": route, "draft": draft, "retrieved": tuple(retrieved),
                  "retrieved_text": retrieved, "citations": tuple(citations(draft))}  # fmt: skip
        if NOT_FOUND in draft:
            return done(outcome="handoff", reason="not_found", text=lang.qa_handoff, **common)
        others = [c.plan_name for c in self.customers.values() if c.id != customer]
        problems = grounding_problems(draft, retrieved, others)
        if problems:
            return done(outcome="handoff", reason="ungrounded", text=lang.qa_handoff,
                        problems=tuple(problems), **common)  # fmt: skip
        return done(outcome="answered", reason=None, text=speakable(draft), **common)


def parse_route(reply: str) -> str:
    """The first route label in the reply; ``unclear`` if there is none."""
    words = re.findall(r"[a-z_]+", reply.lower().replace("off-topic", "off_topic"))
    return next((w for w in words if w in ROUTES), "unclear")
