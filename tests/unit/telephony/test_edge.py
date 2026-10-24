"""The live tunnel's edge filter forwards only Twilio's webhook to n8n."""

from __future__ import annotations

import httpx
from fastapi.testclient import TestClient

from caregap.telephony.edge import MAX_BODY, create_app


def edge() -> tuple[TestClient, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def n8n(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, text="<Response/>", headers={"content-type": "application/xml"})

    http = httpx.Client(transport=httpx.MockTransport(n8n))
    return TestClient(create_app(upstream="http://n8n:5678/webhook/twilio-events", http=http)), seen


def test_forwards_the_webhook_with_query_body_and_signature() -> None:
    c, seen = edge()
    r = c.post("/webhook/twilio-events?attempt_id=7&event=twiml", content=b"CallSid=CA1",
               headers={"X-Twilio-Signature": "sig", "Content-Type":
                        "application/x-www-form-urlencoded", "Cookie": "x"})  # fmt: skip
    assert (r.status_code, r.text) == (200, "<Response/>")
    [req] = seen
    assert str(req.url) == "http://n8n:5678/webhook/twilio-events?attempt_id=7&event=twiml"
    assert req.content == b"CallSid=CA1" and req.headers["x-twilio-signature"] == "sig"
    assert "cookie" not in req.headers


def test_everything_else_is_closed() -> None:
    c, seen = edge()
    assert c.get("/").status_code == 404  # the n8n editor
    assert c.get("/rest/workflows").status_code == 404
    assert c.post("/webhook/campaign-tick").status_code == 404
    assert c.get("/webhook/twilio-events").status_code == 405
    assert c.post("/webhook/twilio-events", content=b"x" * (MAX_BODY + 1)).status_code == 413
    assert c.get("/docs").status_code == 404
    assert seen == []
