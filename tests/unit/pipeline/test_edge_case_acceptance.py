"""Acceptance: the pipeline reproduces every edge-case expectation (23 members x 3 measures)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pyspark.sql import SparkSession

from caregap.data.edge_cases import Member, load
from caregap.pipeline import gold, silver
from caregap.pipeline.bronze import build_bronze

MEMBERS = load().members


@pytest.fixture(scope="module")
def results(spark: SparkSession, edge_export: Path) -> dict[tuple[str, str], dict[str, object]]:
    bronze = build_bronze(spark, str(edge_export))
    tables = {name: build(bronze) for name, build in silver.SILVER_TABLES.items()}
    rows = gold.member_gaps(spark, tables, 2025).collect()
    return {(r["member_id"], r["measure"]): r.asDict() for r in rows}


def test_one_row_per_member_and_measure(results: dict[tuple[str, str], object]) -> None:
    assert len(results) == len(MEMBERS) * 3


@pytest.mark.parametrize("member", MEMBERS, ids=lambda m: m.id)
def test_matches_expected(
    member: Member, results: dict[tuple[str, str], dict[str, object]]
) -> None:
    expected = member.expected.model_dump()
    actual = {m: results[(member.id, m)]["status"] for m in expected}
    reasons = {m: results[(member.id, m)]["reason"] for m in expected}
    assert actual == expected, reasons
