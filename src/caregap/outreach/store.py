"""Postgres access for outreach: member context, open gaps, scripts and reviews."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import psycopg
from psycopg.rows import dict_row

from caregap.outreach.compliance import MemberContext
from caregap.outreach.generate import ScriptOutcome


class NotFoundError(LookupError):
    pass


class ReviewConflictError(RuntimeError):
    pass


def age_on(birth: date, on: date) -> int:
    return on.year - birth.year - ((on.month, on.day) < (birth.month, birth.day))


@dataclass(frozen=True)
class Gap:
    measure: str
    status: str
    evidence_date: date | None
    reason: str


class OutreachStore:
    def __init__(self, conninfo: str) -> None:
        self.conninfo = conninfo

    def _connect(self) -> psycopg.Connection[dict[str, Any]]:
        return psycopg.connect(self.conninfo, row_factory=dict_row)

    def member(self, member_id: str, *, today: date | None = None) -> MemberContext:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT member_id, given_name, family_name, birth_date, phone"
                " FROM members WHERE member_id = %s",
                (member_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"member {member_id} not found")
        return MemberContext(
            member_id=row["member_id"],
            given_name=row["given_name"],
            family_name=row["family_name"],
            birth_date=row["birth_date"],
            phone=row["phone"],
            age=age_on(row["birth_date"], today or datetime.now(UTC).date()),
        )

    def gaps(self, member_id: str, year: int) -> list[Gap]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT measure, status, evidence_date, reason FROM care_gaps"
                " WHERE member_id = %s AND measurement_year = %s ORDER BY measure",
                (member_id, year),
            ).fetchall()
        return [Gap(**r) for r in rows]

    def save_script(self, outcome: ScriptOutcome, year: int, *, hold: bool = False) -> int:
        """Store a generated script. ``hold`` parks a compliant script as
        ``pending_approval`` so a human approves it before it is used."""
        status = "pending_approval" if hold and outcome.status == "compliant" else outcome.status
        attempts = [
            {
                "text": a.text,
                "violations": [
                    {"rule": v.rule, "detail": v.detail} for v in a.compliance.violations
                ],
                "input_tokens": a.input_tokens,
                "output_tokens": a.output_tokens,
                "latency_ms": round(a.latency_s * 1000),
            }
            for a in outcome.attempts
        ]
        with self._connect() as conn:
            row = conn.execute(
                """
                INSERT INTO outreach_scripts (member_id, measurement_year, measures, status,
                    script, violations, attempts, prompt_versions, provider, model,
                    input_tokens, output_tokens, latency_ms)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    outcome.member_id, year, list(outcome.measures), status,
                    outcome.script, json.dumps(attempts[-1]["violations"]), json.dumps(attempts),
                    json.dumps(outcome.prompt_versions), outcome.provider, outcome.model,
                    sum(a.input_tokens for a in outcome.attempts),
                    sum(a.output_tokens for a in outcome.attempts),
                    round(sum(a.latency_s for a in outcome.attempts) * 1000),
                ),
            ).fetchone()  # fmt: skip
        assert row is not None  # noqa: S101 - INSERT ... RETURNING always returns a row
        return int(row["id"])

    def script(self, script_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM outreach_scripts WHERE id = %s", (script_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"script {script_id} not found")
        return row

    def pending_reviews(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as conn:
            return conn.execute(
                "SELECT * FROM outreach_scripts"
                " WHERE status IN ('needs_review', 'pending_approval')"
                " ORDER BY created_at, id LIMIT %s",
                (limit,),
            ).fetchall()

    def review(self, script_id: int, *, approve: bool, reviewer: str, note: str | None) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE outreach_scripts
                SET status = %s, reviewer = %s, review_note = %s, reviewed_at = now()
                WHERE id = %s AND status IN ('needs_review', 'pending_approval')
                """,
                ("approved" if approve else "rejected", reviewer, note, script_id),
            )  # fmt: skip
            if cur.rowcount == 0:
                exists = conn.execute(
                    "SELECT status FROM outreach_scripts WHERE id = %s", (script_id,)
                ).fetchone()
                if exists is None:
                    raise NotFoundError(f"script {script_id} not found")
                raise ReviewConflictError(
                    f"script {script_id} is {exists['status']}, not waiting for review"
                )
