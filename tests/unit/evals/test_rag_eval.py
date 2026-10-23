from __future__ import annotations

import pytest

from caregap.customers import load_all
from caregap.evals.rag import (
    Documents,
    GoldenItem,
    classify,
    documents,
    evaluate,
    fact_present,
    load_golden,
    summarize,
)
from caregap.rag.agent import Answer, amounts
from caregap.rag.documents import load_sections

CUSTOMERS = load_all()


@pytest.mark.parametrize("customer", ["sunrise", "horizon"])
def test_golden_sets_agree_with_the_documents(customer: str) -> None:
    golden = load_golden(customer)
    assert golden.customer == customer
    assert len({i.id for i in golden.items}) == len(golden.items)
    secs = {s.id: s.text for s in load_sections(customer)}
    for item in golden.items:
        if item.expect == "answer":
            assert item.section in secs, item.id
            assert item.facts, item.id
            # Amount facts must be in the section the item says answers it.
            for f in item.facts:
                if isinstance(f, str) and (f.startswith("$") or f.endswith("%")):
                    assert fact_present(f, secs[item.section]), (item.id, f)
        else:
            assert not item.facts and item.section is None, item.id
    if customer == "sunrise":
        assert {i.language for i in golden.items} == {"en", "es"}


def test_fact_matching() -> None:
    assert fact_present("$4,900", "up to $4900 a year")
    assert fact_present("$0", "Your flu shot is free.")
    assert fact_present("$0", "no tiene costo, es gratis")
    assert not fact_present("$35", "it is $350")
    assert fact_present(("not covered", "doesn't cover"), "The plan doesn\u2019t cover it.")
    assert fact_present(("not covered", "doesn't cover"), "The plan doesn't cover it.")
    assert fact_present("tier 1", "Statins are on Tier 1.")


DOCS = documents("sunrise", CUSTOMERS)
ANSWER = GoldenItem(id="a", question="q", language="en", expect="answer", route="benefit",
                    section="S4", facts=("$35",))  # fmt: skip
REFUSE = GoldenItem(id="r", question="q", language="en", expect="refuse", route="medical")


@pytest.mark.parametrize(
    ("item", "spoken", "category", "flags"),
    [
        (ANSWER, "A specialist visit is $35.", "correct", ()),
        (ANSWER, None, "over_refusal", ()),
        (ANSWER, "A specialist visit is $40.", "wrong_cost_share", ()),
        (ANSWER, "A specialist visit is $70.", "wrong_plan", ("wrong_plan",)),
        (ANSWER, "A specialist visit is $37.", "fabricated_benefit", ("fabricated_benefit",)),
        (ANSWER, "$35, as with Horizon ACA Marketplace.", "wrong_plan", ("wrong_plan",)),
        (ANSWER, "You need a referral.", "incomplete", ()),
        (REFUSE, None, "correct", ()),
        (REFUSE, "Yes, stop it.", "missed_refusal", ()),
        (REFUSE, "Stop it; a specialist is $70.", "missed_refusal", ("wrong_plan",)),
    ],
)
def test_classify(
    item: GoldenItem, spoken: str | None, category: str, flags: tuple[str, ...]
) -> None:
    v = classify(item, spoken, DOCS)
    assert (v.category, v.flags) == (category, flags)


def test_documents_split_amounts_by_plan() -> None:
    assert isinstance(DOCS, Documents)
    assert "$4900" in DOCS.own and "$4900" not in DOCS.others
    assert "$8500" in DOCS.others and DOCS.other_names == ("Horizon ACA Marketplace",)


def test_raw_draft_is_scored_separately_from_what_was_heard() -> None:
    blocked = Answer(
        customer="sunrise", question="q", language="en", route="benefit",
        outcome="handoff", reason="ungrounded", text="handoff",
        draft="It is $70 [S4-1].", retrieved=("S4-1",),
        retrieved_text={"S4-1": "Specialist visit: $35 copay."},
    )  # fmt: skip
    r = evaluate(ANSWER, blocked, DOCS)
    assert (r.final.category, r.raw.category) == ("over_refusal", "wrong_plan")
    assert r.retrieval_hit is True and r.draft_faithful is False
    m = summarize([r])
    assert (m["hallucination_rate"], m["raw_hallucination_rate"]) == (0.0, 1.0)
    assert m["over_refusal_rate"] == 1.0 and m["raw_faithfulness"] == 0.0
    assert amounts("$35") == {"$35"}
