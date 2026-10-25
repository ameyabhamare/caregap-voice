"""The Twilio credential n8n uses to place calls (``make n8n-import``).

    python -m caregap.telephony.n8n_credential | n8n import:credentials ...

The core service hands n8n a create-call request without any auth, and the workflow's
"Place call (Twilio)" node signs it with this credential. n8n encrypts credentials at
rest and leaves them out of execution data, so the auth token never shows up in n8n's
execution history. The values come from the same settings the core service uses (the
mock's account by default, the real one in live mode), so re-importing after a change in
``.env`` keeps both sides in step.
"""

from __future__ import annotations

import json
from typing import Any

from caregap.config import Settings, get_settings

#: Referenced by the workflow node (n8n/workflows/campaign-tick.json); keep them in sync.
CREDENTIAL_ID = "cgTwilioAuth0001"
CREDENTIAL_NAME = "CareGap: Twilio account"


def twilio_credential(settings: Settings) -> list[dict[str, Any]]:
    """The ``n8n import:credentials`` payload: one HTTP Basic auth credential."""
    return [
        {
            "id": CREDENTIAL_ID,
            "name": CREDENTIAL_NAME,
            "type": "httpBasicAuth",
            "data": {
                "user": settings.twilio_account_sid,
                "password": settings.twilio_auth_token.get_secret_value(),
            },
        }
    ]


def main() -> None:
    print(json.dumps(twilio_credential(get_settings())))


if __name__ == "__main__":
    main()
