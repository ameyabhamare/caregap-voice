"""Live demo helpers: the tunnel URL, .env updates, the Twilio recording download."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from caregap.campaign.live import download_recording, set_env, tunnel_url
from caregap.config import Settings

LOGS = """
2026-10-24T10:00:00Z INF Requesting new quick Tunnel on trycloudflare.com...
2026-10-24T10:00:01Z INF |  https://brave-otter-sample-words.trycloudflare.com  |
"""


def test_tunnel_url_and_env_file(tmp_path: Path) -> None:
    assert tunnel_url(LOGS) == "https://brave-otter-sample-words.trycloudflare.com"
    assert tunnel_url("no url yet") is None
    env = tmp_path / ".env"
    env.write_text("A=1\nLIVE_WEBHOOK_URL=https://old.trycloudflare.com/x\nB=2\n")
    set_env(env, "LIVE_WEBHOOK_URL", "https://new.trycloudflare.com/webhook/twilio-events")
    assert env.read_text().splitlines() == [
        "A=1", "B=2", "LIVE_WEBHOOK_URL=https://new.trycloudflare.com/webhook/twilio-events"
    ]  # fmt: skip


def test_recording_waits_for_twilio_then_downloads(tmp_path: Path) -> None:
    calls: list[str] = []

    def twilio(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        assert request.headers["authorization"].startswith("Basic ")
        if request.url.path.endswith("Recordings.json"):
            ready = len(calls) > 1
            return httpx.Response(200, json={"recordings": [{"sid": "RE1"}] if ready else []})
        return httpx.Response(200, content=b"ID3mp3")

    settings = Settings(_env_file=None, twilio_api_base_url="https://api.twilio.com",
                        twilio_account_sid="AC1")  # fmt: skip
    http = httpx.Client(transport=httpx.MockTransport(twilio))
    out = download_recording(
        http, settings, "CA9", tmp_path / "demo/call.mp3", sleep=lambda _: None
    )
    assert out.read_bytes() == b"ID3mp3"
    assert calls == ["/2010-04-01/Accounts/AC1/Recordings.json"] * 2 + [
        "/2010-04-01/Accounts/AC1/Recordings/RE1.mp3"
    ]
    never = httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json={"recordings": []})))  # fmt: skip
    with pytest.raises(SystemExit, match="no recording"):
        download_recording(never, settings, "CA9", tmp_path / "x.mp3", attempts=2,
                           sleep=lambda _: None)  # fmt: skip
