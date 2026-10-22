"""Edge-case members through the local pipeline and into Postgres (needs `make up`)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import psycopg
import pytest

from caregap.gaps.sync import sync

pytestmark = pytest.mark.integration
pytest.importorskip("pyspark")


@pytest.fixture(scope="module")
def snapshot(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if shutil.which("java") is None:
        pytest.skip("needs Java for PySpark")
    from caregap.data.edge_cases import export_resources
    from caregap.pipeline.job import main

    root = tmp_path_factory.mktemp("lake")
    source = root / "export"
    source.mkdir()
    for rtype, resources in export_resources().items():
        (source / f"{rtype}.ndjson").write_text("".join(json.dumps(r) + "\n" for r in resources))
    main(["--layer", "all", "--source", str(source), "--lake", str(root / "lake")])
    return root / "lake" / "snapshot"


def _state(pg_url: str) -> tuple[list[Any], list[Any]]:
    with psycopg.connect(pg_url) as conn:
        members = conn.execute(
            "SELECT member_id, given_name, family_name, birth_date, language, phone, "
            "deceased_date, is_edge_case FROM members ORDER BY member_id"
        ).fetchall()
        gaps = conn.execute(
            "SELECT member_id, measure, measurement_year, status, evidence_date, pdc, reason "
            "FROM care_gaps ORDER BY member_id, measure"
        ).fetchall()
    return members, gaps


@pytest.fixture
def clean_db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE outreach_scripts, care_gaps, members")
    return pg_url


def test_sync_loads_edge_cases_and_is_idempotent(clean_db: str, snapshot: Path) -> None:
    first = sync(clean_db, snapshot)
    assert (first.members, first.gaps, first.years) == (23, 69, (2025,))
    state = _state(clean_db)

    sync(clean_db, snapshot)
    assert _state(clean_db) == state

    members, gaps = state
    by_key = {(g[0], g[1]): g for g in gaps}
    assert {by_key[("edge-all-gaps", m)][3] for m in ("awv", "eye_exam", "statin")} == {"gap"}
    assert str(by_key[("edge-statin-pdc-81", "statin")][5]) == "0.8136"
    spanish = next(m for m in members if m[0] == "edge-awv-gap-13mo")
    assert spanish[4] == "es"


def test_sync_replaces_the_measurement_year(clean_db: str, snapshot: Path, tmp_path: Path) -> None:
    sync(clean_db, snapshot)
    changed = tmp_path / "snapshot"
    shutil.copytree(snapshot, changed)
    part = next((changed / "member_gaps").glob("part-*.json"))
    rows = [json.loads(line) for line in part.read_text().splitlines()]
    kept = [r for r in rows if r["member_id"] != "edge-awv-met"]  # member dropped from gold
    part.write_text("".join(json.dumps(r) + "\n" for r in kept))

    result = sync(clean_db, changed)
    assert result.gaps == 66
    _, gaps = _state(clean_db)
    assert len(gaps) == 66
    assert not any(g[0] == "edge-awv-met" for g in gaps)


def test_sync_rejects_gaps_for_unknown_members(
    clean_db: str, snapshot: Path, tmp_path: Path
) -> None:
    broken = tmp_path / "snapshot"
    shutil.copytree(snapshot, broken)
    part = next((broken / "members").glob("part-*.json"))
    lines = part.read_text().splitlines()
    part.write_text("\n".join(line for line in lines if "edge-awv-met" not in line) + "\n")
    with pytest.raises(ValueError, match="unknown members"):
        sync(clean_db, broken)
    assert _state(clean_db) == ([], [])
