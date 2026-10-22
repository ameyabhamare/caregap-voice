"""Load a gold-layer snapshot into Postgres.

    python -m caregap.gaps.sync data/lake/snapshot

The snapshot is the JSON-lines output of the pipeline's ``snapshot`` layer (local run or
downloaded from the Databricks volume): ``members/part-*.json`` and
``member_gaps/part-*.json``. Members are upserted. Care gaps for each measurement year in
the snapshot are replaced in one transaction, so running the sync twice leaves the same
rows, and readers never see a half-synced year.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Literal

import psycopg
from pydantic import BaseModel, ConfigDict

from caregap.config import get_settings
from caregap.db import apply_schema


class MemberRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str
    given_name: str | None = None
    family_name: str | None = None
    birth_date: date
    gender: str | None = None
    language: str = "en"
    phone: str | None = None
    deceased_date: date | None = None
    is_edge_case: bool = False


class GapRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    member_id: str
    measure: Literal["awv", "eye_exam", "statin"]
    measurement_year: int
    status: Literal["met", "gap", "not_eligible", "excluded"]
    evidence_date: date | None = None
    pdc: float | None = None
    reason: str


@dataclass(frozen=True)
class SyncResult:
    members: int
    gaps: int
    years: tuple[int, ...]


def _read_json_lines(directory: Path) -> Iterator[dict[str, object]]:
    parts = sorted(directory.glob("part-*.json"))
    if not parts:
        raise FileNotFoundError(f"no part-*.json files in {directory}")
    for part in parts:
        with part.open() as f:
            for line in f:
                if line.strip():
                    yield json.loads(line)


def read_snapshot(snapshot_dir: Path) -> tuple[list[MemberRow], list[GapRow]]:
    members = [MemberRow.model_validate(r) for r in _read_json_lines(snapshot_dir / "members")]
    gaps = [GapRow.model_validate(r) for r in _read_json_lines(snapshot_dir / "member_gaps")]
    known = {m.member_id for m in members}
    orphans = {g.member_id for g in gaps} - known
    if orphans:
        raise ValueError(f"{len(orphans)} gap rows reference unknown members, e.g. {orphans}")
    return members, gaps


def sync(conninfo: str, snapshot_dir: Path) -> SyncResult:
    members, gaps = read_snapshot(snapshot_dir)
    years = tuple(sorted({g.measurement_year for g in gaps}))
    with psycopg.connect(conninfo) as conn:
        apply_schema(conn)
        with conn.transaction(), conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO members (member_id, given_name, family_name, birth_date, gender,
                                     language, phone, deceased_date, is_edge_case)
                VALUES (%(member_id)s, %(given_name)s, %(family_name)s, %(birth_date)s,
                        %(gender)s, %(language)s, %(phone)s, %(deceased_date)s,
                        %(is_edge_case)s)
                ON CONFLICT (member_id) DO UPDATE SET
                    given_name = EXCLUDED.given_name, family_name = EXCLUDED.family_name,
                    birth_date = EXCLUDED.birth_date, gender = EXCLUDED.gender,
                    language = EXCLUDED.language, phone = EXCLUDED.phone,
                    deceased_date = EXCLUDED.deceased_date,
                    is_edge_case = EXCLUDED.is_edge_case, updated_at = now()
                """,
                [m.model_dump() for m in members],
            )
            cur.execute("DELETE FROM care_gaps WHERE measurement_year = ANY(%s)", (list(years),))
            with cur.copy(
                "COPY care_gaps (member_id, measure, measurement_year, status, evidence_date,"
                " pdc, reason) FROM STDIN"
            ) as copy:
                for g in gaps:
                    pdc = None if g.pdc is None else round(Decimal(str(g.pdc)), 4)
                    copy.write_row(
                        (g.member_id, g.measure, g.measurement_year, g.status,
                         g.evidence_date, pdc, g.reason)
                    )  # fmt: skip
    return SyncResult(len(members), len(gaps), years)


def main() -> None:
    snapshot_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data/lake/snapshot")
    result = sync(get_settings().database_url, snapshot_dir)
    print(
        f"[sync-gaps] {result.members} members, {result.gaps} care-gap rows for "
        f"measurement year(s) {', '.join(map(str, result.years))}"
    )


if __name__ == "__main__":
    main()
