"""Append-only audit log (Phase 10): one row per governed event.

Every event is written with the connection of the change it records, so both commit or
neither does. The table rejects UPDATE and DELETE (a trigger in schema.sql).

Events:
    script_generated    prompt versions, model, rule results, reading grade, cost, trace
    script_reviewed     the approver (actor) and the decision
    audio_rendered      TTS provider, voice, duration
    call_queued         a call attempt claimed for a member
    call_dispatched     Twilio accepted the call
    call_failed_to_dispatch
    call_finished       the final Twilio status and duration
    dtmf                the digit the member pressed and what it did
    escalation_created  kind; the agent's own words are never copied here
    callback_answered   route, outcome, citations, prompt versions, cost, trace
    interaction_logged  channel and outcome (not the summary)

``timeline`` reads one member's events in one campaign: the trace from member to audio to
outcome.
"""

from __future__ import annotations

import sys
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

REFS = ("customer", "campaign_id", "member_id", "script_id", "audio_id", "call_attempt_id",
        "escalation_id", "trace_id")  # fmt: skip


def record(
    conn: psycopg.Connection[Any], event: str, *, actor: str = "system",
    payload: dict[str, Any] | None = None, **refs: Any,
) -> None:  # fmt: skip
    """Append one event. ``refs`` are the columns in ``REFS``; the time is the database's
    real clock (the campaign demo clock may be shifted)."""
    if unknown := set(refs) - set(REFS):
        raise ValueError(f"unknown audit references {sorted(unknown)}")
    cols = ", ".join(["event", "actor", "payload", *refs])
    marks = ", ".join(["%s"] * (3 + len(refs)))
    conn.execute(
        f"INSERT INTO audit_log ({cols}) VALUES ({marks})",  # noqa: S608 - columns from REFS
        (event, actor, Jsonb(payload or {}), *refs.values()),
    )


def timeline(conninfo: str, campaign_id: int, member_id: str) -> dict[str, Any]:
    """The member's campaign record and every audit event for them in that campaign,
    including events recorded without the campaign (audio rendering, reviews) for the
    scripts the campaign used."""
    with psycopg.connect(conninfo, row_factory=dict_row) as conn:
        cm = conn.execute(
            "SELECT cm.campaign_id, cm.member_id, cm.state, cm.reason, cm.calls_made,"
            " cm.script_id, k.customer, k.name AS campaign, k.measurement_year"
            " FROM campaign_members cm JOIN campaigns k ON k.id = cm.campaign_id"
            " WHERE cm.campaign_id = %s AND cm.member_id = %s",
            (campaign_id, member_id),
        ).fetchone()
        if cm is None:
            return {}
        gaps = conn.execute(
            "SELECT measure, status, reason FROM care_gaps"
            " WHERE member_id = %s AND measurement_year = %s ORDER BY measure",
            (member_id, cm["measurement_year"]),
        ).fetchall()
        events = conn.execute(
            """
            SELECT * FROM audit_log
            WHERE member_id = %(m)s AND (
                campaign_id = %(c)s
                OR (campaign_id IS NULL AND script_id IN (
                    SELECT script_id FROM call_attempts
                    WHERE campaign_id = %(c)s AND member_id = %(m)s
                    UNION SELECT script_id FROM campaign_members
                    WHERE campaign_id = %(c)s AND member_id = %(m)s)))
            ORDER BY at, id
            """,
            {"c": campaign_id, "m": member_id},
        ).fetchall()
    return {"member": cm, "gaps": gaps, "events": events}


def format_timeline(t: dict[str, Any]) -> str:
    m = t["member"]
    lines = [
        f"campaign {m['campaign_id']} ({m['campaign']}, {m['customer']}) member {m['member_id']}",
        f"  state {m['state']}"
        + (f" ({m['reason']})" if m["reason"] else "")
        + f", calls {m['calls_made']}",
        "  gaps: " + ", ".join(f"{g['measure']} {g['status']}" for g in t["gaps"]),
    ]
    for e in t["events"]:
        refs = " ".join(f"{k}={e[k]}" for k in REFS[3:7] if e[k] is not None)
        lines.append(f"{e['at']:%Y-%m-%d %H:%M:%S} {e['event']:<24} {e['actor']:<8} {refs}")
        payload = {k: v for k, v in e["payload"].items() if v not in (None, [], {})}
        if payload:
            lines.append(f"    {payload}")
        if e["trace_id"]:
            lines.append(f"    MLflow trace {e['trace_id']}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    """``python -m caregap.audit CAMPAIGN_ID MEMBER_ID`` (make trace-member)."""
    from caregap.config import get_settings

    if len(argv) != 2 or not argv[0].isdigit():
        print("usage: python -m caregap.audit CAMPAIGN_ID MEMBER_ID", file=sys.stderr)
        return 2
    t = timeline(get_settings().database_url, int(argv[0]), argv[1])
    if not t:
        print(f"member {argv[1]} is not in campaign {argv[0]}", file=sys.stderr)
        return 1
    print(format_timeline(t))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
