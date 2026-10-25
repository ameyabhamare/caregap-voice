"""Tiny FHIR resource builders for pipeline unit tests."""

from __future__ import annotations

import json
from itertools import count
from typing import Any

from pyspark.sql import DataFrame, SparkSession

from caregap.data import codes

_ids = count()


def _id(prefix: str) -> str:
    return f"{prefix}-{next(_ids)}"


def _cc(system: str, code: str, text: str = "") -> dict[str, Any]:
    return {"coding": [{"system": system, "code": code, "display": text}], "text": text}


def patient(pid: str, birth: str, **extra: Any) -> dict[str, Any]:
    return {"resourceType": "Patient", "id": pid, "birthDate": birth, **extra}


def encounter(pid: str, code: str, day: str, eid: str | None = None) -> dict[str, Any]:
    return {
        "resourceType": "Encounter",
        "id": eid or _id("enc"),
        "subject": {"reference": f"Patient/{pid}"},
        "type": [_cc(codes.SNOMED, code)],
        "period": {"start": f"{day}T09:00:00+00:00", "end": f"{day}T10:00:00+00:00"},
    }


def condition(pid: str, code: str, onset: str) -> dict[str, Any]:
    return {
        "resourceType": "Condition",
        "id": _id("cond"),
        "subject": {"reference": f"Patient/{pid}"},
        "code": _cc(codes.SNOMED, code),
        "onsetDateTime": f"{onset}T00:00:00+00:00",
    }


def procedure(pid: str, code: str, day: str, eid: str | None = None) -> dict[str, Any]:
    r: dict[str, Any] = {
        "resourceType": "Procedure",
        "id": _id("proc"),
        "subject": {"reference": f"Patient/{pid}"},
        "code": _cc(codes.SNOMED, code),
        "performedPeriod": {"start": f"{day}T09:00:00+00:00"},
    }
    if eid:
        r["encounter"] = {"reference": f"Encounter/{eid}"}
    return r


def observation(pid: str, eid: str, code: str, value: str, day: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": _id("obs"),
        "subject": {"reference": f"Patient/{pid}"},
        "encounter": {"reference": f"Encounter/{eid}"},
        "code": _cc(codes.LOINC, code),
        "effectiveDateTime": f"{day}T09:00:00+00:00",
        "valueCodeableConcept": _cc(codes.LOINC, value),
    }


def eye_exam(pid: str, day: str, left: str | None, right: str | None) -> list[dict[str, Any]]:
    eid = _id("eye")
    out = [
        encounter(pid, codes.OPHTHALMIC_ENCOUNTER[0], day, eid),
        procedure(pid, codes.DIABETIC_EYE_EXAM[0], day, eid),
    ]
    if left:
        out.append(observation(pid, eid, codes.RETINOPATHY_SEVERITY_LEFT[0], left, day))
    if right:
        out.append(observation(pid, eid, codes.RETINOPATHY_SEVERITY_RIGHT[0], right, day))
    return out


def fill(
    pid: str, day: str, days: int, text: str = "Simvastatin 20 MG Oral Tablet",
    status: str = "completed",
) -> dict[str, Any]:  # fmt: skip
    return {
        "resourceType": "MedicationDispense",
        "id": _id("fill"),
        "status": status,
        "subject": {"reference": f"Patient/{pid}"},
        "medicationCodeableConcept": _cc(codes.RXNORM, "312961", text),
        "whenHandedOver": f"{day}T09:00:00+00:00",
        "daysSupply": {"value": days},
    }


def bronze(spark: SparkSession, resources: list[dict[str, Any]]) -> DataFrame:
    rows = [(r["resourceType"], r["id"], json.dumps(r)) for r in resources]
    return spark.createDataFrame(rows, "resource_type string, resource_id string, raw string")
