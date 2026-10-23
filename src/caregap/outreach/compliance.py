"""Rule-based compliance checks for generated outreach scripts.

Deterministic and fast: no model involved. Rules:

- disclaimer       the profile's disclaimer appears verbatim
- opt_out          the profile's opt-out instruction appears verbatim
- max_length       at most ``profile.max_words`` words
- member_phi       none of the member's non-allowed fields (last name, birth date, age,
                   phone, member ID) appear
- phi_pattern      no dates, phone numbers or ID-like numbers at all
- diagnosis_term   no diagnosis or condition names
- drug_name        no medication names
- placeholder      no unfilled template placeholders like [Name] or {{name}}

"Verbatim" ignores case, whitespace and punctuation, so formatting differences pass but
paraphrases do not.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date

from caregap.data.codes import STATIN_INGREDIENTS
from caregap.outreach.profile import OutreachProfile


@dataclass(frozen=True)
class MemberContext:
    """Everything known about the member, used to detect leaks (not all of it is allowed)."""

    member_id: str
    given_name: str | None
    family_name: str | None
    birth_date: date
    phone: str | None
    age: int
    #: Preferred language (ISO 639-1), from the member registry.
    language: str = "en"


@dataclass(frozen=True)
class Violation:
    rule: str
    detail: str


@dataclass(frozen=True)
class ComplianceResult:
    violations: tuple[Violation, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> bool:
        return not self.violations

    @property
    def rules(self) -> list[str]:
        return sorted({v.rule for v in self.violations})


DIAGNOSIS_TERMS = (
    "diabetes", "diabetic", "prediabetes", "retinopathy", "neuropathy", "hyperlipidemia",
    "hypertension", "high blood pressure", "kidney disease", "heart disease", "heart failure",
    "cancer", "depression", "dementia", "copd", "asthma", "obesity", "stroke",
)  # fmt: skip

DRUG_NAMES = (
    *STATIN_INGREDIENTS, "statin", "statins", "metformin", "insulin", "lisinopril",
    "amlodipine", "hydrochlorothiazide", "simvastatin", "atorvastatin", "aspirin",
    "warfarin", "levothyroxine", "losartan", "metoprolol", "gabapentin",
)  # fmt: skip

_MONTHS = (
    "january|february|march|april|may|june|july|august|september|october|november|december"
    "|jan|feb|mar|apr|jun|jul|aug|sep|sept|oct|nov|dec"
)
_DATE_PATTERNS = (
    re.compile(r"\b\d{4}-\d{1,2}-\d{1,2}\b"),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(rf"\b(?:{_MONTHS})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?\b", re.IGNORECASE),
    re.compile(rf"\b\d{{1,2}}(?:st|nd|rd|th)?\s+(?:of\s+)?(?:{_MONTHS})\b", re.IGNORECASE),
)
_PHONE_PATTERN = re.compile(r"\b(?:\+?1[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)?\d{3}[\s.-]\d{4}\b")
_LONG_NUMBER = re.compile(r"\b\d{5,}\b")
_PLACEHOLDER = re.compile(r"\[[A-Za-z _]+\]|\{\{.*?\}\}|<[A-Za-z _]+>|\{[a-z_]+\}")


def normalize(text: str) -> str:
    """Lowercase, letters/digits only, single spaces."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _contains_phrase(text: str, phrase: str) -> bool:
    return f" {normalize(phrase)} " in f" {normalize(text)} "


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s)


def check(script: str, profile: OutreachProfile, member: MemberContext) -> ComplianceResult:
    v: list[Violation] = []
    text_norm = f" {normalize(script)} "

    if not _contains_phrase(script, profile.disclaimer):
        v.append(Violation("disclaimer", "required disclaimer missing or reworded"))
    if not _contains_phrase(script, profile.opt_out):
        v.append(Violation("opt_out", "required opt-out instruction missing or reworded"))

    words = len(script.split())
    if words > profile.max_words:
        v.append(Violation("max_length", f"{words} words > {profile.max_words}"))

    v.extend(_member_phi(script, text_norm, profile, member))

    for pattern in _DATE_PATTERNS:
        if m := pattern.search(script):
            v.append(Violation("phi_pattern", f"date {m.group(0)!r}"))
            break
    if m := _PHONE_PATTERN.search(script):
        v.append(Violation("phi_pattern", f"phone number {m.group(0)!r}"))
    elif m := _LONG_NUMBER.search(script):
        v.append(Violation("phi_pattern", f"ID-like number {m.group(0)!r}"))

    for term in DIAGNOSIS_TERMS:
        if f" {term} " in text_norm:
            v.append(Violation("diagnosis_term", term))
    for name in DRUG_NAMES:
        if f" {name} " in text_norm:
            v.append(Violation("drug_name", name))

    if m := _PLACEHOLDER.search(script):
        v.append(Violation("placeholder", m.group(0)))
    return ComplianceResult(tuple(v))


def _member_phi(
    script: str, text_norm: str, profile: OutreachProfile, member: MemberContext
) -> list[Violation]:
    out: list[Violation] = []
    allowed = set(profile.allowed_member_fields)
    if "family_name" not in allowed and member.family_name:
        # Synthea names carry digits (e.g. "Welch179"); match the alphabetic part too.
        for variant in {member.family_name, re.sub(r"\d+", "", member.family_name)}:
            if len(variant) > 1 and f" {normalize(variant)} " in text_norm:
                out.append(Violation("member_phi", "last name"))
                break
    if (
        "given_name" not in allowed
        and member.given_name
        and f" {normalize(member.given_name)} " in text_norm
    ):
        out.append(Violation("member_phi", "first name"))
    if "member_id" not in allowed and member.member_id.lower() in script.lower():
        out.append(Violation("member_phi", "member ID"))
    if "phone" not in allowed and member.phone:
        phone_digits = _digits(member.phone)
        if len(phone_digits) >= 7 and phone_digits[-7:] in _digits(script):
            out.append(Violation("member_phi", "phone number"))
    if "birth_date" not in allowed:
        b = member.birth_date
        forms = {b.isoformat(), f"{b.month}/{b.day}/{b.year}", f"{b:%B} {b.day} {b.year}"}
        if any(f" {normalize(f)} " in text_norm for f in forms) or f" {b.year} " in text_norm:
            out.append(Violation("member_phi", "birth date"))
    if "age" not in allowed and re.search(
        rf"\b{member.age}\s*(?:-\s*)?(?:years?|yrs?)\b", script, re.IGNORECASE
    ):
        out.append(Violation("member_phi", "age"))
    return out
