"""Twilio's wire format, implemented by hand so the mock and the service agree on it.

Only what outbound calls need: request signatures (``X-Twilio-Signature``), the
create-call request, TwiML for playing a message and gathering one DTMF digit, and
E.164 phone numbers. A real Twilio account replaces the mock in Phase 11 with no change
here.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
from collections.abc import Mapping
from dataclasses import dataclass
from xml.sax.saxutils import escape, quoteattr


class InvalidSignatureError(PermissionError):
    """A request that claims to come from Twilio but is not signed with our token."""


#: CallStatus values after which Twilio sends no more status callbacks.
TERMINAL_STATUSES = frozenset({"completed", "no-answer", "busy", "failed", "canceled"})


def signature(auth_token: str, url: str, params: Mapping[str, str]) -> str:
    """Twilio's request signature: base64 HMAC-SHA1 of the URL followed by every POST
    parameter name and value, sorted by name."""
    data = url + "".join(k + params[k] for k in sorted(params))
    digest = hmac.new(auth_token.encode(), data.encode(), hashlib.sha1).digest()
    return base64.b64encode(digest).decode()


def valid_signature(auth_token: str, url: str, params: Mapping[str, str], sig: str) -> bool:
    return hmac.compare_digest(signature(auth_token, url, params), sig)


def basic_auth(account_sid: str, auth_token: str) -> str:
    return "Basic " + base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()


def to_e164(phone: str | None) -> str | None:
    """US numbers only (the synthetic population is in Massachusetts)."""
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return f"+1{digits}" if len(digits) == 10 else None


@dataclass(frozen=True)
class CallRequest:
    """A create-call request, in the form n8n posts it to Twilio. n8n adds the account's
    Basic auth from its own credential store (``make n8n-import``), so the auth token never
    appears in a request or in n8n's execution history."""

    url: str
    form: dict[str, str]


def call_request(
    *,
    api_base_url: str,
    account_sid: str,
    to: str,
    from_: str,
    twiml_url: str,
    status_callback: str,
    record: bool = False,
) -> CallRequest:
    form = {
        "To": to,
        "From": from_,
        "Url": twiml_url,
        "Method": "POST",
        "StatusCallback": status_callback,
        "StatusCallbackMethod": "POST",
    }
    if record:
        form["Record"] = "true"
    return CallRequest(
        url=f"{api_base_url.rstrip('/')}/2010-04-01/Accounts/{account_sid}/Calls.json",
        form=form,
    )


_XML = '<?xml version="1.0" encoding="UTF-8"?>'


def _say(text: str, language: str) -> str:
    return f"<Say language={quoteattr(language)}>{escape(text)}</Say>"


def play_and_gather(
    audio_url: str, action_url: str, *, closing: str, language: str = "en-US"
) -> str:
    """Play the message while listening for one digit; say ``closing`` if none comes."""
    return (
        f"{_XML}<Response>"
        f'<Gather numDigits="1" timeout="5" method="POST" action={quoteattr(action_url)}>'
        f"<Play>{escape(audio_url)}</Play></Gather>"
        f"{_say(closing, language)}<Hangup/></Response>"
    )


def say_and_hang_up(text: str, *, language: str = "en-US") -> str:
    return f"{_XML}<Response>{_say(text, language)}<Hangup/></Response>"


def ask_by_voice(
    prompt: str, action_url: str, *, closing: str, language: str = "en-US",
    before: str = "",
) -> str:  # fmt: skip
    """Say ``before`` (an answer), then ``prompt`` while listening for speech or one key;
    say ``closing`` and hang up if the caller says nothing."""
    first = _say(before, language) if before else ""
    return (
        f"{_XML}<Response>{first}"
        f'<Gather input="speech dtmf" numDigits="1" timeout="6" speechTimeout="auto" '
        f'language={quoteattr(language)} method="POST" action={quoteattr(action_url)}>'
        f"{_say(prompt, language)}</Gather>"
        f"{_say(closing, language)}<Hangup/></Response>"
    )
