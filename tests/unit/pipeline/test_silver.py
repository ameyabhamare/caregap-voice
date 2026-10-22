from __future__ import annotations

import datetime as dt

from pyspark.sql import SparkSession

from caregap.data import codes
from caregap.pipeline import silver

from . import fhir


def test_members_normalizes_language_phone_and_name(spark: SparkSession) -> None:
    resources = [
        fhir.patient(
            "p1",
            "1950-01-02",
            name=[
                {"use": "maiden", "family": "Old", "given": ["X"]},
                {"use": "official", "family": "Doe", "given": ["Ana", "B"]},
            ],
            telecom=[
                {"system": "email", "value": "a@example.org"},
                {"system": "phone", "value": "555-0100"},
            ],
            communication=[{"language": {"coding": [{"code": "es-MX"}]}}],
            identifier=[{"system": codes.EDGE_CASE_IDENTIFIER_SYSTEM, "value": "p1"}],
        ),
        fhir.patient("p2", "1990-05-05", deceasedDateTime="2025-03-04T10:00:00-05:00"),
    ]
    rows = {r["member_id"]: r for r in silver.members(fhir.bronze(spark, resources)).collect()}
    p1, p2 = rows["p1"], rows["p2"]
    assert (p1["given_name"], p1["family_name"]) == ("Ana", "Doe")
    assert p1["phone"] == "555-0100"
    assert p1["language"] == "es"
    assert p1["is_edge_case"] is True
    assert p2["language"] == "en"  # no communication -> default
    assert p2["is_edge_case"] is False
    assert p2["deceased_date"] == dt.date(2025, 3, 4)  # calendar date as recorded


def test_dispenses_flag_statins_and_drop_incomplete(spark: SparkSession) -> None:
    resources = [
        fhir.fill("p1", "2025-01-01", 30, text="Atorvastatin 40 MG Oral Tablet"),
        fhir.fill("p1", "2025-02-01", 30, text="Metformin 500 MG Oral Tablet"),
        fhir.fill("p1", "2025-03-01", 30, status="cancelled"),
    ]
    rows = silver.medication_dispenses(fhir.bronze(spark, resources)).collect()
    assert [(r["medication_text"].split()[0], r["is_statin"]) for r in rows] == [
        ("Atorvastatin", True),
        ("Metformin", False),
    ]
    assert rows[0]["days_supply"] == 30
    assert rows[0]["member_id"] == "p1"


def test_procedure_falls_back_to_performed_datetime(spark: SparkSession) -> None:
    proc = fhir.procedure("p1", "722161008", "2025-01-01")
    del proc["performedPeriod"]
    proc["performedDateTime"] = "2025-07-07T12:00:00+00:00"
    (row,) = silver.procedures(fhir.bronze(spark, [proc])).collect()
    assert row["performed_date"] == dt.date(2025, 7, 7)
