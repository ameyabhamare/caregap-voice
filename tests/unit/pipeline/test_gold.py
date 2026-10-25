"""Targeted gold-layer cases the edge-case members don't cover."""

from __future__ import annotations

from typing import Any

import pytest
from pyspark.sql import SparkSession

from caregap.data import codes
from caregap.pipeline import gold, silver

from . import fhir

NEG = codes.NO_RETINOPATHY[0]
MILD = codes.MILD_NPDR[0]
DIABETES = codes.DIABETES_TYPE_2[0]


def _gaps(spark: SparkSession, resources: list[dict[str, Any]]) -> dict[tuple[str, str], Any]:
    b = fhir.bronze(spark, resources)
    tables = {name: build(b) for name, build in silver.SILVER_TABLES.items()}
    return {
        (r["member_id"], r["measure"]): r for r in gold.member_gaps(spark, tables, 2025).collect()
    }


def test_supply_past_year_end_is_truncated(spark: SparkSession) -> None:
    # Nov 1 (60 days) covers Nov 1-Dec 30; Dec 1 (30 days) shifts to Dec 31 and is cut there.
    rows = _gaps(spark, [
        fhir.patient("p", "1960-01-01"),
        fhir.fill("p", "2025-11-01", 60),
        fhir.fill("p", "2025-12-01", 30),
    ])  # fmt: skip
    r = rows[("p", "statin")]
    assert r["status"] == "met"
    assert r["pdc"] == pytest.approx(1.0)
    assert r["reason"] == "PDC 100.0% (61 of 61 days)"


def test_prior_year_fills_and_zero_day_fills_are_ignored(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1960-01-01"),
        fhir.fill("p", "2024-12-20", 90),
        fhir.fill("p", "2025-06-01", 0),
        fhir.fill("p", "2025-07-01", 30),
    ])  # fmt: skip
    r = rows[("p", "statin")]
    assert r["status"] == "not_eligible"
    assert r["reason"] == "1 statin fill(s) in 2025; needs 2"


def test_non_statin_fills_do_not_count(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1960-01-01"),
        fhir.fill("p", "2025-01-01", 30, text="Lisinopril 10 MG Oral Tablet"),
        fhir.fill("p", "2025-02-01", 30, text="Lisinopril 10 MG Oral Tablet"),
    ])  # fmt: skip
    assert rows[("p", "statin")]["status"] == "not_eligible"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [(NEG, NEG, "met"), (NEG, None, "gap"), (NEG, MILD, "gap"), (None, None, "gap")],
)
def test_prior_year_exam_needs_negative_result_in_both_eyes(
    spark: SparkSession, left: str | None, right: str | None, expected: str
) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1970-01-01"),
        fhir.condition("p", DIABETES, "2010-01-01"),
        *fhir.eye_exam("p", "2024-05-05", left, right),
    ])  # fmt: skip
    assert rows[("p", "eye_exam")]["status"] == expected


def test_eye_exam_outside_window_does_not_count(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1970-01-01"),
        fhir.condition("p", DIABETES, "2010-01-01"),
        *fhir.eye_exam("p", "2023-05-05", NEG, NEG),  # two years back
        *fhir.eye_exam("p", "2026-01-15", NEG, NEG),  # after the year
    ])  # fmt: skip
    assert rows[("p", "eye_exam")]["status"] == "gap"


def test_diabetes_diagnosed_after_year_end_is_not_eligible(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1970-01-01"),
        fhir.condition("p", DIABETES, "2026-02-01"),
    ])  # fmt: skip
    assert rows[("p", "eye_exam")]["status"] == "not_eligible"


def test_hospice_recorded_as_procedure_excludes(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1950-01-01"),
        fhir.procedure("p", codes.HOSPICE_CARE[0], "2025-03-03"),
    ])  # fmt: skip
    assert rows[("p", "awv")]["status"] == "excluded"
    assert rows[("p", "awv")]["reason"] == "hospice from 2025-03-03"


def test_hospice_in_prior_year_does_not_exclude(spark: SparkSession) -> None:
    rows = _gaps(spark, [
        fhir.patient("p", "1950-01-01"),
        fhir.encounter("p", codes.HOSPICE_ENCOUNTER[0], "2024-12-31"),
    ])  # fmt: skip
    assert rows[("p", "awv")]["status"] == "gap"


def test_member_with_no_history(spark: SparkSession) -> None:
    rows = _gaps(spark, [fhir.patient("p", "1995-06-01")])
    assert {m: rows[("p", m)]["status"] for m in gold.MEASURES} == dict.fromkeys(
        gold.MEASURES, "not_eligible"
    )
    assert rows[("p", "awv")]["reason"] == "age 30 < 65"
    assert rows[("p", "eye_exam")]["reason"] == "no diabetes diagnosis"
    assert rows[("p", "awv")]["evidence_date"] is None


def test_output_columns(spark: SparkSession) -> None:
    b = fhir.bronze(spark, [fhir.patient("p", "1995-06-01")])
    tables = {name: build(b) for name, build in silver.SILVER_TABLES.items()}
    assert tuple(gold.member_gaps(spark, tables, 2025).columns) == gold.GOLD_GAPS_COLUMNS
