from __future__ import annotations

from datetime import date

import pytest

from caregap.outreach.compliance import MemberContext, check, normalize
from tests.helpers import P

MEMBER = MemberContext(
    member_id="9f1c2d3e-0000-4000-8000-123456789abc",
    given_name="Ava",
    family_name="Welch179",
    birth_date=date(1955, 4, 2),
    phone="555-0142",
    age=70,
)
BODY = (
    "Hello Ava, this is a friendly reminder that a yearly wellness visit with your doctor "
    "is a good way to stay on top of your health. Please call your doctor's office to "
    "schedule one."
)


def script(body: str = BODY) -> str:
    return f"{body} {P.disclaimer} {P.opt_out}"


def rules(text: str) -> list[str]:
    return check(text, P, MEMBER).rules


def test_clean_script_passes() -> None:
    result = check(script(), P, MEMBER)
    assert result.passed, result.violations


def test_disclaimer_and_opt_out_tolerate_formatting_but_not_rewording() -> None:
    shouty = f"{BODY}\n\n{P.disclaimer.upper()}  {P.opt_out.replace(',', ' -')}"
    assert rules(shouty) == []
    reworded = f"{BODY} This is not medical advice. Press 9 to stop."
    assert rules(reworded) == ["disclaimer", "opt_out"]


def test_max_length() -> None:
    long_body = " ".join(["word"] * (P.max_words + 1))
    assert "max_length" in rules(script(long_body))


@pytest.mark.parametrize(
    "leak",
    [
        "Mr. Welch, ",  # last name without Synthea digits
        "Welch179, ",
        "We see you were born on April 2, 1955. ",
        "Your birthday is 4/2/1955. ",
        "At 70 years old, ",
        "Call us back at 555-0142. ",
        "Your member ID is 9f1c2d3e-0000-4000-8000-123456789abc. ",
    ],
)
def test_member_phi_is_caught(leak: str) -> None:
    assert "member_phi" in rules(script(leak + BODY))


def test_first_name_is_allowed_but_flagged_when_not_in_allowlist() -> None:
    assert rules(script()) == []
    strict = P.model_copy(update={"allowed_member_fields": ()})
    assert "member_phi" in check(script(), strict, MEMBER).rules


@pytest.mark.parametrize(
    "text",
    [
        "Your last visit was on 2024-11-15. ",
        "We noticed your visit on March 3rd. ",
        "Since the 3rd of March, ",
        "Call 617-555-0199 to book. ",
        "Reference number 4482910. ",
    ],
)
def test_generic_phi_patterns(text: str) -> None:
    assert "phi_pattern" in rules(script(text + BODY))


@pytest.mark.parametrize("term", ["diabetes", "Diabetic", "retinopathy", "high blood pressure"])
def test_diagnosis_terms(term: str) -> None:
    assert "diabetes" not in normalize(P.measure_descriptions["eye_exam"])
    assert "diagnosis_term" in rules(script(f"Because of your {term}, " + BODY))


@pytest.mark.parametrize("drug", ["simvastatin", "Atorvastatin", "statin", "metformin"])
def test_drug_names(drug: str) -> None:
    assert "drug_name" in rules(script(f"Please refill your {drug}. " + BODY))


def test_approved_measure_descriptions_pass() -> None:
    for description in P.measure_descriptions.values():
        assert rules(script(f"Ava, we recommend {description}. " + BODY)) == []


@pytest.mark.parametrize("placeholder", ["[Member Name]", "{{given_name}}", "<plan name>"])
def test_placeholders(placeholder: str) -> None:
    assert "placeholder" in rules(script(f"Hello {placeholder}, " + BODY))


def test_harmless_numbers_and_words_pass() -> None:
    ok = "Press 9 anytime. It takes about 30 minutes, and you may bring a friend. "
    assert rules(script(ok + BODY)) == []
