"""Campaign service: every outreach decision, behind a small API that n8n calls.

Per member, a campaign moves through:

    selected -> preparing -> ready ---------------------------> calling -> completed
                         \\-> awaiting_review -> ready             |  \\-> opted_out (DTMF 9)
                         \\-> skipped                              |  \\-> escalated (DTMF 0)
                                                                   v
                                     retry_wait <- (no-answer, busy, failed) -> failed

- Selection skips members who are deceased, have no voice consent or no usable phone.
- The first ``approval_first_n`` scripts of a campaign, and every script that failed
  compliance, wait in the review queue for a human.
- Calls are placed only inside the call window and only with consent granted at call time.
- Unsuccessful calls are retried after ``retry_delay_minutes`` (moved into the next call
  window) until ``max_call_attempts``; then the member fails and a person is alerted.
- Work stuck in ``preparing`` or ``calling`` (a crashed worker, a lost callback) is
  recovered by ``work()``.

State changes are conditional updates under row locks, so concurrent n8n executions can
ask for the same work and only one does it. Alerts and escalations go to the
``escalations`` table, which doubles as an outbox that n8n delivers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from caregap.campaign.clock import Clock
from caregap.campaign.window import CallWindow
from caregap.outreach.generate import COMPLIANT, ScriptGenerator
from caregap.outreach.profile import OutreachProfile
from caregap.outreach.store import NotFoundError, OutreachStore
from caregap.telephony import twilio
from caregap.voice.render import AudioRenderer, latest_audio

log = logging.getLogger(__name__)

#: Work in these states is reclaimed after this long (crashed worker, lost callback).
STALE_PREPARING = timedelta(minutes=15)
STALE_CALLING = timedelta(minutes=30)

ACTIVE_STATES = ("selected", "preparing", "ready", "calling", "retry_wait")
CALL_DONE = ("completed", "no-answer", "busy", "failed", "canceled")


class InvalidSignatureError(PermissionError):
    pass


class EventConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class TelephonyConfig:
    public_base_url: str
    callback_url: str
    api_base_url: str
    account_sid: str
    auth_token: str
    from_number: str


class CampaignService:
    def __init__(
        self,
        conninfo: str,
        *,
        outreach: OutreachStore,
        generator: ScriptGenerator,
        renderer: Callable[[], AudioRenderer],
        profile: OutreachProfile,
        telephony: TelephonyConfig,
        clock: Clock,
    ) -> None:
        self.conninfo = conninfo
        self.outreach = outreach
        self.generator = generator
        self.renderer = renderer
        self.policy = profile.campaign
        self.window = CallWindow(profile.campaign)
        self.tel = telephony
        self.clock = clock

    @contextmanager
    def _tx(self) -> Iterator[psycopg.Connection[dict[str, Any]]]:
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn, conn.transaction():
            yield conn

    # --- campaigns -----------------------------------------------------------------

    def create(
        self,
        *,
        name: str,
        size: int,
        measurement_year: int,
        retry_delay_minutes: int | None = None,
    ) -> dict[str, Any]:
        """Select up to ``size`` members with an open gap who are not already in a
        campaign for the year (unless it skipped or failed them), in a stable pseudo-random
        order, and record why any of them cannot be called."""
        p = self.policy
        now = self.clock.now()
        with self._tx() as conn:
            campaign = conn.execute(
                "INSERT INTO campaigns (name, measurement_year, approval_first_n,"
                " max_call_attempts, retry_delay_minutes, created_at)"
                " VALUES (%s, %s, %s, %s, %s, %s) RETURNING *",
                (name, measurement_year, p.approval_first_n, p.max_call_attempts,
                 p.retry_delay_minutes if retry_delay_minutes is None else retry_delay_minutes,
                 now),
            ).fetchone()  # fmt: skip
            assert campaign is not None  # noqa: S101 - INSERT ... RETURNING
            candidates = conn.execute(
                """
                SELECT m.member_id, m.phone, m.deceased_date, c.status AS consent
                FROM members m
                LEFT JOIN consent c ON c.member_id = m.member_id AND c.channel = 'voice'
                WHERE EXISTS (SELECT 1 FROM care_gaps g WHERE g.member_id = m.member_id
                              AND g.measurement_year = %(year)s AND g.status = 'gap')
                  AND NOT EXISTS (
                      SELECT 1 FROM campaign_members cm JOIN campaigns k ON k.id = cm.campaign_id
                      WHERE cm.member_id = m.member_id AND k.measurement_year = %(year)s
                        AND cm.state NOT IN ('skipped', 'failed'))
                ORDER BY md5(m.member_id), m.member_id
                LIMIT %(size)s
                """,
                {"year": measurement_year, "size": size},
            ).fetchall()
            for c in candidates:
                reason = skip_reason(c["deceased_date"], c["consent"], c["phone"])
                conn.execute(
                    "INSERT INTO campaign_members (campaign_id, member_id, state, reason,"
                    " state_since) VALUES (%s, %s, %s, %s, %s)",
                    (campaign["id"], c["member_id"], "skipped" if reason else "selected",
                     reason, now),
                )  # fmt: skip
        return self.summary(campaign["id"])

    def summary(self, campaign_id: int) -> dict[str, Any]:
        with self._tx() as conn:
            campaign = conn.execute(
                "SELECT * FROM campaigns WHERE id = %s", (campaign_id,)
            ).fetchone()
            if campaign is None:
                raise NotFoundError(f"campaign {campaign_id} not found")

            def counts(sql: str) -> dict[str, int]:
                return {r["k"]: r["n"] for r in conn.execute(sql, (campaign_id,))}

            states = counts(
                "SELECT state AS k, count(*) AS n FROM campaign_members"
                " WHERE campaign_id = %s GROUP BY 1 ORDER BY 1"
            )
            skipped = counts(
                "SELECT reason AS k, count(*) AS n FROM campaign_members"
                " WHERE campaign_id = %s AND state = 'skipped' GROUP BY 1 ORDER BY 1"
            )
            calls = counts(
                "SELECT status AS k, count(*) AS n FROM call_attempts"
                " WHERE campaign_id = %s GROUP BY 1 ORDER BY 1"
            )
            escalations = counts(
                "SELECT kind AS k, count(*) AS n FROM escalations"
                " WHERE campaign_id = %s GROUP BY 1 ORDER BY 1"
            )
            gated = conn.execute(
                "SELECT count(*) AS n FROM campaign_members WHERE campaign_id = %s AND human_gate",
                (campaign_id,),
            ).fetchone()
        return {
            "id": campaign["id"],
            "name": campaign["name"],
            "measurement_year": campaign["measurement_year"],
            "approval_first_n": campaign["approval_first_n"],
            "max_call_attempts": campaign["max_call_attempts"],
            "retry_delay_minutes": campaign["retry_delay_minutes"],
            "members": sum(states.values()),
            "states": states,
            "skipped": skipped,
            "calls": calls,
            "escalations": escalations,
            "human_reviewed": gated["n"] if gated else 0,
            # Nothing left for the automation to do; awaiting_review waits on people.
            "done": not any(states.get(s) for s in ACTIVE_STATES),
        }

    # --- work for the orchestrator -------------------------------------------------

    def work(self, *, prepare_limit: int = 4, send_limit: int = 20) -> list[dict[str, Any]]:
        """What can be done now: ``prepare`` (generate a script) and ``send`` (place a
        call) items. Recovers stuck work and applies review decisions first. Read-only
        apart from that; the action endpoints claim items atomically."""
        now = self.clock.now()
        self._apply_reviews(now)
        self._recover_stale_calls(now)
        with self._tx() as conn:
            items = conn.execute(
                "SELECT campaign_id, member_id, 'prepare' AS action FROM campaign_members"
                " WHERE state = 'selected' OR (state = 'preparing' AND state_since < %s)"
                " ORDER BY campaign_id, member_id LIMIT %s",
                (now - STALE_PREPARING, prepare_limit),
            ).fetchall()
            if self.window.is_open(now):
                items += conn.execute(
                    "SELECT campaign_id, member_id, 'send' AS action FROM campaign_members"
                    " WHERE state IN ('ready', 'retry_wait') AND next_call_at <= %s"
                    " ORDER BY next_call_at, campaign_id, member_id LIMIT %s",
                    (now, send_limit),
                ).fetchall()
        return items

    def _apply_reviews(self, now: datetime) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE campaign_members cm
                SET state = CASE s.status WHEN 'approved' THEN 'ready' ELSE 'skipped' END,
                    reason = CASE s.status WHEN 'approved' THEN NULL ELSE 'script_rejected' END,
                    next_call_at = %(now)s, state_since = %(now)s
                FROM outreach_scripts s
                WHERE s.id = cm.script_id AND cm.state = 'awaiting_review'
                  AND s.status IN ('approved', 'rejected')
                """,
                {"now": now},
            )

    def _recover_stale_calls(self, now: datetime) -> None:
        with self._tx() as conn:
            stale = conn.execute(
                "SELECT campaign_id, member_id FROM campaign_members"
                " WHERE state = 'calling' AND state_since < %s FOR UPDATE SKIP LOCKED",
                (now - STALE_CALLING,),
            ).fetchall()
            for s in stale:
                conn.execute(
                    "UPDATE call_attempts SET status = 'failed', updated_at = %s,"
                    " error = 'no final status callback'"
                    " WHERE campaign_id = %s AND member_id = %s"
                    " AND status IN ('queued', 'dispatched')",
                    (now, s["campaign_id"], s["member_id"]),
                )
                self._after_unsuccessful_call(
                    conn, s["campaign_id"], s["member_id"], now, "no final status callback"
                )

    # --- prepare ---------------------------------------------------------------------

    def prepare(self, campaign_id: int, member_id: str) -> dict[str, Any]:
        """Generate the member's script. Compliant scripts beyond the first N are ready
        to call; the rest wait for a human."""
        now = self.clock.now()
        with self._tx() as conn:
            claimed = conn.execute(
                "UPDATE campaign_members SET state = 'preparing', state_since = %s"
                " WHERE campaign_id = %s AND member_id = %s"
                " AND (state = 'selected' OR (state = 'preparing' AND state_since < %s))"
                " RETURNING member_id",
                (now, campaign_id, member_id, now - STALE_PREPARING),
            ).fetchone()
            campaign = conn.execute(
                "SELECT * FROM campaigns WHERE id = %s", (campaign_id,)
            ).fetchone()
        if campaign is None:
            raise NotFoundError(f"campaign {campaign_id} not found")
        if claimed is None:
            return self._no_action(campaign_id, member_id, "not waiting to be prepared")
        year = campaign["measurement_year"]
        try:
            open_measures = [
                g.measure for g in self.outreach.gaps(member_id, year) if g.status == "gap"
            ]
            if not open_measures:
                self._set_state(campaign_id, member_id, "skipped", reason="no_open_gap")
                return {"action": "skipped", "reason": "no_open_gap"}
            outcome = self.generator.generate(self.outreach.member(member_id), open_measures)
        except Exception:
            # Give the work back; the next tick retries it.
            self._set_state(campaign_id, member_id, "selected", only_from="preparing")
            raise
        with self._tx() as conn:
            # Serialize the first-N count per campaign.
            conn.execute("SELECT id FROM campaigns WHERE id = %s FOR UPDATE", (campaign_id,))
            gated = conn.execute(
                "SELECT count(*) AS n FROM campaign_members WHERE campaign_id = %s AND human_gate",
                (campaign_id,),
            ).fetchone()
            hold = gated is not None and gated["n"] < campaign["approval_first_n"]
            script_id = self.outreach.save_script(outcome, year, hold=hold)
            needs_human = hold or outcome.status != COMPLIANT
            conn.execute(
                "UPDATE campaign_members SET state = %s, script_id = %s, human_gate = %s,"
                " next_call_at = %s, state_since = %s"
                " WHERE campaign_id = %s AND member_id = %s",
                ("awaiting_review" if needs_human else "ready", script_id, needs_human,
                 None if needs_human else now, now, campaign_id, member_id),
            )  # fmt: skip
        state = "awaiting_review" if needs_human else "ready"
        log.info("campaign %s member %s: script %s, %s", campaign_id, member_id, script_id, state)
        return {"action": "prepared", "state": state, "script_id": script_id}

    # --- send ------------------------------------------------------------------------

    def send(self, campaign_id: int, member_id: str) -> dict[str, Any]:
        """Claim a due call: check window and consent, render audio if needed, record the
        attempt and return the Twilio request for the orchestrator to post."""
        now = self.clock.now()
        if not self.window.is_open(now):
            return self._no_action(campaign_id, member_id, "outside the call window")
        with self._tx() as conn:
            cm = conn.execute(
                """
                SELECT cm.*, m.phone, c.status AS consent
                FROM campaign_members cm
                JOIN members m ON m.member_id = cm.member_id
                LEFT JOIN consent c ON c.member_id = cm.member_id AND c.channel = 'voice'
                WHERE cm.campaign_id = %s AND cm.member_id = %s
                FOR UPDATE OF cm
                """,
                (campaign_id, member_id),
            ).fetchone()
            if cm is None:
                raise NotFoundError(f"member {member_id} is not in campaign {campaign_id}")
            if cm["state"] not in ("ready", "retry_wait") or cm["next_call_at"] > now:
                return self._no_action(campaign_id, member_id, "no call due", cm["state"])
            reason = skip_reason(None, cm["consent"], cm["phone"])
            if reason:
                self._update(conn, campaign_id, member_id, now, state="skipped", reason=reason)
                return {"action": "skipped", "reason": reason}
            to = twilio.to_e164(cm["phone"])
            assert to is not None  # noqa: S101 - skip_reason checked it
            audio = latest_audio(self.conninfo, cm["script_id"])
            if audio is None:
                audio = self.renderer().render(self.outreach.script(cm["script_id"]))
            attempt = conn.execute(
                "INSERT INTO call_attempts (campaign_id, member_id, attempt_no, script_id,"
                " audio_id, status, created_at, updated_at)"
                " VALUES (%s, %s, %s, %s, %s, 'queued', %s, %s) RETURNING id",
                (campaign_id, member_id, cm["calls_made"] + 1, cm["script_id"], audio.id,
                 now, now),
            ).fetchone()  # fmt: skip
            assert attempt is not None  # noqa: S101 - INSERT ... RETURNING
            conn.execute(
                "UPDATE campaign_members SET state = 'calling', calls_made = calls_made + 1,"
                " next_call_at = NULL, state_since = %s"
                " WHERE campaign_id = %s AND member_id = %s",
                (now, campaign_id, member_id),
            )
        req = twilio.call_request(
            api_base_url=self.tel.api_base_url,
            account_sid=self.tel.account_sid,
            auth_token=self.tel.auth_token,
            to=to,
            from_=self.tel.from_number,
            twiml_url=self.twiml_url(attempt["id"]),
            status_callback=self.callback_url(attempt["id"], "status"),
        )
        return {
            "action": "call",
            "attempt_id": attempt["id"],
            "twilio": {"url": req.url, "authorization": req.authorization, "form": req.form},
        }

    def dispatched(self, attempt_id: int, call_sid: str) -> None:
        with self._tx() as conn:
            row = conn.execute(
                "UPDATE call_attempts SET call_sid = COALESCE(call_sid, %s),"
                " status = CASE status WHEN 'queued' THEN 'dispatched' ELSE status END,"
                " updated_at = %s WHERE id = %s RETURNING call_sid",
                (call_sid, self.clock.now(), attempt_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"call attempt {attempt_id} not found")
        if row["call_sid"] != call_sid:
            raise EventConflictError(f"call attempt {attempt_id} has call {row['call_sid']}")

    def dispatch_failed(self, attempt_id: int, error: str) -> None:
        """The create-call request failed after the orchestrator's own retries."""
        now = self.clock.now()
        with self._tx() as conn:
            attempt = self._lock_attempt(conn, attempt_id)
            if attempt["status"] not in ("queued", "dispatched"):
                return
            conn.execute(
                "UPDATE call_attempts SET status = 'failed', error = %s, updated_at = %s"
                " WHERE id = %s",
                (error[:1000], now, attempt_id),
            )
            self._after_unsuccessful_call(
                conn, attempt["campaign_id"], attempt["member_id"], now, "call request failed"
            )

    # --- telephony callbacks -----------------------------------------------------------

    def twiml_url(self, attempt_id: int) -> str:
        return f"{self.tel.public_base_url.rstrip('/')}/twiml/attempts/{attempt_id}"

    def callback_url(self, attempt_id: int, event: str) -> str:
        sep = "&" if "?" in self.tel.callback_url else "?"
        url = f"{self.tel.callback_url}{sep}attempt_id={attempt_id}"
        return url + "&event=gather" if event == "gather" else url

    def _check_signature(self, url: str, params: Mapping[str, str], sig: str) -> None:
        if not twilio.valid_signature(self.tel.auth_token, url, params, sig):
            raise InvalidSignatureError("invalid X-Twilio-Signature")

    def twiml(self, attempt_id: int, params: Mapping[str, str], sig: str) -> str:
        """TwiML for an answered call: play the rendered message, listen for one digit."""
        self._check_signature(self.twiml_url(attempt_id), params, sig)
        with self._tx() as conn:
            attempt = conn.execute(
                "SELECT script_id FROM call_attempts WHERE id = %s", (attempt_id,)
            ).fetchone()
        if attempt is None:
            raise NotFoundError(f"call attempt {attempt_id} not found")
        audio_url = f"{self.tel.public_base_url.rstrip('/')}/scripts/{attempt['script_id']}/audio"
        return twilio.play_and_gather(
            audio_url, self.callback_url(attempt_id, "gather"), closing=self.policy.goodbye
        )

    def telephony_event(
        self, query: Mapping[str, str], body: Mapping[str, str], sig: str
    ) -> str | None:
        """A Twilio callback relayed by the orchestrator. Returns TwiML for gather events.

        The signature is checked against the URL we gave Twilio, rebuilt from our own
        settings, so a relay cannot change what was signed."""
        try:
            attempt_id = int(query["attempt_id"])
        except (KeyError, ValueError) as exc:
            raise NotFoundError("callback without a valid attempt_id") from exc
        event = "gather" if query.get("event") == "gather" else "status"
        self._check_signature(self.callback_url(attempt_id, event), body, sig)
        now = self.clock.now()
        with self._tx() as conn:
            attempt = self._lock_attempt(conn, attempt_id)
            sid = body.get("CallSid")
            if attempt["call_sid"] and sid != attempt["call_sid"]:
                raise EventConflictError(f"CallSid {sid} does not match attempt {attempt_id}")
            conn.execute(
                "INSERT INTO telephony_events (call_attempt_id, kind, payload, received_at)"
                " VALUES (%s, %s, %s, %s)",
                (attempt_id, event, Jsonb(dict(body)), now),
            )
            conn.execute(
                "UPDATE call_attempts SET call_sid = COALESCE(call_sid, %s) WHERE id = %s",
                (sid, attempt_id),
            )
            if event == "gather":
                return self._on_digits(conn, attempt, body.get("Digits", ""), now)
            self._on_status(conn, attempt, body, now)
        return None

    def _on_digits(
        self, conn: psycopg.Connection[dict[str, Any]], attempt: dict[str, Any], digits: str,
        now: datetime,
    ) -> str:  # fmt: skip
        cid, mid = attempt["campaign_id"], attempt["member_id"]
        conn.execute(
            "UPDATE call_attempts SET digits = %s, updated_at = %s WHERE id = %s",
            (digits, now, attempt["id"]),
        )
        if digits == "9":
            conn.execute(
                """
                INSERT INTO consent (member_id, channel, status, source, updated_at)
                VALUES (%s, 'voice', 'revoked', 'dtmf', %s)
                ON CONFLICT (member_id, channel)
                DO UPDATE SET status = 'revoked', source = 'dtmf', updated_at = EXCLUDED.updated_at
                """,
                (mid, now),
            )
            self._update(conn, cid, mid, now, state="opted_out", reason="dtmf_9")
            return twilio.say_and_hang_up(self.policy.opt_out_confirmation)
        if digits == "0":
            self._update(conn, cid, mid, now, state="escalated", reason="dtmf_0")
            self._escalate(
                conn, "member_request", cid, mid, attempt["id"], now,
                "member pressed 0 to ask for a call back",
            )  # fmt: skip
            return twilio.say_and_hang_up(self.policy.callback_confirmation)
        return twilio.say_and_hang_up(self.policy.goodbye)

    def _on_status(
        self, conn: psycopg.Connection[dict[str, Any]], attempt: dict[str, Any],
        body: Mapping[str, str], now: datetime,
    ) -> None:  # fmt: skip
        status = body.get("CallStatus", "")
        if status not in twilio.TERMINAL_STATUSES or attempt["status"] in CALL_DONE:
            return  # progress events, or a duplicate final callback
        duration = body.get("CallDuration")
        conn.execute(
            "UPDATE call_attempts SET status = %s, duration_s = %s, updated_at = %s"
            " WHERE id = %s",
            (status, int(duration) if duration and duration.isdigit() else None, now,
             attempt["id"]),
        )  # fmt: skip
        cid, mid = attempt["campaign_id"], attempt["member_id"]
        if status == "completed":
            # A DTMF choice made during the call has already set the final state.
            conn.execute(
                "UPDATE campaign_members SET state = 'completed', state_since = %s"
                " WHERE campaign_id = %s AND member_id = %s AND state = 'calling'",
                (now, cid, mid),
            )
        else:
            self._after_unsuccessful_call(conn, cid, mid, now, status)

    def _after_unsuccessful_call(
        self, conn: psycopg.Connection[dict[str, Any]], campaign_id: int, member_id: str,
        now: datetime, why: str,
    ) -> None:  # fmt: skip
        cm = conn.execute(
            "SELECT cm.calls_made, k.max_call_attempts, k.retry_delay_minutes"
            " FROM campaign_members cm JOIN campaigns k ON k.id = cm.campaign_id"
            " WHERE cm.campaign_id = %s AND cm.member_id = %s AND cm.state = 'calling'",
            (campaign_id, member_id),
        ).fetchone()
        if cm is None:
            return  # already final
        if cm["calls_made"] < cm["max_call_attempts"]:
            at = self.window.next_open(now + timedelta(minutes=cm["retry_delay_minutes"]))
            conn.execute(
                "UPDATE campaign_members SET state = 'retry_wait', next_call_at = %s,"
                " state_since = %s WHERE campaign_id = %s AND member_id = %s",
                (at, now, campaign_id, member_id),
            )
            return
        reason = f"{cm['calls_made']} calls, last: {why}"
        self._update(conn, campaign_id, member_id, now, state="failed", reason=reason)
        self._escalate(
            conn, "delivery_failed", campaign_id, member_id, None, now, f"not reached: {reason}"
        )

    # --- alerts and the notification outbox -------------------------------------------

    def alert(self, *, workflow: str, node: str | None, message: str, execution: str | None) -> int:
        """A workflow failed in the orchestrator."""
        detail = f"workflow {workflow!r}" + (f" node {node!r}" if node else "")
        detail += f": {message}" + (f" (execution {execution})" if execution else "")
        with self._tx() as conn:
            return self._escalate(
                conn, "workflow_error", None, None, None, self.clock.now(), detail[:2000]
            )

    def outbox(self, limit: int = 50) -> list[dict[str, Any]]:
        """Escalations not yet delivered, as Slack-compatible ``{"text": ...}`` bodies.
        Internal IDs only: no names, phone numbers or health details."""
        with self._tx() as conn:
            rows = conn.execute(
                "SELECT * FROM escalations WHERE notified_at IS NULL ORDER BY id LIMIT %s",
                (limit,),
            ).fetchall()
        return [{"id": r["id"], "text": notification_text(r)} for r in rows]

    def mark_notified(self, escalation_id: int) -> None:
        with self._tx() as conn:
            row = conn.execute(
                "UPDATE escalations SET notified_at = COALESCE(notified_at, %s)"
                " WHERE id = %s RETURNING id",
                (self.clock.now(), escalation_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"escalation {escalation_id} not found")

    def escalations(self, *, open_only: bool = True) -> list[dict[str, Any]]:
        with self._tx() as conn:
            return conn.execute(
                "SELECT * FROM escalations WHERE resolved_at IS NULL OR NOT %s ORDER BY id",
                (open_only,),
            ).fetchall()

    # --- helpers -------------------------------------------------------------------------

    def _escalate(
        self, conn: psycopg.Connection[dict[str, Any]], kind: str, campaign_id: int | None,
        member_id: str | None, attempt_id: int | None, now: datetime, detail: str,
    ) -> int:  # fmt: skip
        row = conn.execute(
            "INSERT INTO escalations (kind, campaign_id, member_id, call_attempt_id, detail,"
            " created_at) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (kind, campaign_id, member_id, attempt_id, detail, now),
        ).fetchone()
        assert row is not None  # noqa: S101 - INSERT ... RETURNING
        log.warning("escalation %s (%s): %s", row["id"], kind, detail)
        return int(row["id"])

    def _lock_attempt(
        self, conn: psycopg.Connection[dict[str, Any]], attempt_id: int
    ) -> dict[str, Any]:
        attempt = conn.execute(
            "SELECT * FROM call_attempts WHERE id = %s FOR UPDATE", (attempt_id,)
        ).fetchone()
        if attempt is None:
            raise NotFoundError(f"call attempt {attempt_id} not found")
        return attempt

    @staticmethod
    def _update(
        conn: psycopg.Connection[dict[str, Any]], campaign_id: int, member_id: str,
        now: datetime, *, state: str, reason: str | None = None,
    ) -> None:  # fmt: skip
        conn.execute(
            "UPDATE campaign_members SET state = %s, reason = %s, next_call_at = NULL,"
            " state_since = %s WHERE campaign_id = %s AND member_id = %s",
            (state, reason, now, campaign_id, member_id),
        )

    def _set_state(
        self, campaign_id: int, member_id: str, state: str, *, reason: str | None = None,
        only_from: str | None = None,
    ) -> None:  # fmt: skip
        with self._tx() as conn:
            conn.execute(
                "UPDATE campaign_members SET state = %s, reason = %s, state_since = %s"
                " WHERE campaign_id = %s AND member_id = %s"
                " AND (%s::text IS NULL OR state = %s)",
                (state, reason, self.clock.now(), campaign_id, member_id, only_from, only_from),
            )

    def _no_action(
        self, campaign_id: int, member_id: str, why: str, state: str | None = None
    ) -> dict[str, Any]:
        if state is None:
            with self._tx() as conn:
                row = conn.execute(
                    "SELECT state FROM campaign_members WHERE campaign_id = %s AND member_id = %s",
                    (campaign_id, member_id),
                ).fetchone()
            if row is None:
                raise NotFoundError(f"member {member_id} is not in campaign {campaign_id}")
            state = row["state"]
        return {"action": "none", "reason": why, "state": state}


def skip_reason(deceased: object, consent: str | None, phone: str | None) -> str | None:
    if deceased is not None:
        return "deceased"
    if consent is None:
        return "no_consent"
    if consent != "granted":
        return "consent_revoked"
    if twilio.to_e164(phone) is None:
        return "invalid_phone"
    return None


def notification_text(e: Mapping[str, Any]) -> str:
    where = " ".join(
        f"{k} {e[k]}" for k in ("campaign_id", "member_id", "call_attempt_id") if e.get(k)
    )
    head = f"[caregap] {e['kind']} #{e['id']}" + (f" ({where})" if where else "")
    return f"{head}: {e['detail']}"
