"""Inbound callback calls (Phase 11): Twilio's signed webhooks relayed by n8n, answered by
the grounded callback agent (fake model, fake embeddings; needs `make up`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from caregap.customers import load_all
from caregap.rag.store import ChunkStore, ingest
from caregap.telephony import twilio
from tests.integration.test_callbacks import BagOfWords, client

pytestmark = pytest.mark.integration

HOOK = "http://localhost:5678/webhook/twilio-events"  # Settings default
TOKEN = "mock-auth-token"  # Settings default
SUNRISE = load_all()["sunrise"].languages["en"]
CALLER = {"CallSid": "CAin1", "From": "+15550142777", "To": "+15550100000"}


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE benefit_chunks, callback_questions, escalations, audit_log CASCADE")
        conn.execute("TRUNCATE members CASCADE")
        conn.execute(
            "INSERT INTO members (member_id, given_name, family_name, birth_date, phone)"
            " VALUES ('m-1', 'Ava', 'Synth', '1950-01-01', '555-014-2777')"
        )
    ingest(load_all(), ChunkStore(pg_url), BagOfWords(), "bag-of-words")
    return pg_url


def call(c: TestClient, query: dict[str, str], body: dict[str, str],
         sig: str | None = None) -> Any:  # fmt: skip
    url = HOOK + "?" + "&".join(f"{k}={v}" for k, v in query.items())
    signed = twilio.signature(TOKEN, url, body) if sig is None else sig
    return c.post("/telephony/events", json={"query": query, "body": body, "signature": signed})


def speech(turn: int) -> dict[str, str]:
    return {"event": "inbound-speech", "customer": "sunrise", "turn": str(turn)}


def test_greeting_question_answer_and_another_question(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, ["benefit", "A specialist visit is a $35 copay [S4-2]."])
    hello = call(c, {"event": "inbound", "customer": "sunrise"}, CALLER).json()["twiml"]
    assert SUNRISE.qa_greeting in hello
    assert 'input="speech dtmf"' in hello
    assert "event=inbound-speech&amp;customer=sunrise&amp;turn=1" in hello
    asked = {**CALLER, "SpeechResult": "What is the specialist copay?"}
    answer = call(c, speech(1), asked).json()["twiml"]
    assert "A specialist visit is a $35 copay." in answer  # citation markers removed
    assert "turn=2" in answer and "another question" in answer
    with psycopg.connect(db) as conn:
        row = conn.execute("SELECT member_id, outcome FROM callback_questions").fetchone()
    assert row == ("m-1", "answered")  # the caller's number identifies the member


def test_medical_questions_hand_off_and_end_the_call(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, ["medical"])
    asked = {**CALLER, "SpeechResult": "Should I stop taking my statin?"}
    twiml = call(c, speech(1), asked).json()["twiml"]
    assert SUNRISE.qa_medical in twiml and "<Hangup/>" in twiml and "<Gather" not in twiml
    with psycopg.connect(db) as conn:
        assert conn.execute("SELECT kind FROM escalations").fetchall() == [("callback_question",)]


def test_pressing_0_asks_for_a_person(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, [])
    twiml = call(c, speech(1), {**CALLER, "Digits": "0"}).json()["twiml"]
    assert SUNRISE.callback_confirmation in twiml
    with psycopg.connect(db) as conn:
        esc = conn.execute("SELECT kind, member_id, detail FROM escalations").fetchone()
        audited = conn.execute(
            "SELECT payload->>'inbound' FROM audit_log WHERE event = 'escalation_created'"
        ).fetchone()
    assert esc == ("member_request", "m-1", "sunrise caller asked for a person on an inbound call")
    assert audited == ("true",)


def test_the_last_turn_ends_the_call(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, ["benefit", "A specialist visit is a $35 copay [S4-2]."])
    asked = {**CALLER, "SpeechResult": "What is the specialist copay?"}
    twiml = call(c, speech(3), asked).json()["twiml"]
    assert "$35 copay" in twiml and "<Gather" not in twiml and SUNRISE.goodbye in twiml


def test_forged_or_unknown_webhooks_are_refused(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, [])
    hello = {"event": "inbound", "customer": "sunrise"}
    assert call(c, hello, CALLER, sig="forged").status_code == 403
    # The turn is part of the signed URL: a relay cannot change it.
    signed_for_1 = twilio.signature(TOKEN, HOOK + "?event=inbound-speech&customer=sunrise&turn=1",
                                    CALLER)  # fmt: skip
    assert call(c, speech(2), CALLER, sig=signed_for_1).status_code == 403
    assert call(c, {"event": "inbound", "customer": "acme"}, CALLER).status_code == 404
