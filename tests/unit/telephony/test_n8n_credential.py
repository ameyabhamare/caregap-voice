from __future__ import annotations

import json

import pytest
from pydantic import SecretStr

from caregap.config import Settings
from caregap.telephony import n8n_credential


def test_credential_follows_the_core_settings() -> None:
    settings = Settings(twilio_account_sid="AC123", twilio_auth_token=SecretStr("tok"))
    assert n8n_credential.twilio_credential(settings) == [
        {
            "id": "cgTwilioAuth0001",
            "name": "CareGap: Twilio account",
            "type": "httpBasicAuth",
            "data": {"user": "AC123", "password": "tok"},
        }
    ]


def test_main_prints_the_import_payload(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = Settings(twilio_account_sid="AC9", twilio_auth_token=SecretStr("t9"))
    monkeypatch.setattr(n8n_credential, "get_settings", lambda: settings)
    n8n_credential.main()
    assert json.loads(capsys.readouterr().out)[0]["data"] == {"user": "AC9", "password": "t9"}
