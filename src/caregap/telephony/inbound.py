"""Inbound callback calls (Phase 11): a member calls the plan's number with a question.

Twilio posts to the same n8n webhook as outbound calls (``event=inbound``), and n8n
relays it here like every other Twilio request. The number's webhook in Twilio is
``<webhook>?event=inbound&customer=<id>``; the signature covers that URL, so the customer
is trusted only after the signature check, and every URL is rebuilt from our settings.

    greeting (speech or one key) -> question -> grounded answer (caregap.rag) -> "another
    question?" ... up to MAX_TURNS; the callback digit hands the caller to a person.

Answers are spoken only after the callback agent's grounding check, by Twilio's own
voice (<Say>). Speech is recognised by Twilio in the plan's default language.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

import psycopg
from psycopg.rows import dict_row

from caregap import audit
from caregap.customers import CustomerConfig
from caregap.outreach.store import NotFoundError
from caregap.rag.service import CallbackService
from caregap.telephony import twilio

log = logging.getLogger(__name__)

#: Questions answered on one call before it ends politely.
MAX_TURNS = 3


class InboundCalls:
    def __init__(
        self, conninfo: str, *, callback_url: str, auth_token: str,
        customers: Mapping[str, CustomerConfig], callbacks: Callable[[], CallbackService],
    ) -> None:  # fmt: skip
        self.conninfo = conninfo
        self.callback_url = callback_url
        self.auth_token = auth_token
        self.customers = dict(customers)
        self.callbacks = callbacks

    def url(self, customer: str, event: str, turn: int | None = None) -> str:
        sep = "&" if "?" in self.callback_url else "?"
        url = f"{self.callback_url}{sep}event={event}&customer={customer}"
        return url if turn is None else f"{url}&turn={turn}"

    def handle(self, query: Mapping[str, str], body: Mapping[str, str], sig: str) -> str:
        event, customer = query.get("event", ""), query.get("customer", "")
        if customer not in self.customers or event not in ("inbound", "inbound-speech"):
            raise NotFoundError("unknown inbound call webhook")
        turn = None
        if event == "inbound-speech":
            turn = int(query["turn"]) if query.get("turn", "").isdigit() else 0
        if not twilio.valid_signature(self.auth_token, self.url(customer, event, turn), body,
                                      sig):  # fmt: skip
            raise twilio.InvalidSignatureError("invalid X-Twilio-Signature")
        cfg = self.customers[customer]
        lang = cfg.languages[cfg.default_language]
        if event == "inbound":
            return twilio.ask_by_voice(
                lang.qa_greeting, self.url(customer, "inbound-speech", 1),
                closing=lang.goodbye, language=lang.say_language,
            )  # fmt: skip
        assert turn is not None  # noqa: S101 - set above
        member_id = self._member(body.get("From", ""))
        if body.get("Digits") == cfg.escalation.callback_digit:
            self._escalate(customer, member_id, body.get("CallSid", ""))
            return twilio.say_and_hang_up(lang.callback_confirmation, language=lang.say_language)
        question = body.get("SpeechResult", "").strip()
        if not question:
            return twilio.say_and_hang_up(lang.goodbye, language=lang.say_language)
        _, answer, _ = self.callbacks().ask(customer, question, member_id)
        said = cfg.languages[answer.language].say_language
        if answer.outcome == "handoff" or turn >= MAX_TURNS:
            # The service already escalated a hand-off; say the answer and end politely.
            return twilio.say_and_hang_up(f"{answer.text} {lang.goodbye}", language=said)
        return twilio.ask_by_voice(
            lang.qa_more, self.url(customer, "inbound-speech", turn + 1),
            closing=lang.goodbye, language=lang.say_language, before=answer.text,
        )  # fmt: skip

    def _member(self, caller: str) -> str | None:
        """The member whose phone number called, if exactly one matches."""
        digits = re.sub(r"\D", "", caller)[-10:]
        if len(digits) != 10:
            return None
        with psycopg.connect(self.conninfo) as conn:
            rows = conn.execute(
                "SELECT member_id FROM members"
                " WHERE right(regexp_replace(phone, '\\D', '', 'g'), 10) = %s LIMIT 2",
                (digits,),
            ).fetchall()
        return str(rows[0][0]) if len(rows) == 1 else None

    def _escalate(self, customer: str, member_id: str | None, call_sid: str) -> int:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            row: dict[str, Any] | None = conn.execute(
                "INSERT INTO escalations (kind, member_id, detail) VALUES ('member_request', %s,"
                " %s) RETURNING id",
                (member_id, f"{customer} caller asked for a person on an inbound call"),
            ).fetchone()
            assert row is not None  # noqa: S101 - INSERT ... RETURNING
            audit.record(
                conn, "escalation_created", actor="twilio", customer=customer,
                member_id=member_id, escalation_id=row["id"],
                payload={"kind": "member_request", "inbound": True, "call_sid": call_sid},
            )  # fmt: skip
        log.warning("escalation %s: inbound %s caller asked for a person", row["id"], customer)
        return int(row["id"])
