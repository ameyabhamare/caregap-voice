"""Callback Q&A against Postgres + pgvector, with a fake model and fake embeddings."""

from __future__ import annotations

import hashlib
import re
from decimal import Decimal
from pathlib import Path

import psycopg
import pytest
from fastapi.testclient import TestClient
from langchain_core.embeddings import Embeddings

from caregap.api.app import create_app
from caregap.config import Settings
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.customers import load_all
from caregap.llm.base import LLMClient
from caregap.outreach.store import OutreachStore
from caregap.prompts import FilePromptStore
from caregap.rag.store import ChunkStore, ingest
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration


class BagOfWords(Embeddings):
    """Deterministic 768-d hashed bag of words: enough for retrieval to be meaningful."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * 768
        for w in re.findall(r"[a-z]+", text.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 768] += 1.0  # noqa: S324
        return v

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE benefit_chunks, callback_questions, escalations CASCADE")
        conn.execute("TRUNCATE members CASCADE")
        conn.execute(
            "INSERT INTO members (member_id, given_name, family_name, birth_date)"
            " VALUES ('m-1', 'Ava', 'Synth', '1950-01-01')"
        )
    ingest(load_all(), ChunkStore(pg_url), BagOfWords(), "bag-of-words")
    return pg_url


def test_ingest_is_idempotent_and_search_stays_within_one_customer(db: str) -> None:
    store = ChunkStore(db)
    ingest(load_all(), store, BagOfWords(), "bag-of-words")
    with psycopg.connect(db) as conn:
        counts: dict[str, int] = dict(conn.execute(
            "SELECT customer, count(*) FROM benefit_chunks GROUP BY 1"
        ).fetchall())  # fmt: skip
    assert set(counts) == {"sunrise", "horizon"} and min(counts.values()) > 11
    q = BagOfWords().embed_query("specialist visit copay referral")
    for customer in ("sunrise", "horizon"):
        hits = store.search(customer, q, 4)
        assert hits[0][0].section == "S4"
        assert {c.customer for c, _ in hits} == {customer}
    assert "$4,900" in store.all_text("sunrise") and "$4,900" not in store.all_text("horizon")


def client(db: str, tmp_path: Path, replies: list[str]) -> TestClient:
    llm = LLMClient(FakeProvider(replies), CostLedger(InMemoryLedgerStore(), Decimal("5")))
    return TestClient(
        create_app(
            Settings(_env_file=None, database_url=db, data_dir=tmp_path),
            store=OutreachStore(db),
            llm=llm,
            prompts=FilePromptStore(),
            customers=load_all(),
            embeddings=BagOfWords(),
        )
    )


def test_answered_question_is_logged_without_escalation(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, ["benefit", "A specialist visit is a $35 copay [S4-2]."])
    r = c.post("/callbacks/questions",
               json={"customer": "sunrise", "question": "What is the specialist copay?",
                     "member_id": "m-1"})  # fmt: skip
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["outcome"], body["answer"], body["citations"]) == (
        "answered", "A specialist visit is a $35 copay.", ["S4-2"],
    )  # fmt: skip
    assert body["escalation_id"] is None
    with psycopg.connect(db) as conn:
        row = conn.execute(
            "SELECT route, outcome, draft, retrieved ? 'S4-2' FROM callback_questions"
        ).fetchone()
    assert row == ("benefit", "answered", "A specialist visit is a $35 copay [S4-2].", True)


def test_hand_offs_escalate_without_leaking_the_question(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, ["medical"])
    q = "My blood sugar was 180 this morning, is that bad?"
    body = c.post("/callbacks/questions", json={"customer": "horizon", "question": q}).json()
    assert (body["outcome"], body["reason"]) == ("handoff", "medical")
    assert body["answer"] == load_all()["horizon"].languages["en"].qa_medical
    [item] = c.get("/notifications/outbox").json()["items"]
    assert item["id"] == body["escalation_id"]
    assert "callback question" in item["text"] and "180" not in item["text"]


def test_unknown_customer_or_member(db: str, tmp_path: Path) -> None:
    c = client(db, tmp_path, [])
    assert c.post("/callbacks/questions",
                  json={"customer": "nobody", "question": "hi"}).status_code == 404  # fmt: skip
    r = c.post("/callbacks/questions",
               json={"customer": "sunrise", "question": "hi", "member_id": "nope"})  # fmt: skip
    assert r.status_code == 404
