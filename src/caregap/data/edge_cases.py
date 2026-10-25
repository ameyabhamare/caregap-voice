"""Hand-built edge-case members: parse fixtures/edge_cases/members.yaml, emit FHIR bundles.

Bundles use the same codes and resource shapes as Synthea 4.0.0 output, so gap logic
cannot tell them apart except by the ``edge-case`` tag. Patient IDs are the readable
member IDs (e.g. ``edge-awv-met``); every other ID is a UUID derived from the member ID,
so output is deterministic.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from caregap.data import codes

DEFAULT_PATH = Path("fixtures/edge_cases/members.yaml")
_NAMESPACE = uuid.UUID("0d7f6c1e-2b8a-4d33-8f0e-9c4b6a5e7d21")

Outcome = Literal["met", "gap", "not_eligible", "excluded"]
Resource = dict[str, Any]


class _Event(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WellnessVisit(_Event):
    type: Literal["wellness_visit"]
    date: date


class Hospice(_Event):
    type: Literal["hospice"]
    date: date


class Diabetes(_Event):
    type: Literal["diabetes"]
    onset: date
    kind: Literal[1, 2]


class Prediabetes(_Event):
    type: Literal["prediabetes"]
    onset: date


class EyeExam(_Event):
    type: Literal["eye_exam"]
    date: date
    retinopathy: Literal["none", "mild"] | None = None


class StatinOrder(_Event):
    type: Literal["statin_order"]
    date: date
    status: Literal["active", "completed", "stopped"]


class StatinFill(_Event):
    type: Literal["statin_fill"]
    date: date
    days: int = Field(gt=0)


Event = Annotated[
    WellnessVisit | Hospice | Diabetes | Prediabetes | EyeExam | StatinOrder | StatinFill,
    Field(discriminator="type"),
]


class Expected(BaseModel):
    model_config = ConfigDict(extra="forbid")
    awv: Outcome
    eye_exam: Outcome
    statin: Outcome


class Member(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^edge-[a-z0-9-]+$", max_length=64)
    given: str
    gender: Literal["female", "male"]
    birth_date: date
    deceased: date | None = None
    language: Literal["en", "es"] = "en"
    events: list[Event]
    expected: Expected
    why: str

    @model_validator(mode="after")
    def _fills_need_an_order(self) -> Member:
        fills = [e for e in self.events if isinstance(e, StatinFill)]
        orders = [e for e in self.events if isinstance(e, StatinOrder)]
        if fills and not any(o.date <= min(f.date for f in fills) for o in orders):
            raise ValueError(f"{self.id}: statin fills need an earlier statin_order")
        return self


class EdgeCaseSet(BaseModel):
    model_config = ConfigDict(extra="forbid")
    measurement_year: int
    as_of: date
    members: list[Member]

    @model_validator(mode="after")
    def _unique_ids(self) -> EdgeCaseSet:
        ids = [m.id for m in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("duplicate edge-case member ids")
        return self


def load(path: Path = DEFAULT_PATH) -> EdgeCaseSet:
    return EdgeCaseSet.model_validate(yaml.safe_load(path.read_text()))


# --------------------------------------------------------------------------- FHIR output


def _concept(system: str, code_display: tuple[str, str]) -> Resource:
    code, display = code_display
    return {"coding": [{"system": system, "code": code, "display": display}], "text": display}


def _instant(d: date, hour: int = 9) -> str:
    return f"{d.isoformat()}T{hour:02d}:00:00+00:00"


class _BundleBuilder:
    def __init__(self, member: Member) -> None:
        self.member = member
        self.entries: list[Resource] = []
        self._n = 0
        tag = {"system": codes.SYNTHETIC_TAG_SYSTEM, "code": "edge-case"}
        self._meta = {"tag": [tag]}

    def _id(self) -> str:
        self._n += 1
        return str(uuid.uuid5(_NAMESPACE, f"{self.member.id}:{self._n}"))

    def add(self, resource: Resource, *, rid: str | None = None) -> str:
        rid = rid or self._id()
        resource = {"resourceType": resource.pop("resourceType"), "id": rid, **resource}
        resource["meta"] = self._meta
        self.entries.append(
            {
                "fullUrl": f"urn:uuid:{rid}",
                "resource": resource,
                "request": {"method": "POST", "url": resource["resourceType"]},
            }
        )
        return rid

    @property
    def subject(self) -> Resource:
        return {"reference": f"urn:uuid:{self.member.id}"}

    def encounter(self, code: tuple[str, str], d: date) -> str:
        return self.add(
            {
                "resourceType": "Encounter",
                "status": "finished",
                "class": {
                    "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
                    "code": "AMB",
                },
                "type": [_concept(codes.SNOMED, code)],
                "subject": self.subject,
                "period": {"start": _instant(d), "end": _instant(d, 10)},
            }
        )

    def condition(self, code: tuple[str, str], onset: date) -> None:
        cs = "http://terminology.hl7.org/CodeSystem/condition-clinical"
        vs = "http://terminology.hl7.org/CodeSystem/condition-ver-status"
        self.add(
            {
                "resourceType": "Condition",
                "clinicalStatus": {"coding": [{"system": cs, "code": "active"}]},
                "verificationStatus": {"coding": [{"system": vs, "code": "confirmed"}]},
                "code": _concept(codes.SNOMED, code),
                "subject": self.subject,
                "onsetDateTime": _instant(onset),
                "recordedDate": _instant(onset),
            }
        )

    def eye_exam(self, e: EyeExam) -> None:
        enc = self.encounter(codes.OPHTHALMIC_ENCOUNTER, e.date)
        enc_ref = {"reference": f"urn:uuid:{enc}"}
        self.add(
            {
                "resourceType": "Procedure",
                "status": "completed",
                "code": _concept(codes.SNOMED, codes.DIABETIC_EYE_EXAM),
                "subject": self.subject,
                "encounter": enc_ref,
                "performedPeriod": {"start": _instant(e.date), "end": _instant(e.date, 10)},
            }
        )
        if e.retinopathy is None:
            return
        value = codes.NO_RETINOPATHY if e.retinopathy == "none" else codes.MILD_NPDR
        for eye in (codes.RETINOPATHY_SEVERITY_LEFT, codes.RETINOPATHY_SEVERITY_RIGHT):
            self.add(
                {
                    "resourceType": "Observation",
                    "status": "final",
                    "category": [
                        {
                            "coding": [
                                {
                                    "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                                    "code": "exam",
                                }
                            ]
                        }
                    ],
                    "code": _concept(codes.LOINC, eye),
                    "subject": self.subject,
                    "encounter": enc_ref,
                    "effectiveDateTime": _instant(e.date),
                    "valueCodeableConcept": _concept(codes.LOINC, value),
                }
            )

    def patient(self) -> None:
        m = self.member
        resource: Resource = {
            "resourceType": "Patient",
            "identifier": [{"system": codes.EDGE_CASE_IDENTIFIER_SYSTEM, "value": m.id}],
            "name": [{"use": "official", "family": "Edgecase", "given": [m.given]}],
            "telecom": [{"system": "phone", "value": self._phone(), "use": "mobile"}],
            "gender": m.gender,
            "birthDate": m.birth_date.isoformat(),
            "address": [{"city": "Boston", "state": "MA", "country": "US"}],
            "communication": [
                {
                    "language": {
                        "coding": [{"system": "urn:ietf:bcp:47", "code": m.language}],
                    }
                }
            ],
        }
        if m.deceased:
            resource["deceasedDateTime"] = _instant(m.deceased)
        self.add(resource, rid=m.id)

    def _phone(self) -> str:
        # Fictional 555-01xx range, stable per member.
        n = int(uuid.uuid5(_NAMESPACE, self.member.id).hex[:4], 16) % 100
        return f"555-01{n:02d}"


def _event_date(e: Event) -> date:
    return e.onset if isinstance(e, Diabetes | Prediabetes) else e.date


def build_bundle(member: Member) -> Resource:
    """A FHIR R4 transaction bundle for one edge-case member, shaped like Synthea output."""
    b = _BundleBuilder(member)
    b.patient()
    order_ids: list[tuple[date, str, StatinOrder]] = []
    statin = _concept(codes.RXNORM, codes.SIMVASTATIN_20)
    for e in sorted(member.events, key=_event_date):
        if isinstance(e, WellnessVisit):
            b.encounter(codes.WELLNESS_ENCOUNTER, e.date)
        elif isinstance(e, Hospice):
            b.encounter(codes.HOSPICE_ENCOUNTER, e.date)
        elif isinstance(e, Diabetes):
            dx = codes.DIABETES_TYPE_1 if e.kind == 1 else codes.DIABETES_TYPE_2
            b.condition(dx, e.onset)
        elif isinstance(e, Prediabetes):
            b.condition(codes.PREDIABETES, e.onset)
        elif isinstance(e, EyeExam):
            b.eye_exam(e)
        elif isinstance(e, StatinOrder):
            oid = b.add(
                {
                    "resourceType": "MedicationRequest",
                    "status": e.status,
                    "intent": "order",
                    "medicationCodeableConcept": statin,
                    "subject": b.subject,
                    "authoredOn": _instant(e.date),
                }
            )
            order_ids.append((e.date, oid, e))
        elif isinstance(e, StatinFill):
            order = max((o for o in order_ids if o[0] <= e.date), key=lambda o: o[0])
            b.add(
                {
                    "resourceType": "MedicationDispense",
                    "status": "completed",
                    "medicationCodeableConcept": statin,
                    "subject": b.subject,
                    "authorizingPrescription": [{"reference": f"urn:uuid:{order[1]}"}],
                    "quantity": {"value": e.days, "unit": "tablet"},
                    "daysSupply": {
                        "value": e.days,
                        "unit": "days",
                        "system": "http://unitsofmeasure.org",
                        "code": "d",
                    },
                    "whenHandedOver": _instant(e.date),
                }
            )
    return {"resourceType": "Bundle", "type": "transaction", "entry": b.entries}


def build_all(path: Path = DEFAULT_PATH) -> dict[str, Resource]:
    return {m.id: build_bundle(m) for m in load(path).members}


def export_resources(path: Path = DEFAULT_PATH) -> dict[str, list[Resource]]:
    """Edge-case resources grouped by type, shaped like a HAPI bulk export.

    ``urn:uuid:`` references are rewritten to ``Type/id`` the way HAPI resolves them on
    load, so pipeline tests can run on edge cases without a FHIR server.
    """
    by_type: dict[str, list[Resource]] = {}
    for bundle in build_all(path).values():
        targets = {
            e["fullUrl"]: f"{e['resource']['resourceType']}/{e['resource']['id']}"
            for e in bundle["entry"]
        }
        for e in bundle["entry"]:
            resource = _rewrite_refs(e["resource"], targets)
            by_type.setdefault(resource["resourceType"], []).append(resource)
    return by_type


def _rewrite_refs(obj: Any, targets: dict[str, str]) -> Any:
    if isinstance(obj, dict):
        out = {k: _rewrite_refs(v, targets) for k, v in obj.items()}
        if isinstance(out.get("reference"), str):
            out["reference"] = targets.get(out["reference"], out["reference"])
        return out
    if isinstance(obj, list):
        return [_rewrite_refs(v, targets) for v in obj]
    return obj
