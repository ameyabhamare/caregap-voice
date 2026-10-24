"""Agent tools (Phase 9): the core endpoints behind the MCP server."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from caregap.api.app import create_app
from caregap.campaign.clock import FixedClock
from caregap.config import Settings
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.customers import CustomerConfig, load_all
from caregap.llm.base import LLMClient
from caregap.outreach.store import OutreachStore
from caregap.prompts import FilePromptStore
from caregap.rag.store import ChunkStore, ingest
from tests.integration.test_callbacks import BagOfWords
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE member_interactions, escalations, benefit_chunks CASCADE")
        conn.execute("TRUNCATE members CASCADE")
        conn.execute(
            "INSERT INTO members (member_id, given_name, family_name, birth_date, phone,"
            " language) VALUES"
            " ('m-old', 'Ava', 'Synth', '1950-01-01', '555-0142', 'es'),"
            " ('m-young', 'Bo', 'Synth', '1990-06-01', '555-0100', 'en')"
        )
        conn.execute(
            "INSERT INTO care_gaps (member_id, measure, measurement_year, status, reason) VALUES"
            " ('m-old', 'statin', 2025, 'met', 'PDC 90.0%'),"
            " ('m-old', 'awv', 2025, 'gap', 'no wellness visit in 2025'),"
            " ('m-young', 'awv', 2025, 'not_eligible', 'age 35 < 65'),"
            " ('m-young', 'eye_exam', 2025, 'gap', 'no eye exam in 2025')"
        )
    ingest(load_all(), ChunkStore(pg_url), BagOfWords(), "bag-of-words")
    return pg_url


def make_app(db: str, customers: dict[str, CustomerConfig] | None = None) -> Any:
    llm = LLMClient(FakeProvider([]), CostLedger(InMemoryLedgerStore(), Decimal("5")))
    return create_app(
        Settings(_env_file=None, database_url=db),
        store=OutreachStore(db),
        llm=llm,
        prompts=FilePromptStore(),
        customers=customers if customers is not None else load_all(),
        embeddings=BagOfWords(),
        clock=FixedClock(datetime.fromisoformat("2026-01-13T10:00:00-05:00")),
    )


def test_member_view_shows_only_allowed_fields_and_the_plans_measures(db: str) -> None:
    c = TestClient(make_app(db))
    old = c.get("/members/m-old").json()
    assert {k: old[k] for k in ("customer", "plan_name", "language", "fields")} == {
        "customer": "sunrise", "plan_name": "Sunrise Medicare Advantage", "language": "es",
        "fields": {"given_name": "Ava"},
    }  # fmt: skip
    # Sunrise priority order (awv, eye_exam, statin), only measures with a gap row.
    assert [(g["measure"], g["status"]) for g in old["gaps"]] == [("awv", "gap"), ("statin", "met")]
    young = c.get("/members/m-young").json()
    assert (young["customer"], young["fields"], young["language"]) == ("horizon", {}, "en")
    # Horizon has no wellness visit measure, so the awv row is not shown.
    assert [g["measure"] for g in young["gaps"]] == ["eye_exam"]
    assert "555-0142" not in str(old) and "Synth" not in str(old) + str(young)
    assert c.get("/members/nobody").status_code == 404
    only_sunrise = TestClient(make_app(db, {"sunrise": load_all()["sunrise"]}))
    assert only_sunrise.get("/members/m-young").status_code == 409


def test_interactions_are_logged_with_the_members_customer(db: str) -> None:
    c = TestClient(make_app(db))
    log = {"channel": "phone", "outcome": "follow_up", "summary": "Call back Friday."}
    r = c.post("/members/m-young/interactions", json=log)
    assert r.status_code == 201, r.text
    with psycopg.connect(db) as conn:
        row = conn.execute(
            "SELECT id, member_id, customer, channel, outcome, summary, source"
            " FROM member_interactions"
        ).fetchone()
    assert row == (r.json()["interaction_id"], "m-young", "horizon", "phone", "follow_up",
                   "Call back Friday.", "mcp")  # fmt: skip
    bad = {"channel": "fax", "outcome": "reached", "summary": "x"}
    assert c.post("/members/m-young/interactions", json=bad).status_code == 422
    ok = {"channel": "sms", "outcome": "reached", "summary": "x"}
    assert c.post("/members/nobody/interactions", json=ok).status_code == 404


def test_escalation_is_idempotent_while_open_and_notifies_with_ids_only(db: str) -> None:
    c = TestClient(make_app(db))
    ask = {"member_id": "m-old", "reason": "Ava asked to talk to a nurse about her statin."}
    first = c.post("/escalations", json=ask)
    again = c.post("/escalations", json=ask)
    other = c.post("/escalations", json={**ask, "reason": "Wants a ride to the clinic."})
    assert (first.status_code, again.status_code, other.status_code) == (201, 200, 201)
    eid = first.json()["escalation_id"]
    assert again.json() == {"escalation_id": eid, "created": False}
    assert other.json()["escalation_id"] != eid
    outbox = c.get("/notifications/outbox").json()["items"]
    assert [i["text"] for i in outbox if i["id"] == eid] == [
        f"[caregap] agent_request #{eid} (member_id m-old): agent requested a human (via mcp)"
    ]
    assert "nurse" not in str(outbox) and "Ava" not in str(outbox)
    with psycopg.connect(db) as conn:
        conn.execute("UPDATE escalations SET resolved_at = now() WHERE id = %s", (eid,))
    reopened = c.post("/escalations", json=ask)
    assert reopened.status_code == 201 and reopened.json()["escalation_id"] not in (eid,)
    assert c.post("/escalations", json={**ask, "campaign_id": 999999}).status_code == 404
    assert c.post("/escalations", json={**ask, "member_id": "nobody"}).status_code == 404


def test_benefit_search_returns_lines_as_written_within_one_plan(db: str) -> None:
    c = TestClient(make_app(db))
    hits = c.post("/benefits/search",
                  json={"customer": "horizon", "query": "specialist visit copay referral",
                        "k": 3}).json()  # fmt: skip
    assert len(hits) == 3 and hits[0]["section"] == "S4"
    text = ChunkStore(db).all_text("horizon")
    assert all(h["text"] in text for h in hits)
    assert c.post("/benefits/search", json={"customer": "acme", "query": "x"}).status_code == 404
