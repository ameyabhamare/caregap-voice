"""Campaign state machine through the API, against Postgres (needs `make up`).

Fake model and TTS, a fixed clock, and Twilio callbacks signed the way Twilio signs them.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit
from zoneinfo import ZoneInfo

import psycopg
import pytest
from fastapi.testclient import TestClient

from caregap.api.app import create_app
from caregap.campaign.clock import FixedClock
from caregap.config import Settings
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.customers import CustomerConfig
from caregap.llm.base import LLMClient, Message, ProviderError
from caregap.outreach.store import OutreachStore
from caregap.prompts import FilePromptStore
from caregap.telephony import twilio
from caregap.voice.render import AudioRenderer
from tests.helpers import SUNRISE
from tests.integration.test_api import FakeTTS
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration

NY = ZoneInfo("America/New_York")
TUESDAY_10AM = datetime(2026, 1, 13, 10, 0, tzinfo=NY)
CUSTOMER = SUNRISE.model_copy(
    update={
        "campaign": SUNRISE.campaign.model_copy(
            update={"approval_first_n": 1, "max_call_attempts": 2, "retry_delay_minutes": 60}
        )
    }
)
PROFILE = CUSTOMER.profile("en")
EN = CUSTOMER.languages["en"]
TOKEN = "test-token"
GOOD = (
    "Hi {name}, a yearly wellness visit with your doctor is due. "
    f"{PROFILE.disclaimer} {PROFILE.opt_out}"
)

# member_id, given name, phone, deceased, consent
MEMBERS = [
    ("m-a", "Ava", "555-010-0001", None, "granted"),
    ("m-b", "Bea", "555-010-0002", None, "granted"),
    ("m-c", "Cal", "555-010-0003", None, "granted"),
    ("m-zed", "Zed", "555-010-0004", None, "granted"),  # the model fails compliance for Zed
    ("m-dead", "Dee", "555-010-0005", "2025-03-01", "granted"),
    ("m-nocon", "Nia", "555-010-0006", None, None),
    ("m-revoked", "Rex", "555-010-0007", None, "revoked"),
    ("m-badphone", "Phil", "0007", None, "granted"),
    ("m-nogap", "Ned", "555-010-0009", None, "granted"),
]


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute(
            "TRUNCATE escalations, telephony_events, call_attempts, campaign_members, campaigns,"
            " consent, script_audio, outreach_scripts, care_gaps, members CASCADE"
        )
        for mid, name, phone, deceased, consent in MEMBERS:
            conn.execute(
                "INSERT INTO members (member_id, given_name, family_name, birth_date, phone,"
                " deceased_date) VALUES (%s, %s, 'Synth', '1950-01-01', %s, %s)",
                (mid, name, phone, deceased),
            )
            conn.execute(
                "INSERT INTO care_gaps (member_id, measure, measurement_year, status, reason)"
                " VALUES (%s, 'awv', 2025, %s, 'test')",
                (mid, "met" if mid == "m-nogap" else "gap"),
            )
            if consent:
                conn.execute(
                    "INSERT INTO consent (member_id, channel, status, source)"
                    " VALUES (%s, 'voice', %s, 'test')",
                    (mid, consent),
                )
    return pg_url


def reply(messages: list[Message]) -> str:
    text = " ".join(m.content for m in messages)
    if "Zed" in text:
        return "Hi Zed, about your diabetes."
    m = re.search(r"Member first name: (\w+)", text)
    return GOOD.format(name=m.group(1) if m else "there")


class Harness:
    def __init__(
        self,
        db: str,
        tmp_path: Path,
        provider: FakeProvider,
        customers: dict[str, CustomerConfig] | None = None,
    ) -> None:
        self.db = db
        self.clock = FixedClock(TUESDAY_10AM)
        self.settings = Settings(
            _env_file=None,
            database_url=db,
            data_dir=tmp_path,
            twilio_auth_token=TOKEN,
            public_base_url="http://core:8000",
            twilio_callback_url="http://n8n:5678/webhook/twilio-events",
        )
        llm = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
        renderer = AudioRenderer(db, FakeTTS(), tmp_path, overrides_enabled=True)
        self.client = TestClient(
            create_app(
                self.settings,
                store=OutreachStore(db),
                llm=llm,
                prompts=FilePromptStore(),
                renderer=renderer,
                clock=self.clock,
                customers=customers or {"sunrise": CUSTOMER},
            ),
            raise_server_exceptions=False,
        )

    def post(self, path: str, body: dict[str, Any] | None = None, status: int = 200) -> Any:
        r = self.client.post(path, json=body or {})
        assert r.status_code == status, r.text
        return r.json()

    def work(self) -> list[tuple[str, str]]:
        items = self.client.get("/work").json()["items"]
        return [(i["action"], i["member_id"]) for i in items]

    def state(self, cid: int, mid: str) -> tuple[str, str | None]:
        with psycopg.connect(self.db) as conn:
            row = conn.execute(
                "SELECT state, reason FROM campaign_members"
                " WHERE campaign_id = %s AND member_id = %s",
                (cid, mid),
            ).fetchone()
        assert row is not None
        return row[0], row[1]

    def campaign(self, size: int = 20) -> int:
        return int(self.post("/campaigns", {"customer": "sunrise", "size": size}, status=201)["id"])

    def ready(self, cid: int, *mids: str) -> None:
        """Prepare members and approve whatever waits for review."""
        for mid in mids:
            self.post(f"/campaigns/{cid}/members/{mid}/prepare")
        for s in self.client.get("/reviews").json():
            self.post(f"/reviews/{s['id']}", {"decision": "approve", "reviewer": "test"})
        self.work()

    def call(self, cid: int, mid: str, sid: str) -> int:
        out = self.post(f"/campaigns/{cid}/members/{mid}/send")
        assert out["action"] == "call", out
        self.post(f"/attempts/{out['attempt_id']}/dispatched", {"call_sid": sid})
        return int(out["attempt_id"])

    def event(
        self, attempt_id: int, body: dict[str, str], *, gather: bool = False, status: int = 200
    ) -> Any:
        query = {"attempt_id": str(attempt_id)} | ({"event": "gather"} if gather else {})
        url = "http://n8n:5678/webhook/twilio-events?" + "&".join(
            f"{k}={v}" for k, v in query.items()
        )
        sig = twilio.signature(TOKEN, url, body)
        return self.post(
            "/telephony/events", {"query": query, "body": body, "signature": sig}, status=status
        )


@pytest.fixture
def h(db: str, tmp_path: Path) -> Harness:
    return Harness(db, tmp_path, FakeProvider(reply))


def test_selection_records_why_members_cannot_be_called(h: Harness) -> None:
    summary = h.post("/campaigns", {"customer": "sunrise", "size": 20}, status=201)
    assert summary["members"] == 8  # m-nogap has no open gap
    assert summary["states"] == {"selected": 4, "skipped": 4}
    assert summary["skipped"] == {
        "consent_revoked": 1,
        "deceased": 1,
        "invalid_phone": 1,
        "no_consent": 1,
    }
    assert not summary["done"]
    # A second campaign for the same year does not re-select active members, but may retry
    # members the first one skipped.
    second = h.post("/campaigns", {"customer": "sunrise", "size": 20}, status=201)
    assert second["states"] == {"skipped": 4}


def test_first_n_and_failed_scripts_wait_for_a_human(h: Harness) -> None:
    cid = h.campaign()
    assert h.work() == [("prepare", "m-a"), ("prepare", "m-b")]  # at most 2 in flight
    first = h.post(f"/campaigns/{cid}/members/m-a/prepare")
    assert first["state"] == "awaiting_review"  # first N = 1
    assert h.post(f"/campaigns/{cid}/members/m-b/prepare")["state"] == "ready"
    assert h.post(f"/campaigns/{cid}/members/m-zed/prepare")["state"] == "awaiting_review"
    # Preparing again is a no-op, not a second model call.
    assert h.post(f"/campaigns/{cid}/members/m-a/prepare")["action"] == "none"

    reviews = {s["member_id"]: s["status"] for s in h.client.get("/reviews").json()}
    assert reviews == {"m-a": "pending_approval", "m-zed": "needs_review"}
    assert ("send", "m-b") in h.work()
    assert ("send", "m-a") not in h.work()

    h.post(f"/reviews/{first['script_id']}", {"decision": "approve", "reviewer": "ana"})
    zed = h.client.get("/reviews").json()[0]
    h.post(f"/reviews/{zed['id']}", {"decision": "reject", "reviewer": "ana"})
    assert {("send", "m-a"), ("send", "m-b")} <= set(h.work())
    assert h.state(cid, "m-zed") == ("skipped", "script_rejected")
    assert h.client.get(f"/campaigns/{cid}").json()["human_reviewed"] == 2


def test_a_completed_call_end_to_end(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-a")
    out = h.post(f"/campaigns/{cid}/members/m-a/send")
    assert out["action"] == "call"
    tw = out["twilio"]
    assert tw["url"].endswith(f"/Accounts/{h.settings.twilio_account_sid}/Calls.json")
    assert tw["form"]["To"] == "+15550100001"
    assert tw["form"]["StatusCallback"] == (
        f"http://n8n:5678/webhook/twilio-events?attempt_id={out['attempt_id']}"
    )
    # Claimed: a second send does nothing.
    assert h.post(f"/campaigns/{cid}/members/m-a/send")["action"] == "none"
    aid = out["attempt_id"]
    h.post(f"/attempts/{aid}/dispatched", {"call_sid": "CA1"})

    # Twilio fetches the TwiML (signed) and plays the rendered audio.
    params = {"CallSid": "CA1", "CallStatus": "in-progress"}
    sig = twilio.signature(TOKEN, tw["form"]["Url"], params)
    path = urlsplit(tw["form"]["Url"]).path
    r = h.client.post(path, data=params, headers={"X-Twilio-Signature": sig})
    assert r.status_code == 200
    assert "<Play>http://core:8000/scripts/" in r.text
    assert "event=gather" in r.text
    audio_path = r.text.split("<Play>http://core:8000")[1].split("</Play>")[0]
    assert h.client.get(audio_path).content[:4] == b"RIFF"
    assert h.client.post(path, data=params, headers={"X-Twilio-Signature": "x"}).status_code == 403

    h.event(aid, {"CallSid": "CA1", "CallStatus": "ringing"})
    assert h.state(cid, "m-a") == ("calling", None)
    h.event(aid, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "21"})
    h.event(aid, {"CallSid": "CA1", "CallStatus": "completed", "CallDuration": "21"})  # dup
    assert h.state(cid, "m-a") == ("completed", None)
    summary = h.client.get(f"/campaigns/{cid}").json()
    assert summary["calls"] == {"completed": 1}
    with psycopg.connect(h.db) as conn:
        assert conn.execute("SELECT count(*) FROM telephony_events").fetchone() == (3,)


def test_dtmf_9_opts_out_and_0_escalates(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-a", "m-b")
    a = h.call(cid, "m-a", "CA9")
    twiml = h.event(a, {"CallSid": "CA9", "Digits": "9"}, gather=True)["twiml"]
    assert EN.opt_out_confirmation in twiml
    h.event(a, {"CallSid": "CA9", "CallStatus": "completed"})
    assert h.state(cid, "m-a") == ("opted_out", "dtmf_9")
    with psycopg.connect(h.db) as conn:
        assert conn.execute(
            "SELECT status, source FROM consent WHERE member_id = 'm-a'"
        ).fetchone() == ("revoked", "dtmf")

    b = h.call(cid, "m-b", "CA0")
    twiml = h.event(b, {"CallSid": "CA0", "Digits": "0"}, gather=True)["twiml"]
    assert EN.callback_confirmation in twiml
    h.event(b, {"CallSid": "CA0", "CallStatus": "completed"})
    assert h.state(cid, "m-b") == ("escalated", "dtmf_0")
    [esc] = h.client.get("/escalations").json()
    assert (esc["kind"], esc["member_id"]) == ("member_request", "m-b")


def test_retries_then_failure_alert_through_the_outbox(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-a")
    a1 = h.call(cid, "m-a", "CA-1")
    h.event(a1, {"CallSid": "CA-1", "CallStatus": "no-answer"})
    assert h.state(cid, "m-a") == ("retry_wait", None)
    assert ("send", "m-a") not in h.work()
    h.clock.advance(minutes=59)
    assert ("send", "m-a") not in h.work()
    h.clock.advance(minutes=1)
    assert ("send", "m-a") in h.work()

    a2 = h.call(cid, "m-a", "CA-2")
    h.event(a2, {"CallSid": "CA-2", "CallStatus": "busy"})
    assert h.state(cid, "m-a") == ("failed", "2 calls, last: busy")

    outbox = h.client.get("/notifications/outbox").json()
    assert outbox["url"] == h.settings.notify_webhook_url
    [item] = outbox["items"]
    assert item["text"].startswith("[caregap] delivery_failed #")
    assert "m-a" in item["text"] and "555" not in item["text"]
    h.post(f"/escalations/{item['id']}/notified")
    assert h.client.get("/notifications/outbox").json()["items"] == []
    assert h.client.get(f"/campaigns/{cid}").json()["calls"] == {"busy": 1, "no-answer": 1}


def test_retry_waits_for_the_next_call_window(h: Harness) -> None:
    h.clock.at = datetime(2026, 1, 17, 19, 30, tzinfo=NY)  # Saturday evening
    cid = h.campaign()
    h.ready(cid, "m-a")
    a = h.call(cid, "m-a", "CA1")
    h.event(a, {"CallSid": "CA1", "CallStatus": "no-answer"})
    with psycopg.connect(h.db) as conn:
        row = conn.execute("SELECT next_call_at FROM campaign_members WHERE member_id = 'm-a'")
        assert row.fetchone() == (datetime(2026, 1, 19, 9, 0, tzinfo=NY),)  # Monday 9am
    h.clock.at = datetime(2026, 1, 18, 12, 0, tzinfo=NY)  # Sunday: window closed
    assert [w for w in h.work() if w[0] == "send"] == []
    assert h.post(f"/campaigns/{cid}/members/m-a/send")["reason"] == "outside the call window"


def test_consent_is_checked_again_at_call_time(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-b")
    with psycopg.connect(h.db) as conn:
        conn.execute("UPDATE consent SET status = 'revoked' WHERE member_id = 'm-b'")
    assert h.post(f"/campaigns/{cid}/members/m-b/send") == {
        "action": "skipped",
        "reason": "consent_revoked",
    }


def test_lost_callbacks_and_refused_calls_are_retried(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-a", "m-b")
    h.call(cid, "m-a", "CA-lost")
    out = h.post(f"/campaigns/{cid}/members/m-b/send")
    h.post(f"/attempts/{out['attempt_id']}/dispatch-failed", {"error": "HTTP 503"})
    assert h.state(cid, "m-b") == ("retry_wait", None)

    h.clock.advance(minutes=31)
    h.work()
    assert h.state(cid, "m-a") == ("retry_wait", None)
    calls = h.client.get(f"/campaigns/{cid}").json()["calls"]
    assert calls == {"failed": 2}


def test_bad_or_mismatched_callbacks_are_refused(h: Harness) -> None:
    cid = h.campaign()
    h.ready(cid, "m-a")
    a = h.call(cid, "m-a", "CA1")
    body = {"CallSid": "CA1", "CallStatus": "completed"}
    sig = twilio.signature("wrong", f"http://n8n:5678/webhook/twilio-events?attempt_id={a}", body)
    r = h.client.post(
        "/telephony/events",
        json={"query": {"attempt_id": str(a)}, "body": body, "signature": sig},
    )
    assert r.status_code == 403
    h.event(a, {"CallSid": "CA-other", "CallStatus": "completed"}, status=409)
    h.event(999999, {"CallSid": "CA1", "CallStatus": "completed"}, status=404)
    assert h.state(cid, "m-a") == ("calling", None)


def test_model_failure_returns_work_to_the_queue(db: str, tmp_path: Path) -> None:
    def down(_: list[Message]) -> str:
        raise ProviderError("Ollama unreachable")

    h = Harness(db, tmp_path, FakeProvider(down))
    cid = h.campaign()
    r = h.client.post(f"/campaigns/{cid}/members/m-a/prepare")
    assert r.status_code == 503
    assert h.state(cid, "m-a") == ("selected", None)


def test_campaign_is_done_when_only_people_are_left(h: Harness) -> None:
    with psycopg.connect(h.db) as conn:
        conn.execute("DELETE FROM consent WHERE member_id <> 'm-a'")
    cid = h.campaign()
    assert h.work() == [("prepare", "m-a")]
    h.post(f"/campaigns/{cid}/members/m-a/prepare")  # held: first N
    summary = h.client.get(f"/campaigns/{cid}").json()
    assert summary["states"] == {"awaiting_review": 1, "skipped": 7}
    assert summary["done"]


def test_alerts_and_the_notification_sink(h: Harness) -> None:
    out = h.post(
        "/alerts",
        {"workflow": "campaign-tick", "node": "Prepare", "message": "boom", "execution": "42"},
        status=201,
    )
    [item] = h.client.get("/notifications/outbox").json()["items"]
    assert item["id"] == out["escalation_id"]
    assert "workflow_error" in item["text"] and "boom" in item["text"]
    h.post("/notifications", {"text": item["text"]}, status=202)
    lines = (h.settings.data_dir / "notifications.log").read_text().splitlines()
    assert json.loads(lines[-1])["text"] == item["text"]


def test_callback_url_query_is_rebuilt_not_trusted(h: Harness) -> None:
    """A relay that adds query parameters cannot change what the signature covers."""
    cid = h.campaign()
    h.ready(cid, "m-a")
    a = h.call(cid, "m-a", "CA1")
    body = {"CallSid": "CA1", "CallStatus": "completed"}
    url = f"http://n8n:5678/webhook/twilio-events?attempt_id={a}"
    query = dict(parse_qsl(urlsplit(url).query)) | {"extra": "1"}
    sig = twilio.signature(TOKEN, url, body)
    h.post("/telephony/events", {"query": query, "body": body, "signature": sig})
    assert h.state(cid, "m-a") == ("completed", None)


def test_generation_is_capped_across_campaigns(h: Harness) -> None:
    cid = h.campaign()
    with psycopg.connect(h.db) as conn:
        conn.execute(
            "UPDATE campaign_members SET state = 'preparing', state_since = %s"
            " WHERE member_id IN ('m-a', 'm-b')",
            (TUESDAY_10AM,),
        )
    assert h.work() == []
    out = h.post(f"/campaigns/{cid}/members/m-c/prepare")
    assert (out["action"], out["reason"]) == ("none", "generation capacity in use")
    # A worker that died mid-generation frees its slot after 15 minutes.
    h.clock.advance(minutes=16)
    assert h.work() == [("prepare", "m-a"), ("prepare", "m-b")]


# A second customer for tests: under-65 members, eye exams only, weekday calls, no
# delivery-failure alerts, 7 to opt out and 1 for a call back.
YOUNG = CUSTOMER.model_copy(
    update={
        "id": "young",
        "members": CUSTOMER.members.model_copy(update={"min_age": None, "max_age": 64}),
        "measures": ("eye_exam",),
        "campaign": CUSTOMER.campaign.model_copy(update={"call_days": frozenset(range(1, 6))}),
        "escalation": CUSTOMER.escalation.model_copy(
            update={"opt_out_digit": "7", "callback_digit": "1",
                    "alert_on_delivery_failure": False}
        ),
    }
)  # fmt: skip


@pytest.fixture
def two(db: str, tmp_path: Path) -> Harness:
    with psycopg.connect(db) as conn:
        for mid, name, measure in [("y-eye", "Yan", "eye_exam"), ("y-statin", "Yul", "statin")]:
            conn.execute(
                "INSERT INTO members (member_id, given_name, family_name, birth_date, phone)"
                " VALUES (%s, %s, 'Synth', '1990-06-01', '555-020-0001')",
                (mid, name),
            )
            conn.execute(
                "INSERT INTO care_gaps (member_id, measure, measurement_year, status, reason)"
                " VALUES (%s, %s, 2025, 'gap', 'test')",
                (mid, measure),
            )
            conn.execute(
                "INSERT INTO consent (member_id, channel, status, source)"
                " VALUES (%s, 'voice', 'granted', 'test')",
                (mid,),
            )
    return Harness(db, tmp_path, FakeProvider(reply), {"sunrise": CUSTOMER, "young": YOUNG})


def test_customers_select_only_their_members_and_measures(two: Harness) -> None:
    young = two.post("/campaigns", {"customer": "young", "size": 20}, status=201)
    # y-statin is under 65 but has no eye-exam gap; Sunrise's 65+ members are not young's.
    assert (young["customer"], young["members"]) == ("young", 1)
    sunrise = two.post("/campaigns", {"customer": "sunrise", "size": 20}, status=201)
    assert sunrise["members"] == 8
    assert two.client.post("/campaigns", json={"customer": "nobody"}).status_code == 404


def test_each_customer_has_its_own_window_and_digits(two: Harness) -> None:
    cid = int(two.post("/campaigns", {"customer": "young", "size": 5}, status=201)["id"])
    two.ready(cid, "y-eye")
    with psycopg.connect(two.db) as conn:
        script = conn.execute(
            "SELECT customer, language, measures FROM outreach_scripts WHERE member_id = 'y-eye'"
        ).fetchone()
    assert script == ("young", "en", ["eye_exam"])
    two.clock.at = datetime(2026, 1, 17, 12, 0, tzinfo=NY)  # Saturday: young does not call
    assert two.post(f"/campaigns/{cid}/members/y-eye/send")["reason"] == "outside the call window"
    two.clock.at = TUESDAY_10AM
    a = two.call(cid, "y-eye", "CA-y")
    two.event(a, {"CallSid": "CA-y", "Digits": "9"}, gather=True)  # not young's opt-out key
    two.event(a, {"CallSid": "CA-y", "CallStatus": "completed"})
    assert two.state(cid, "y-eye") == ("completed", None)


def test_customers_without_delivery_alerts(two: Harness) -> None:
    cid = int(two.post("/campaigns", {"customer": "young", "size": 5}, status=201)["id"])
    two.ready(cid, "y-eye")
    for n in (1, 2):
        a = two.call(cid, "y-eye", f"CA-{n}")
        two.event(a, {"CallSid": f"CA-{n}", "CallStatus": "busy"})
        two.clock.advance(minutes=61)
        two.work()
    assert two.state(cid, "y-eye") == ("failed", "2 calls, last: busy")
    assert two.client.get("/escalations").json() == []
