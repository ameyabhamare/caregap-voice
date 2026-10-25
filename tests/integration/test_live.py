"""Live mode (Phase 11) against Postgres: one-member campaigns, calls only to the demo
number and at most LIVE_MAX_CALLS of them, audio through R2 signed URLs, recording on.
No real Twilio or R2: nothing here leaves the machine (needs `make up`)."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from caregap.api.app import create_app
from caregap.config import Settings
from tests.integration.test_campaign import Harness, reply
from tests.integration.test_campaign import db as db
from tests.unit.llm.fakes import FakeProvider
from tests.unit.test_live import LIVE

pytestmark = pytest.mark.integration

DEMO = LIVE["live_demo_number"]


@pytest.fixture
def live(db: str, tmp_path: Path) -> Harness:
    import psycopg

    with psycopg.connect(db) as conn:
        conn.execute("UPDATE campaigns SET live = false")
    return Harness(db, tmp_path, FakeProvider(reply), **{**LIVE, "live_max_calls": 1})


def test_live_calls_go_only_to_the_demo_number_with_audio_through_r2(live: Harness) -> None:
    live.campaign(size=20, status=409)  # one member per live campaign
    cid = live.campaign(size=1)
    [mid] = live.selected(cid)
    live.ready(cid, mid)
    out = live.post(f"/campaigns/{cid}/members/{mid}/send")
    aid = out["attempt_id"]
    form = out["twilio"]["form"]
    assert out["twilio"]["url"].startswith("https://api.twilio.com/2010-04-01/Accounts/AC1")
    assert (form["To"], form["Record"]) == (DEMO, "true")  # never the synthetic number
    assert form["Url"].startswith("https://demo.trycloudflare.com/webhook/twilio-events?")
    put = urlsplit(out["upload"]["put_url"])
    assert (put.netloc, put.path) == ("acct.r2.cloudflarestorage.com",
                                      f"/caregap-audio/calls/{aid}.wav")  # fmt: skip
    assert parse_qs(put.query)["X-Amz-Expires"] == ["600"]
    assert out["upload"]["script_id"] > 0
    live.post(f"/attempts/{aid}/dispatched", {"call_sid": "CAlive1"})
    twiml = live.event(aid, {"CallSid": "CAlive1", "CallStatus": "in-progress"},
                       event="twiml")["twiml"]  # fmt: skip
    play = twiml.split("<Play>")[1].split("</Play>")[0].replace("&amp;", "&")
    get = urlsplit(play)
    assert (get.netloc, get.path) == (put.netloc, put.path)
    assert parse_qs(get.query)["X-Amz-Expires"] == ["900"]
    assert "core:8000" not in twiml  # the core service is not reachable from Twilio


def test_live_calls_stop_at_the_cap(live: Harness) -> None:
    first = live.campaign(size=1)
    [a] = live.selected(first)
    live.ready(first, a)
    live.post(f"/campaigns/{first}/members/{a}/send")
    second = live.campaign(size=1)
    [b] = live.selected(second)
    live.ready(second, b)
    r = live.client.post(f"/campaigns/{second}/members/{b}/send")
    assert r.status_code == 409 and "LIVE_MAX_CALLS" in r.json()["detail"], r.text


def test_half_configured_live_mode_does_not_start(db: str) -> None:
    settings = Settings(_env_file=None, database_url=db, **{**LIVE, "r2_bucket": ""})
    with pytest.raises(RuntimeError, match="R2_BUCKET is not set"):
        create_app(settings)
