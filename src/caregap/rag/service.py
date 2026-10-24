"""Callback questions: ask the agent, log the interaction, escalate hand-offs."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from caregap import audit, observability
from caregap.outreach.store import NotFoundError
from caregap.rag.agent import Answer, CallbackAgent


class CallbackService:
    def __init__(self, conninfo: str, agent: CallbackAgent) -> None:
        self.conninfo = conninfo
        self.agent = agent

    def ask(
        self, customer: str, question: str, member_id: str | None = None
    ) -> tuple[int, Answer, int | None]:
        """Answer or hand off. Returns (question ID, answer, escalation ID or None).

        Hand-offs go to the escalation outbox with IDs only: the question itself may hold
        health details, so it stays in ``callback_questions``, not in notifications."""
        if customer not in self.agent.customers:
            raise NotFoundError(f"customer {customer!r} is not configured")
        if member_id is not None:
            with psycopg.connect(self.conninfo) as check:
                if not check.execute(
                    "SELECT 1 FROM members WHERE member_id = %s", (member_id,)
                ).fetchone():
                    raise NotFoundError(f"member {member_id} not found")
        tags = {"operation": "callback_question", "customer": customer, "member_id": member_id}
        with observability.span("callback_question", "AGENT", root=True, tags=tags) as span:
            span.set_inputs({"question": question})
            a = self.agent.ask(customer, question)
            span.set_outputs({"outcome": a.outcome, "reason": a.reason, "answer": a.text})
            trace_id = observability.trace_id()
        with psycopg.connect(self.conninfo, row_factory=dict_row) as conn, conn.transaction():
            row = conn.execute(
                """
                INSERT INTO callback_questions (customer, member_id, question, language, route,
                    outcome, reason, answer, draft, citations, retrieved, prompt_versions,
                    model, latency_ms, cost_usd, trace_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id
                """,
                (customer, member_id, question, a.language, a.route, a.outcome, a.reason,
                 a.text, a.draft, Jsonb(list(a.citations)), Jsonb(list(a.retrieved)),
                 Jsonb(dict(a.prompt_versions)), a.model, round(a.latency_s * 1000),
                 a.cost_usd, trace_id),
            ).fetchone()  # fmt: skip
            assert row is not None  # noqa: S101 - INSERT ... RETURNING
            qid = int(row["id"])
            escalation_id = None
            if a.outcome == "handoff":
                esc = conn.execute(
                    "INSERT INTO escalations (kind, member_id, callback_question_id, detail)"
                    " VALUES ('callback_question', %s, %s, %s) RETURNING id",
                    (member_id, qid,
                     f"{customer} callback question {qid} needs a person ({a.reason})"),
                ).fetchone()  # fmt: skip
                assert esc is not None  # noqa: S101 - INSERT ... RETURNING
                escalation_id = int(esc["id"])
                audit.record(
                    conn,
                    "escalation_created",
                    customer=customer,
                    member_id=member_id,
                    escalation_id=escalation_id,
                    payload={"kind": "callback_question"},
                )
            # The question and answer stay in callback_questions; the audit row has IDs,
            # the outcome and what produced it.
            audit.record(
                conn, "callback_answered", customer=customer, member_id=member_id,
                escalation_id=escalation_id, trace_id=trace_id,
                payload={"question_id": qid, "route": a.route, "outcome": a.outcome,
                         "reason": a.reason, "citations": list(a.citations),
                         "prompt_versions": dict(a.prompt_versions), "model": a.model,
                         "cost_usd": str(a.cost_usd)},
            )  # fmt: skip
        return qid, a, escalation_id
