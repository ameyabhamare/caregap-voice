"""Governance (Phase 10): the audit log traces one campaign member from member to script
to audio to call outcome (needs `make up`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import psycopg
import pytest

from caregap import audit
from tests.integration.test_campaign import Harness, reply
from tests.integration.test_campaign import db as db
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration

#: Sonnet prices (per million tokens): 100 in at $2 + 50 out at $10 = $0.0007 per call.
CALL_USD = Decimal("0.0007")


@pytest.fixture
def h(db: str, tmp_path: Path) -> Harness:
    with psycopg.connect(db) as conn:
        conn.execute("TRUNCATE audit_log")
    priced = FakeProvider(reply, name="claude", model="claude-sonnet-5-5")
    return Harness(db, tmp_path, priced)


def run_member(h: Harness) -> tuple[int, int]:
    """Prepare, approve (first N), render, call; the member presses 0, the call ends."""
    cid = h.campaign()
    h.ready(cid, "m-a")
    aid = h.call(cid, "m-a", "CA1")
    h.event(aid, {"CallSid": "CA1", "Digits": "0"}, gather=True)
    h.event(aid, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "30"})
    return cid, aid


def test_audit_log_is_append_only(h: Harness) -> None:
    with psycopg.connect(h.db) as conn:
        audit.record(conn, "test_event", member_id="m-a")
    for sql in ("UPDATE audit_log SET actor = 'x'", "DELETE FROM audit_log"):
        with psycopg.connect(h.db) as conn, pytest.raises(psycopg.errors.RaiseException):
            conn.execute(sql)
    with pytest.raises(ValueError, match="unknown audit references"):
        audit.record(None, "x", member="m-a")  # type: ignore[arg-type]


def test_one_member_is_traceable_from_member_to_audio_to_outcome(h: Harness) -> None:
    cid, aid = run_member(h)
    t = h.client.get(f"/audit/campaigns/{cid}/members/m-a").json()
    assert (t["member"]["state"], t["member"]["calls_made"]) == ("escalated", 1)
    assert [g["measure"] for g in t["gaps"]] == ["awv"]
    events = [(e["event"], e["actor"]) for e in t["events"]]
    assert events == [
        ("script_generated", "system"),
        ("script_reviewed", "test"),
        ("audio_rendered", "system"),
        ("call_queued", "system"),
        ("call_dispatched", "n8n"),
        ("dtmf", "twilio"),
        ("escalation_created", "system"),
        ("call_finished", "twilio"),
    ]
    by = {e["event"]: e for e in t["events"]}
    generated = by["script_generated"]["payload"]
    assert generated["prompt_versions"] == {"outreach-script": "file"}
    assert (generated["provider"], generated["model"]) == ("claude", "claude-sonnet-5-5")
    assert generated["status"] == "pending_approval" and generated["attempts"] == [[]]
    assert generated["reading_grade"] is not None and generated["reading_level_ok"] is True
    assert Decimal(generated["cost_usd"]) == CALL_USD
    assert by["script_reviewed"]["payload"]["decision"] == "approve"
    script_id = by["script_generated"]["script_id"]
    assert {by[e]["script_id"] for e in ("script_reviewed", "audio_rendered", "call_queued")} == {
        script_id
    }
    assert by["call_queued"]["audio_id"] == by["audio_rendered"]["audio_id"]
    assert by["dtmf"]["payload"] == {"digits": "0", "action": "escalated"}
    assert by["call_finished"]["payload"] == {"status": "completed", "duration_s": "30"}
    assert by["call_finished"]["call_attempt_id"] == aid
    assert by["escalation_created"]["payload"] == {"kind": "member_request"}
    with psycopg.connect(h.db) as conn:
        row = conn.execute(
            "SELECT cost_usd, compliance_ms, campaign_id, trace_id FROM outreach_scripts"
            " WHERE id = %s",
            (script_id,),
        ).fetchone()
    assert row is not None and row[0] == CALL_USD and row[1] >= 0 and row[2] == cid
    assert row[3] is None  # tracing is off in tests
    text = audit.format_timeline(audit.timeline(h.db, cid, "m-a"))
    assert "script_generated" in text and "call_finished" in text and "escalated" in text
    assert h.client.get(f"/audit/campaigns/{cid}/members/nobody").status_code == 404


def test_rejected_and_revised_scripts_record_every_attempt(h: Harness) -> None:
    cid = h.campaign()
    h.post(f"/campaigns/{cid}/members/m-zed/prepare")
    zed = h.client.get("/reviews").json()[0]
    h.post(f"/reviews/{zed['id']}", {"decision": "reject", "reviewer": "ana", "note": "PHI"})
    events = audit.timeline(h.db, cid, "m-zed")["events"]
    generated, reviewed = events
    assert generated["payload"]["status"] == "needs_review"
    assert len(generated["payload"]["attempts"]) == 2  # first attempt and the revision
    assert Decimal(generated["payload"]["cost_usd"]) == 2 * CALL_USD
    assert (reviewed["actor"], reviewed["payload"]) == ("ana", {"decision": "reject",
                                                                 "note": "PHI"})  # fmt: skip
