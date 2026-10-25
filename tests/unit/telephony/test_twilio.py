from __future__ import annotations

import xml.etree.ElementTree as ET

from caregap.telephony import twilio


def test_signature_matches_twilios_documented_example() -> None:
    # The test vector from twilio-python's RequestValidator tests.
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"
    params = {
        "CallSid": "CA1234567890ABCDE",
        "Caller": "+14158675309",
        "Digits": "1234",
        "From": "+14158675309",
        "To": "+18005551212",
    }
    sig = twilio.signature("12345", url, params)
    assert sig == "RSOYDt4T1cUTdK1PDd93/VVr8B8="
    assert twilio.valid_signature("12345", url, params, sig)
    assert not twilio.valid_signature("12345", url, {**params, "Digits": "9"}, sig)
    assert not twilio.valid_signature("other", url, params, sig)


def test_to_e164() -> None:
    assert twilio.to_e164("555-322-4433") == "+15553224433"
    assert twilio.to_e164("1 (555) 322-4433") == "+15553224433"
    assert twilio.to_e164("322-4433") is None
    assert twilio.to_e164(None) is None


def test_call_request() -> None:
    req = twilio.call_request(
        api_base_url="http://mock:8100/",
        account_sid="ACx",
        to="+15553224433",
        from_="+15550100000",
        twiml_url="http://core/twiml/attempts/1",
        status_callback="http://n8n/webhook/twilio-events?attempt_id=1",
    )
    assert req.url == "http://mock:8100/2010-04-01/Accounts/ACx/Calls.json"
    assert req.form["StatusCallback"].endswith("attempt_id=1")


def test_basic_auth() -> None:
    assert twilio.basic_auth("ACx", "tok") == "Basic QUN4OnRvaw=="


def test_twiml_is_well_formed_and_escaped() -> None:
    doc = twilio.play_and_gather(
        "http://core/scripts/1/audio", "http://n8n/hook?attempt_id=1&event=gather", closing="Bye"
    )
    root = ET.fromstring(doc)
    gather = root.find("Gather")
    assert gather is not None
    assert gather.get("action") == "http://n8n/hook?attempt_id=1&event=gather"
    assert root.findtext("Gather/Play") == "http://core/scripts/1/audio"
    assert root.findtext("Say") == "Bye"
    assert ET.fromstring(twilio.say_and_hang_up("a < b")).findtext("Say") == "a < b"
