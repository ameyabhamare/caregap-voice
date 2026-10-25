"""R2 presigned URLs: AWS SigV4 query signing, checked against AWS's published example
("Authenticating Requests: Using Query Parameters", Amazon S3 API reference)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

import pytest

from caregap.storage.r2 import R2Bucket, presign

AWS_EXAMPLE = (
    "https://examplebucket.s3.amazonaws.com/test.txt?X-Amz-Algorithm=AWS4-HMAC-SHA256"
    "&X-Amz-Credential=AKIAIOSFODNN7EXAMPLE%2F20130524%2Fus-east-1%2Fs3%2Faws4_request"
    "&X-Amz-Date=20130524T000000Z&X-Amz-Expires=86400&X-Amz-SignedHeaders=host"
    "&X-Amz-Signature=aeeed9bbccd4d02ee5c0109b86d86835f995330da4c265957d157751f604d404"
)


def test_matches_the_aws_example() -> None:
    url = presign(
        "GET", "https://examplebucket.s3.amazonaws.com/test.txt",
        access_key="AKIAIOSFODNN7EXAMPLE",
        secret_key="wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        region="us-east-1", expires_s=86400, now=datetime(2013, 5, 24, tzinfo=UTC),
    )  # fmt: skip
    assert url == AWS_EXAMPLE


def test_r2_urls_are_path_style_short_lived_and_method_specific() -> None:
    b = R2Bucket("https://acct.r2.cloudflarestorage.com", "caregap-audio", "ak", "sk")
    now = datetime(2026, 10, 24, 12, tzinfo=UTC)
    put = b.put_url("calls/7.wav", expires_s=600, now=now)
    get = b.get_url("calls/7.wav", expires_s=900, now=now)
    parts = urlsplit(put)
    assert (parts.netloc, parts.path) == ("acct.r2.cloudflarestorage.com",
                                          "/caregap-audio/calls/7.wav")  # fmt: skip
    q = parse_qs(parts.query)
    assert q["X-Amz-Credential"] == ["ak/20261024/auto/s3/aws4_request"]
    assert q["X-Amz-Expires"] == ["600"]
    assert parse_qs(urlsplit(get).query)["X-Amz-Expires"] == ["900"]
    sig = parse_qs(urlsplit(get).query)["X-Amz-Signature"][0]
    assert sig != q["X-Amz-Signature"][0]  # PUT and GET sign differently


def test_rejects_bad_input() -> None:
    kw = {"access_key": "a", "secret_key": "s", "region": "auto",
          "now": datetime(2026, 1, 1, tzinfo=UTC)}  # fmt: skip
    with pytest.raises(ValueError, match="expires_s"):
        presign("GET", "https://h/b/k", expires_s=0, **kw)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="query"):
        presign("GET", "https://h/b/k?x=1", expires_s=60, **kw)  # type: ignore[arg-type]
