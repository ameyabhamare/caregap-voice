"""Core service API against Postgres, with a fake model (needs `make up`)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest
from fastapi.testclient import TestClient

from caregap.api.app import create_app
from caregap.config import Settings
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.llm.base import LLMClient, ProviderError
from caregap.outreach.generate import ScriptGenerator
from caregap.outreach.profile import DEFAULT_PROFILE as P
from caregap.outreach.store import OutreachStore
from caregap.prompts import FilePromptStore
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration

GOOD = f"Hi Ava, a yearly wellness visit with your doctor is due. {P.disclaimer} {P.opt_out}"
BAD = "Hi Ava, about your diabetes."


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE outreach_scripts, care_gaps, members")
        conn.execute(
            "INSERT INTO members (member_id, given_name, family_name, birth_date, phone)"
            " VALUES ('m-gap', 'Ava', 'Welch179', '1955-04-02', '555-0142'),"
            "        ('m-none', 'Bo', 'Lind', '1990-01-01', '555-0100')"
        )
        conn.execute(
            "INSERT INTO care_gaps (member_id, measure, measurement_year, status, reason) VALUES"
            " ('m-gap', 'awv', 2025, 'gap', 'no wellness visit in 2025'),"
            " ('m-gap', 'statin', 2025, 'met', 'PDC 90.0% (300 of 333 days)'),"
            " ('m-none', 'awv', 2025, 'not_eligible', 'age 35 < 65')"
        )
    return pg_url


def _client(db: str, replies: Any) -> tuple[TestClient, FakeProvider]:
    provider = FakeProvider(replies)
    llm = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    app = create_app(
        Settings(_env_file=None, database_url=db),
        store=OutreachStore(db),
        generator=ScriptGenerator(llm, FilePromptStore(), P),
    )
    return TestClient(app), provider


def test_health_and_gaps(db: str) -> None:
    client, _ = _client(db, [])
    assert client.get("/health").json() == {
        "status": "ok",
        "provider": "ollama",
        "model": "fake-model",
    }
    gaps = client.get("/members/m-gap/gaps").json()
    assert [(g["measure"], g["status"]) for g in gaps] == [("awv", "gap"), ("statin", "met")]
    assert client.get("/members/nobody/gaps").status_code == 404


def test_compliant_script_is_stored(db: str) -> None:
    client, provider = _client(db, [GOOD])
    r = client.post("/scripts", json={"member_id": "m-gap"})
    assert r.status_code == 201
    body = r.json()
    assert (body["status"], body["measures"], body["attempts"]) == ("compliant", ["awv"], 1)
    assert body["violations"] == []
    assert client.get(f"/scripts/{body['id']}").json()["script"] == GOOD
    sent = " ".join(m.content for m in provider.calls[0])
    assert "Welch" not in sent and "555-0142" not in sent


def test_failing_script_goes_to_review_and_can_be_approved_once(db: str) -> None:
    client, _ = _client(db, [BAD, BAD])
    created = client.post("/scripts", json={"member_id": "m-gap"}).json()
    assert created["status"] == "needs_review"
    assert created["attempts"] == 2
    assert {v["rule"] for v in created["violations"]} >= {"diagnosis_term", "disclaimer"}
    assert [s["id"] for s in client.get("/reviews").json()] == [created["id"]]

    review = {"decision": "approve", "reviewer": "ana@example.org", "note": "edited offline"}
    approved = client.post(f"/reviews/{created['id']}", json=review)
    assert approved.status_code == 200
    assert (approved.json()["status"], approved.json()["reviewer"]) == (
        "approved",
        "ana@example.org",
    )
    assert client.get("/reviews").json() == []
    assert client.post(f"/reviews/{created['id']}", json=review).status_code == 409


def test_reject_and_not_found(db: str) -> None:
    client, _ = _client(db, [BAD, BAD])
    created = client.post("/scripts", json={"member_id": "m-gap"}).json()
    r = client.post(f"/reviews/{created['id']}", json={"decision": "reject", "reviewer": "x"})
    assert r.json()["status"] == "rejected"
    assert (
        client.post("/reviews/999999", json={"decision": "reject", "reviewer": "x"}).status_code
        == 404
    )


def test_member_without_open_gaps_or_unknown(db: str) -> None:
    client, provider = _client(db, [])
    assert client.post("/scripts", json={"member_id": "m-none"}).status_code == 409
    assert client.post("/scripts", json={"member_id": "nobody"}).status_code == 404
    assert provider.calls == []


def test_provider_and_budget_errors_map_to_clear_statuses(db: str) -> None:
    def down(_: object) -> str:
        raise ProviderError("Ollama unreachable")

    client, _ = _client(db, down)
    r = client.post("/scripts", json={"member_id": "m-gap"})
    assert r.status_code == 503
    assert "unreachable" in r.json()["detail"]

    paid = FakeProvider([GOOD], name="claude", model="claude-opus-5-5")
    llm = LLMClient(paid, CostLedger(InMemoryLedgerStore(), Decimal("0")))
    app = create_app(
        Settings(_env_file=None, database_url=db),
        store=OutreachStore(db),
        generator=ScriptGenerator(llm, FilePromptStore(), P),
    )
    assert TestClient(app).post("/scripts", json={"member_id": "m-gap"}).status_code == 402
