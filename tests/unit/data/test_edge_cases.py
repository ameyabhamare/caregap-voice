"""Edge-case fixture tests.

``_oracle`` is a small reference implementation of the working measure definitions in
members.yaml. It checks that every hand-written expectation follows from those
definitions, so a reviewer only has to agree with the definitions and the event lists.
Phase 2's PySpark gap logic must reproduce the same expectations from the FHIR export.
"""

from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from caregap.data import codes
from caregap.data.edge_cases import (
    Diabetes,
    EdgeCaseSet,
    EyeExam,
    Hospice,
    Member,
    StatinFill,
    WellnessVisit,
    build_all,
    build_bundle,
    load,
)

SET = load()
YEAR_START, YEAR_END = date(2025, 1, 1), date(2025, 12, 31)


def _age(m: Member, on: date) -> int:
    b = m.birth_date
    return on.year - b.year - ((on.month, on.day) < (b.month, b.day))


def _in_year(d: date) -> bool:
    return YEAR_START <= d <= YEAR_END


def _pdc(fills: list[StatinFill]) -> float:
    fills = sorted(fills, key=lambda f: f.date)
    covered_until = fills[0].date - timedelta(days=1)
    covered = 0
    for f in fills:
        start = max(f.date, covered_until + timedelta(days=1))  # shift early refills forward
        end = min(start + timedelta(days=f.days - 1), YEAR_END)
        if end >= start:
            covered += (end - start).days + 1
            covered_until = end
    return covered / ((YEAR_END - fills[0].date).days + 1)


def _oracle(m: Member) -> dict[str, str]:
    age = _age(m, YEAR_END)
    excluded = (m.deceased is not None and m.deceased <= YEAR_END) or any(
        isinstance(e, Hospice) and _in_year(e.date) for e in m.events
    )

    def outcome(eligible: bool, met: bool) -> str:
        if not eligible:
            return "not_eligible"
        if excluded:
            return "excluded"
        return "met" if met else "gap"

    awv_met = any(isinstance(e, WellnessVisit) and _in_year(e.date) for e in m.events)

    diabetic = any(isinstance(e, Diabetes) and e.onset <= YEAR_END for e in m.events)
    exams = [e for e in m.events if isinstance(e, EyeExam)]
    eye_met = any(_in_year(e.date) for e in exams) or any(
        e.date.year == 2024 and e.retinopathy == "none" for e in exams
    )

    fills = [e for e in m.events if isinstance(e, StatinFill) and _in_year(e.date)]
    statin_eligible = len(fills) >= 2
    statin_met = statin_eligible and _pdc(fills) >= 0.80

    return {
        "awv": outcome(age >= 65, awv_met),
        "eye_exam": outcome(diabetic and 18 <= age <= 75, eye_met),
        "statin": outcome(statin_eligible, statin_met),
    }


@pytest.mark.parametrize("member", SET.members, ids=lambda m: m.id)
def test_expectations_follow_from_definitions(member: Member) -> None:
    assert member.expected.model_dump() == _oracle(member), member.why


def test_every_outcome_is_covered_for_every_measure() -> None:
    for measure in ("awv", "eye_exam", "statin"):
        seen = {getattr(m.expected, measure) for m in SET.members}
        assert {"met", "gap", "not_eligible", "excluded"} <= seen, measure


def test_pdc_boundaries_are_exact() -> None:
    by_id = {m.id: m for m in SET.members}

    def pdc(mid: str) -> float:
        return _pdc([e for e in by_id[mid].events if isinstance(e, StatinFill)])

    assert pdc("edge-statin-pdc-81") == pytest.approx(240 / 295)
    assert pdc("edge-statin-pdc-79") == pytest.approx(240 / 305)
    assert pdc("edge-statin-early-refills") == pytest.approx(300 / 365)


def test_bundles_are_deterministic() -> None:
    assert build_all() == build_all()


@pytest.mark.parametrize("member", SET.members, ids=lambda m: m.id)
def test_bundle_shape(member: Member) -> None:
    bundle = build_bundle(member)
    assert bundle["type"] == "transaction"
    resources = [e["resource"] for e in bundle["entry"]]
    patients = [r for r in resources if r["resourceType"] == "Patient"]
    assert len(patients) == 1
    assert patients[0]["id"] == member.id
    ids = [r["id"] for r in resources]
    assert len(ids) == len(set(ids))
    for r in resources:
        assert {"system": codes.SYNTHETIC_TAG_SYSTEM, "code": "edge-case"} in r["meta"]["tag"]
    # Every internal reference points at a resource in the same bundle.
    known = {f"urn:uuid:{i}" for i in ids}
    for ref in _references(resources):
        assert ref in known, ref


def test_resources_use_synthea_codes() -> None:
    bundle = build_bundle(next(m for m in SET.members if m.id == "edge-eye-prior-negative"))
    by_type: dict[str, list[Any]] = {}
    for e in bundle["entry"]:
        by_type.setdefault(e["resource"]["resourceType"], []).append(e["resource"])
    assert by_type["Procedure"][0]["code"]["coding"][0]["code"] == codes.DIABETIC_EYE_EXAM[0]
    values = {o["valueCodeableConcept"]["coding"][0]["code"] for o in by_type["Observation"]}
    assert values == {codes.NO_RETINOPATHY[0]}
    assert by_type["Condition"][0]["code"]["coding"][0]["code"] == codes.DIABETES_TYPE_2[0]


def _references(obj: Any) -> list[str]:
    if isinstance(obj, dict):
        refs = [obj["reference"]] if isinstance(obj.get("reference"), str) else []
        return refs + [r for v in obj.values() for r in _references(v)]
    if isinstance(obj, list):
        return [r for v in obj for r in _references(v)]
    return []


def _raw() -> Any:
    return yaml.safe_load(Path("fixtures/edge_cases/members.yaml").read_text())


def test_rejects_duplicate_ids() -> None:
    raw = _raw()
    raw["members"].append(raw["members"][0])
    with pytest.raises(ValidationError, match="duplicate"):
        EdgeCaseSet.model_validate(raw)


def test_rejects_fill_without_prior_order() -> None:
    raw = _raw()
    member = next(m for m in raw["members"] if m["id"] == "edge-statin-pdc-81")
    member["events"] = [e for e in member["events"] if e["type"] != "statin_order"]
    with pytest.raises(ValidationError, match="statin_order"):
        EdgeCaseSet.model_validate(raw)


def test_rejects_unknown_event_type() -> None:
    raw = _raw()
    raw["members"][0]["events"].append({"type": "flu_shot", "date": "2025-01-01"})
    with pytest.raises(ValidationError):
        EdgeCaseSet.model_validate(raw)
