"""A local stand-in for Twilio's outbound-call API.

    uvicorn --factory caregap.telephony.mock:create_app --port 8100

It speaks Twilio's wire format: ``POST /2010-04-01/Accounts/{sid}/Calls.json`` with basic
auth and a form body; signed (``X-Twilio-Signature``) form posts to the TwiML ``Url``, the
``<Gather>`` action and the ``StatusCallback``. For an answered call it fetches the TwiML,
downloads the ``<Play>`` audio and checks it is a WAV, so the delivery path is exercised
for real.

Outcomes are deterministic: a hash of the number and how many calls it has had picks
from a fixed distribution (see ``OUTCOMES``). Tests and demos can script the outcomes for
a number with ``PUT /mock/numbers/{to}``; ``POST /mock/reset`` clears all state.
"""

from __future__ import annotations

import hashlib
import io
import logging
import math
import os
import re
import threading
import time
import uuid
import wave
import xml.etree.ElementTree as ET
from collections import Counter, deque
from collections.abc import Callable
from typing import Any
from urllib.parse import parse_qsl

import httpx
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from caregap.telephony.twilio import basic_auth, signature

log = logging.getLogger(__name__)

#: Outcome -> share of calls in percent. api-error is an HTTP 503 from the create-call
#: request itself (exercises the orchestrator's retries); dtmf-N answers and presses N.
OUTCOMES: dict[str, int] = {
    "completed": 55,
    "no-answer": 18,
    "busy": 6,
    "failed": 4,
    "dtmf-9": 7,
    "dtmf-0": 6,
    "api-error": 4,
}
E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def pick_outcome(to: str, n: int) -> str:
    """The outcome of call number ``n`` (0-based) to ``to``."""
    bucket = int.from_bytes(hashlib.sha256(f"{to}:{n}".encode()).digest()[:4]) % 100
    for outcome, share in OUTCOMES.items():
        if bucket < share:
            return outcome
        bucket -= share
    raise AssertionError("OUTCOMES must sum to 100")


def _error(status: int, code: int, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message, "status": status}, status_code=status)


def wav_seconds(data: bytes) -> float:
    with wave.open(io.BytesIO(data)) as w:
        return float(w.getnframes() / w.getframerate())


class Outcomes(BaseModel):
    outcomes: list[str]


class MockTwilio:
    def __init__(
        self, account_sid: str, auth_token: str, delay_s: float, http: httpx.Client
    ) -> None:
        self.account_sid = account_sid
        self.auth_token = auth_token
        self.delay_s = delay_s
        self.http = http
        self.lock = threading.Lock()
        self.reset()

    def reset(self) -> None:
        with self.lock:
            self.calls: dict[str, dict[str, Any]] = {}
            self.counts: Counter[str] = Counter()
            self.scripted: dict[str, deque[str]] = {}

    def next_outcome(self, to: str) -> str:
        with self.lock:
            n = self.counts[to]
            self.counts[to] += 1
            queue = self.scripted.get(to)
            return queue.popleft() if queue else pick_outcome(to, n)

    def _post(self, call: dict[str, Any], url: str, params: dict[str, str]) -> httpx.Response:
        """A signed form post, the way Twilio calls a webhook."""
        sig = signature(self.auth_token, url, params)
        r = self.http.post(url, data=params, headers={"X-Twilio-Signature": sig})
        call["events"].append({"url": url, "params": params, "status_code": r.status_code})
        return r

    def simulate(self, call: dict[str, Any]) -> None:
        time.sleep(self.delay_s)
        form = call["request"]
        base = {
            "AccountSid": self.account_sid,
            "CallSid": call["sid"],
            "From": form["From"],
            "To": form["To"],
            "Direction": "outbound-api",
        }
        status, duration = call["outcome"], 0
        try:
            if status in ("completed", "dtmf-9", "dtmf-0"):
                status, duration = self._answer(call, base)
        except (httpx.HTTPError, ET.ParseError, wave.Error, EOFError, ValueError) as exc:
            # Twilio would play an application error; for the campaign it is a failed call.
            log.warning("call %s: %s", call["sid"], exc)
            call["error"] = str(exc)
            status = "failed"
        call["status"] = status
        if callback := form.get("StatusCallback"):
            params = {**base, "CallStatus": status, "CallDuration": str(duration)}
            try:
                self._post(call, callback, params)
            except httpx.HTTPError as exc:
                log.warning("call %s: status callback failed: %s", call["sid"], exc)
                call["error"] = f"status callback failed: {exc}"

    def _answer(self, call: dict[str, Any], base: dict[str, str]) -> tuple[str, int]:
        r = self._post(call, call["request"]["Url"], {**base, "CallStatus": "in-progress"})
        r.raise_for_status()
        root = ET.fromstring(r.text)  # noqa: S314 - TwiML from the service under test
        play_url = root.findtext(".//Play")
        if not play_url:
            raise ValueError("TwiML has no <Play>")
        audio = self.http.get(play_url)
        audio.raise_for_status()
        if audio.content[:4] != b"RIFF":
            raise ValueError("audio is not a WAV file")
        seconds = wav_seconds(audio.content)
        call["audio_seconds"] = round(seconds, 2)
        if call["outcome"].startswith("dtmf-"):
            gather = root.find(".//Gather")
            action = gather.get("action") if gather is not None else None
            if not action:
                raise ValueError("TwiML has no <Gather action>")
            digits = call["outcome"].removeprefix("dtmf-")
            reply = self._post(call, action, {**base, "CallStatus": "in-progress",
                                              "Digits": digits})  # fmt: skip
            reply.raise_for_status()
        return "completed", math.ceil(seconds)


def create_app(
    *,
    account_sid: str | None = None,
    auth_token: str | None = None,
    delay_s: float | None = None,
    http: httpx.Client | None = None,
) -> FastAPI:
    from caregap.config import get_settings

    settings = get_settings()
    mock = MockTwilio(
        account_sid or settings.twilio_account_sid,
        auth_token or settings.twilio_auth_token.get_secret_value(),
        float(os.environ.get("MOCK_CALL_DELAY_S", "1.0")) if delay_s is None else delay_s,
        http or httpx.Client(timeout=60),
    )
    app = FastAPI(title="Twilio mock", version="0.1.0")
    app.state.mock = mock

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/2010-04-01/Accounts/{sid}/Calls.json", response_model=None)
    async def create_call(sid: str, request: Request, background: BackgroundTasks) -> JSONResponse:
        if sid != mock.account_sid or request.headers.get("authorization") != basic_auth(
            mock.account_sid, mock.auth_token
        ):
            return _error(401, 20003, "Authenticate")
        form = dict(parse_qsl((await request.body()).decode(), keep_blank_values=True))
        for field in ("To", "From", "Url"):
            if not form.get(field):
                return _error(400, 21205 if field == "Url" else 21201, f"{field} is required")
        if not E164.match(form["To"]):
            return _error(400, 21211, f"Invalid 'To' Phone Number: {form['To']}")
        outcome = mock.next_outcome(form["To"])
        if outcome == "api-error":
            return _error(503, 20500, "Service unavailable (mock)")
        call_sid = "CA" + uuid.uuid4().hex
        call: dict[str, Any] = {
            "sid": call_sid,
            "request": form,
            "outcome": outcome,
            "status": "queued",
            "events": [],
        }
        mock.calls[call_sid] = call
        simulate: Callable[[dict[str, Any]], None] = mock.simulate
        background.add_task(simulate, call)
        body = {
            "sid": call_sid,
            "account_sid": mock.account_sid,
            "to": form["To"],
            "from": form["From"],
            "status": "queued",
        }
        return JSONResponse(body, status_code=201)

    @app.get("/mock/calls")
    def calls() -> list[dict[str, Any]]:
        return list(mock.calls.values())

    @app.put("/mock/numbers/{to}")
    def script_number(to: str, req: Outcomes) -> dict[str, Any]:
        unknown = set(req.outcomes) - set(OUTCOMES)
        if unknown:
            raise HTTPException(422, f"unknown outcomes {sorted(unknown)}")
        mock.scripted[to] = deque(req.outcomes)
        return {"to": to, "outcomes": req.outcomes}

    @app.post("/mock/reset")
    def reset() -> dict[str, str]:
        mock.reset()
        return {"status": "ok"}

    return app
