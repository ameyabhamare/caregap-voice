"""What an agent may see and do for one member: the core side of the MCP server (Phase 9).

The MCP server (``caregap.mcp``) only calls the HTTP API; the rules live here:
- a member is shown only with the fields their plan's ``allowed_member_fields`` permits
  (the same minimum-necessary list that limits prompts) and only their plan's measures;
- interactions and escalations need a member that belongs to a configured customer;
- an agent's own words go in ``note``/``summary``, never in ``escalations.detail``, so
  notifications carry IDs only;
- asking for a human twice with the same reason while the first request is open returns
  the first request (tool calls get retried).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal, get_args

import psycopg
from psycopg.rows import dict_row

from caregap.campaign.clock import Clock
from caregap.customers import CustomerConfig, customer_for
from caregap.outreach.compliance import MemberContext
from caregap.outreach.store import Gap, NotFoundError, OutreachStore

Channel = Literal["phone", "sms", "email", "chat", "other"]
Outcome = Literal["reached", "left_message", "no_answer", "resolved", "follow_up", "other"]
CHANNELS: tuple[str, ...] = get_args(Channel)
OUTCOMES: tuple[str, ...] = get_args(Outcome)

log = logging.getLogger(__name__)


class NoCustomerError(Exception):
    """The member belongs to no configured customer in that measurement year."""


@dataclass(frozen=True)
class MemberView:
    member_id: str
    customer: str
    plan_name: str
    #: The language this plan would contact the member in.
    language: str
    #: Only the fields in the plan's ``allowed_member_fields``.
    fields: dict[str, str | int | date | None]
    #: The plan's measures, in its priority order. status ``gap`` means open.
    gaps: list[Gap]


class MemberService:
    def __init__(
        self, conninfo: str, store: OutreachStore, customers: Mapping[str, CustomerConfig],
        clock: Clock,
    ) -> None:  # fmt: skip
        self.conninfo = conninfo
        self.store = store
        self.customers = dict(customers)
        self.clock = clock

    def _customer(self, member: MemberContext, year: int) -> CustomerConfig:
        c = customer_for(self.customers, member.birth_date, year)
        if c is None:
            raise NoCustomerError(f"member {member.member_id} has no configured customer in {year}")
        return c

    def view(self, member_id: str, year: int) -> MemberView:
        member = self.store.member(member_id, today=self.clock.now().date())
        c = self._customer(member, year)
        by_measure = {g.measure: g for g in self.store.gaps(member_id, year)}
        return MemberView(
            member_id=member.member_id,
            customer=c.id,
            plan_name=c.plan_name,
            language=c.language_for(member.language),
            fields={f: getattr(member, f) for f in c.allowed_member_fields},
            gaps=[by_measure[m] for m in c.measures if m in by_measure],
        )

    def log_interaction(
        self, member_id: str, *, channel: str, outcome: str, summary: str, source: str,
        year: int,
    ) -> int:  # fmt: skip
        if channel not in CHANNELS or outcome not in OUTCOMES:
            raise ValueError(f"channel must be one of {CHANNELS}, outcome one of {OUTCOMES}")
        c = self._customer(self.store.member(member_id), year)
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn:
            row = conn.execute(
                "INSERT INTO member_interactions (member_id, customer, channel, outcome, summary,"
                " source, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                (member_id, c.id, channel, outcome, summary, source, self.clock.now()),
            ).fetchone()
        assert row is not None  # noqa: S101 - INSERT ... RETURNING
        return int(row["id"])

    def escalate(
        self, member_id: str, *, reason: str, source: str, year: int,
        campaign_id: int | None = None,
    ) -> tuple[int, bool]:  # fmt: skip
        """Ask for a human. Returns (escalation ID, whether it was created now)."""
        self._customer(self.store.member(member_id), year)
        detail = f"agent requested a human (via {source})"
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn, conn.transaction():
            if (
                campaign_id is not None
                and not conn.execute(
                    "SELECT 1 FROM campaigns WHERE id = %s", (campaign_id,)
                ).fetchone()
            ):
                raise NotFoundError(f"campaign {campaign_id} not found")
            row = conn.execute(
                "INSERT INTO escalations (kind, campaign_id, member_id, detail, note,"
                " created_at) VALUES ('agent_request', %s, %s, %s, %s, %s)"
                " ON CONFLICT (member_id, md5(note))"
                " WHERE kind = 'agent_request' AND resolved_at IS NULL DO NOTHING"
                " RETURNING id",
                (campaign_id, member_id, detail, reason, self.clock.now()),
            ).fetchone()
            if row is not None:
                log.warning("escalation %s (agent_request) for member %s", row["id"], member_id)
                return int(row["id"]), True
            existing: dict[str, Any] | None = conn.execute(
                "SELECT id FROM escalations WHERE kind = 'agent_request' AND member_id = %s"
                " AND md5(note) = md5(%s) AND resolved_at IS NULL",
                (member_id, reason),
            ).fetchone()
        assert existing is not None  # noqa: S101 - the conflicting row
        return int(existing["id"]), False
