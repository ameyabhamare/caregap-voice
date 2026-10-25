"""The only thing the live-mode tunnel exposes (Phase 11).

    uvicorn --factory caregap.telephony.edge:create_app --port 8200   (compose: edge)

A quick Cloudflare tunnel publishes everything behind its origin, and n8n's editor must
not be public. So the tunnel points here, and this forwards exactly one route to n8n:
``POST /webhook/twilio-events`` (Twilio's TwiML fetches, keypresses, call status and
inbound calls), with its query string, form body and ``X-Twilio-Signature``. Everything
else is a 404. The core service checks every signature; this filter only limits reach.
"""

from __future__ import annotations

import os

import httpx
from fastapi import FastAPI, Request, Response

PATH = "/webhook/twilio-events"
#: Twilio's form posts are small; anything bigger is not Twilio.
MAX_BODY = 64 * 1024
FORWARDED_HEADERS = ("content-type", "x-twilio-signature", "i-twilio-idempotency-token")


def create_app(*, upstream: str | None = None, http: httpx.Client | None = None) -> FastAPI:
    target = upstream or os.environ.get("N8N_WEBHOOK_URL", "http://n8n:5678" + PATH)
    client = http or httpx.Client(timeout=30)
    app = FastAPI(title="CareGap edge", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(PATH)
    async def twilio_events(request: Request) -> Response:
        body = await request.body()
        if len(body) > MAX_BODY:
            return Response(status_code=413)
        headers = {h: request.headers[h] for h in FORWARDED_HEADERS if h in request.headers}
        try:
            r = client.post(target, params=request.query_params, content=body, headers=headers)
        except httpx.HTTPError:
            return Response(status_code=502)
        return Response(
            r.content, status_code=r.status_code,
            media_type=r.headers.get("content-type", "application/xml"),
        )  # fmt: skip

    return app
