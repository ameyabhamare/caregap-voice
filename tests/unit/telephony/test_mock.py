from __future__ import annotations

from collections import Counter
from urllib.parse import parse_qsl

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from caregap.telephony import twilio
from caregap.telephony.mock import OUTCOMES, create_app, pick_outcome
from caregap.voice.base import Audio

SID, TOKEN = "ACtest", "tok"
AUTH = {"Authorization": twilio.basic_auth(SID, TOKEN)}
URL = f"/2010-04-01/Accounts/{SID}/Calls.json"
WAV = Audio(np.zeros(24000 * 3 + 100, dtype=np.float32), 24000).to_wav()  # 3.004 s


class FakeService:
    """The core service and n8n, as the mock sees them: TwiML, audio and callbacks."""

    def __init__(self, *, audio: bytes = WAV) -> None:
        self.audio = audio
        self.posts: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if request.method == "GET":
            return httpx.Response(200, content=self.audio)
        params = dict(parse_qsl(request.content.decode()))
        sig = request.headers["X-Twilio-Signature"]
        assert twilio.valid_signature(TOKEN, url, params, sig), url
        self.posts.append((url, params))
        if url.startswith("http://core/twiml"):
            doc = twilio.play_and_gather(
                "http://core/scripts/1/audio", "http://n8n/hook?attempt_id=1&event=gather",
                closing="Bye",
            )  # fmt: skip
            return httpx.Response(200, text=doc)
        return httpx.Response(200, json={})


def client(service: FakeService) -> TestClient:
    http = httpx.Client(transport=httpx.MockTransport(service))
    return TestClient(create_app(account_sid=SID, auth_token=TOKEN, delay_s=0, http=http))


FORM = {
    "To": "+15550100001",
    "From": "+15550100000",
    "Url": "http://core/twiml/attempts/1",
    "StatusCallback": "http://n8n/hook?attempt_id=1",
}


def script(c: TestClient, *outcomes: str) -> None:
    assert c.put(f"/mock/numbers/{FORM['To']}", json={"outcomes": list(outcomes)}).is_success


def test_outcome_distribution_is_deterministic() -> None:
    assert sum(OUTCOMES.values()) == 100
    counts = Counter(pick_outcome(f"+1555{i:07d}", 0) for i in range(5000))
    assert counts.keys() == OUTCOMES.keys()
    assert abs(counts["completed"] / 5000 - 0.55) < 0.03
    assert pick_outcome("+15550100001", 3) == pick_outcome("+15550100001", 3)


def test_answered_call_fetches_twiml_and_audio_then_reports_completed() -> None:
    service = FakeService()
    c = client(service)
    script(c, "completed")
    r = c.post(URL, data=FORM, headers=AUTH)
    assert r.status_code == 201
    sid = r.json()["sid"]
    assert sid.startswith("CA")
    urls = [u for u, _ in service.posts]
    assert urls == [FORM["Url"], FORM["StatusCallback"]]
    status = service.posts[-1][1]
    assert (status["CallSid"], status["CallStatus"], status["CallDuration"]) == (
        sid,
        "completed",
        "4",
    )
    [call] = c.get("/mock/calls").json()
    assert (call["status"], call["audio_seconds"]) == ("completed", 3.0)


@pytest.mark.parametrize("digit", ["9", "0"])
def test_dtmf_posts_digits_to_the_gather_action(digit: str) -> None:
    service = FakeService()
    c = client(service)
    script(c, f"dtmf-{digit}")
    c.post(URL, data=FORM, headers=AUTH)
    gather = service.posts[1]
    assert gather[0] == "http://n8n/hook?attempt_id=1&event=gather"
    assert gather[1]["Digits"] == digit
    assert service.posts[2][1]["CallStatus"] == "completed"


@pytest.mark.parametrize("outcome", ["no-answer", "busy", "failed"])
def test_unanswered_calls_only_send_a_status(outcome: str) -> None:
    service = FakeService()
    c = client(service)
    script(c, outcome)
    c.post(URL, data=FORM, headers=AUTH)
    assert [(u, p["CallStatus"]) for u, p in service.posts] == [(FORM["StatusCallback"], outcome)]


def test_bad_audio_fails_the_call() -> None:
    service = FakeService(audio=b"<html>not found</html>")
    c = client(service)
    script(c, "completed")
    c.post(URL, data=FORM, headers=AUTH)
    assert service.posts[-1][1]["CallStatus"] == "failed"
    assert "WAV" in c.get("/mock/calls").json()[0]["error"]


def test_api_errors_auth_and_validation() -> None:
    c = client(FakeService())
    script(c, "api-error", "completed")
    assert c.post(URL, data=FORM, headers=AUTH).status_code == 503
    assert c.post(URL, data=FORM, headers=AUTH).status_code == 201  # retry succeeds
    assert c.post(URL, data=FORM).status_code == 401
    other = {"Authorization": twilio.basic_auth(SID, "wrong")}
    assert c.post(URL, data=FORM, headers=other).status_code == 401
    r = c.post(URL, data={**FORM, "To": "555-0100"}, headers=AUTH)
    assert (r.status_code, r.json()["code"]) == (400, 21211)
    assert c.post(URL, data={**FORM, "Url": ""}, headers=AUTH).status_code == 400
    assert c.put("/mock/numbers/x", json={"outcomes": ["nope"]}).status_code == 422


def test_reset_clears_calls_and_counters() -> None:
    c = client(FakeService())
    script(c, "busy")
    c.post(URL, data=FORM, headers=AUTH)
    assert c.post("/mock/reset").is_success
    assert c.get("/mock/calls").json() == []
    first = pick_outcome(FORM["To"], 0)
    c.post(URL, data=FORM, headers=AUTH)
    if first != "api-error":
        assert c.get("/mock/calls").json()[0]["outcome"] == first


def test_fake_r2_accepts_only_valid_unexpired_presigned_urls() -> None:
    from datetime import UTC, datetime, timedelta

    from caregap.storage.r2 import R2Bucket

    c = TestClient(create_app(account_sid=SID, auth_token=TOKEN, delay_s=0,
                              r2_credentials=("ak", "sk")))  # fmt: skip
    bucket = R2Bucket("http://testserver/r2", "audio", "ak", "sk")
    put = bucket.put_url("calls/1.wav", expires_s=60)
    assert c.put(put, content=b"RIFF....").status_code == 200
    got = c.get(bucket.get_url("calls/1.wav", expires_s=60))
    assert (got.status_code, got.content) == (200, b"RIFF....")
    assert c.get(put).status_code == 403  # a PUT URL cannot be used to read
    wrong = R2Bucket("http://testserver/r2", "audio", "ak", "other-secret")
    assert c.get(wrong.get_url("calls/1.wav", expires_s=60)).status_code == 403
    old = bucket.get_url("calls/1.wav", expires_s=60,
                         now=datetime.now(UTC) - timedelta(minutes=5))  # fmt: skip
    assert c.get(old).json()["detail"] == "Request has expired"
    assert c.get(bucket.get_url("calls/2.wav", expires_s=60)).status_code == 404
