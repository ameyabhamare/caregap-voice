"""Live demo mode (Phase 11): the checks that keep a real Twilio account safe.

With ``LIVE_MODE=true`` the core service refuses to start unless every check passes, and
``caregap.campaign.service`` refuses calls beyond them:
- calls go only to ``LIVE_DEMO_NUMBER``, which must be on ``LIVE_ALLOWED_NUMBERS``
  (Twilio trial accounts can only call verified numbers anyway);
- at most ``LIVE_MAX_CALLS`` calls in all live campaigns, one member per live campaign;
- audio goes through R2 with short-lived signed URLs, so the core service stays private
  and only the n8n Twilio webhook is public (through the tunnel's edge filter);
- Twilio talks to HTTPS URLs only.
"""

from __future__ import annotations

import re

from caregap.config import Settings
from caregap.storage.r2 import R2Bucket

E164 = re.compile(r"^\+[1-9]\d{6,14}$")


def allowed_numbers(settings: Settings) -> frozenset[str]:
    return frozenset(n.strip() for n in settings.live_allowed_numbers.split(",") if n.strip())


def problems(settings: Settings) -> list[str]:
    """Why live mode cannot run with these settings (empty when it can)."""
    if not settings.live_mode:
        return []
    out = []
    allowed = allowed_numbers(settings)
    if not allowed:
        out.append("LIVE_ALLOWED_NUMBERS is empty")
    if bad := sorted(n for n in allowed if not E164.match(n)):
        out.append(f"LIVE_ALLOWED_NUMBERS has numbers not in E.164 form: {bad}")
    if settings.live_demo_number not in allowed:
        out.append("LIVE_DEMO_NUMBER must be one of LIVE_ALLOWED_NUMBERS")
    if settings.audio_store != "r2":
        out.append("AUDIO_STORE must be r2 in live mode (the core service stays private)")
    for name in ("r2_endpoint", "r2_bucket", "r2_access_key_id"):
        if not getattr(settings, name):
            out.append(f"{name.upper()} is not set")
    if not settings.r2_secret_access_key.get_secret_value():
        out.append("R2_SECRET_ACCESS_KEY is not set")
    for name in ("twilio_api_base_url", "twilio_callback_url", "r2_endpoint"):
        if not getattr(settings, name).startswith("https://"):
            out.append(f"{name.upper()} must be an https:// URL in live mode")
    if settings.twilio_account_sid.startswith("ACmock"):
        out.append("TWILIO_ACCOUNT_SID is still the mock account")
    return out


def r2_bucket(settings: Settings) -> R2Bucket | None:
    if settings.audio_store != "r2":
        return None
    return R2Bucket(
        endpoint=settings.r2_endpoint,
        bucket=settings.r2_bucket,
        access_key=settings.r2_access_key_id,
        secret_key=settings.r2_secret_access_key.get_secret_value(),
    )
