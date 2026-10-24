"""Short-lived signed URLs for Cloudflare R2 (S3-compatible), Phase 11 live mode.

The core service never moves audio to R2 itself: it hands n8n a signed PUT URL for the
call's audio, and puts a signed GET URL in the TwiML Twilio fetches. Both expire within
minutes; a lifecycle rule on the bucket deletes objects after a day (docs/live-demo.md).

AWS Signature Version 4, query-string form ("presigned URL"), implemented with the
standard library and checked against AWS's published example (tests/unit/test_r2.py).
R2 uses region ``auto`` and path-style URLs: ``https://<account>.r2.cloudflarestorage.com
/<bucket>/<key>``.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote, urlsplit

UNSIGNED = "UNSIGNED-PAYLOAD"


def _uri(text: str, *, keep_slash: bool) -> str:
    return quote(text, safe="/~" if keep_slash else "~")


def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode(), hashlib.sha256).digest()


def presign(
    method: str, url: str, *, access_key: str, secret_key: str, region: str,
    expires_s: int, now: datetime, service: str = "s3",
) -> str:  # fmt: skip
    """A presigned URL for ``method url``, valid for ``expires_s`` seconds from ``now``."""
    if not 1 <= expires_s <= 604800:
        raise ValueError("expires_s must be between 1 second and 7 days")
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        raise ValueError("url must not have a query or fragment")
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{stamp[:8]}/{region}/{service}/aws4_request"
    query = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": stamp,
        "X-Amz-Expires": str(expires_s),
        "X-Amz-SignedHeaders": "host",
    }
    canonical_query = "&".join(
        f"{_uri(k, keep_slash=False)}={_uri(v, keep_slash=False)}" for k, v in sorted(query.items())
    )
    path = _uri(parts.path or "/", keep_slash=True)
    canonical = "\n".join(
        [method.upper(), path, canonical_query, f"host:{parts.netloc}\n", "host", UNSIGNED]
    )
    to_sign = "\n".join(
        ["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(f"AWS4{secret_key}".encode(), stamp[:8])
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    sig = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return f"{parts.scheme}://{parts.netloc}{path}?{canonical_query}&X-Amz-Signature={sig}"


@dataclass(frozen=True)
class R2Bucket:
    """One bucket. ``endpoint`` is ``https://<account_id>.r2.cloudflarestorage.com``."""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "auto"

    def object_url(self, key: str) -> str:
        return f"{self.endpoint.rstrip('/')}/{self.bucket}/{key.lstrip('/')}"

    def put_url(self, key: str, *, expires_s: int, now: datetime | None = None) -> str:
        return self._sign("PUT", key, expires_s, now)

    def get_url(self, key: str, *, expires_s: int, now: datetime | None = None) -> str:
        return self._sign("GET", key, expires_s, now)

    def _sign(self, method: str, key: str, expires_s: int, now: datetime | None) -> str:
        return presign(
            method, self.object_url(key), access_key=self.access_key,
            secret_key=self.secret_key, region=self.region, expires_s=expires_s,
            now=now or datetime.now(UTC),
        )  # fmt: skip
