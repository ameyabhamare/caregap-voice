"""Live mode (Phase 11) refuses to run half-configured."""

from __future__ import annotations

from typing import Any

from caregap import live
from caregap.config import Settings

LIVE: dict[str, Any] = {
    "live_mode": True,
    "live_allowed_numbers": "+15550109999, +15550108888",
    "live_demo_number": "+15550109999",
    "audio_store": "r2",
    "r2_endpoint": "https://acct.r2.cloudflarestorage.com",
    "r2_bucket": "caregap-audio",
    "r2_access_key_id": "ak",
    "r2_secret_access_key": "sk",
    "twilio_api_base_url": "https://api.twilio.com",
    "twilio_callback_url": "https://demo.trycloudflare.com/webhook/twilio-events",
    "twilio_account_sid": "AC" + "1" * 32,
}


def settings(**kw: Any) -> Settings:
    return Settings(_env_file=None, **{**LIVE, **kw})


def test_off_by_default_and_complete_live_settings_pass() -> None:
    assert live.problems(Settings(_env_file=None)) == []
    assert live.problems(settings()) == []
    assert live.allowed_numbers(settings()) == {"+15550109999", "+15550108888"}
    bucket = live.r2_bucket(settings())
    assert bucket is not None and bucket.object_url("k") == (
        "https://acct.r2.cloudflarestorage.com/caregap-audio/k"
    )
    assert live.r2_bucket(Settings(_env_file=None)) is None


def test_each_missing_or_unsafe_setting_is_named() -> None:
    cases = {
        "LIVE_ALLOWED_NUMBERS is empty": {"live_allowed_numbers": ""},
        "not in E.164": {"live_allowed_numbers": "+15550109999,555-0100"},
        "LIVE_DEMO_NUMBER must be one of": {"live_demo_number": "+15550100000"},
        "AUDIO_STORE must be r2": {"audio_store": "core"},
        "R2_SECRET_ACCESS_KEY is not set": {"r2_secret_access_key": ""},
        "R2_BUCKET is not set": {"r2_bucket": ""},
        "TWILIO_CALLBACK_URL must be an https:// URL": {
            "twilio_callback_url": "http://localhost:5678/webhook/twilio-events"
        },
        "TWILIO_API_BASE_URL must be an https:// URL": {"twilio_api_base_url": "http://mock"},
        "still the mock account": {"twilio_account_sid": "ACmock000"},
    }
    for expected, override in cases.items():
        found = live.problems(settings(**override))
        assert any(expected in p for p in found), (expected, found)
