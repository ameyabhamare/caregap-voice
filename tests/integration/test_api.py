"""Core service API against Postgres, with a fake model (needs `make up`)."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import psycopg
import pytest
from fastapi.testclient import TestClient

from caregap.api.app import create_app
from caregap.config import Settings
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.llm.base import LLMClient, ProviderError
from caregap.outreach.store import OutreachStore
from caregap.prompts import FilePromptStore
from caregap.voice.base import Audio
from caregap.voice.render import AudioRenderer
from tests.helpers import SUNRISE, P
from tests.unit.llm.fakes import FakeProvider

pytestmark = pytest.mark.integration

GOOD = f"Hi Ava, a yearly wellness visit with your doctor is due. {P.disclaimer} {P.opt_out}"
BAD = "Hi Ava, about your diabetes."


@pytest.fixture
def db(pg_url: str) -> str:
    with psycopg.connect(pg_url) as conn:
        conn.execute("TRUNCATE script_audio, outreach_scripts, care_gaps, members CASCADE")
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
        llm=llm,
        prompts=FilePromptStore(),
        customers={"sunrise": SUNRISE},
    )
    return TestClient(app), provider


def test_health_and_gaps(db: str) -> None:
    client, _ = _client(db, [])
    health = client.get("/health").json()
    assert {k: health[k] for k in ("status", "provider", "model")} == {
        "status": "ok",
        "provider": "ollama",
        "model": "fake-model",
    }
    assert "clock" in health
    gaps = client.get("/members/m-gap/gaps").json()
    assert [(g["measure"], g["status"]) for g in gaps] == [("awv", "gap"), ("statin", "met")]
    assert client.get("/members/nobody/gaps").status_code == 404


def test_compliant_script_is_stored(db: str) -> None:
    client, provider = _client(db, [GOOD])
    r = client.post("/scripts", json={"member_id": "m-gap"})
    assert r.status_code == 201
    body = r.json()
    assert (body["status"], body["measures"], body["attempts"]) == ("compliant", ["awv"], 1)
    assert (body["customer"], body["language"]) == ("sunrise", "en")
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
    r = client.post("/scripts", json={"member_id": "m-none"})  # 35: no customer takes them
    assert (r.status_code, "no configured customer" in r.json()["detail"]) == (409, True)
    r = client.post("/scripts", json={"member_id": "m-gap", "customer": "nobody"})
    assert r.status_code == 404
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
        llm=llm,
        prompts=FilePromptStore(),
        customers={"sunrise": SUNRISE},
    )
    assert TestClient(app).post("/scripts", json={"member_id": "m-gap"}).status_code == 402


class FakeTTS:
    name = "fake-tts"

    def synthesize(self, text: str, *, voice: str, lang: str = "en-us") -> Audio:
        return Audio(np.zeros(24000, dtype=np.float32), 24000)


def _audio_client(db: str, tmp_path: Path, replies: list[str]) -> TestClient:
    llm = LLMClient(FakeProvider(replies), CostLedger(InMemoryLedgerStore(), Decimal("5")))
    renderer = AudioRenderer(db, FakeTTS(), tmp_path, overrides_enabled=True)
    app = create_app(
        Settings(_env_file=None, database_url=db),
        store=OutreachStore(db),
        llm=llm,
        prompts=FilePromptStore(),
        customers={"sunrise": SUNRISE},
        renderer=renderer,
    )
    return TestClient(app)


def test_render_audio_for_compliant_script(db: str, tmp_path: Path) -> None:
    client = _audio_client(db, tmp_path, [GOOD])
    script = client.post("/scripts", json={"member_id": "m-gap"}).json()
    assert client.get(f"/scripts/{script['id']}/audio").status_code == 404

    r = client.post(f"/scripts/{script['id']}/audio")
    assert r.status_code == 201
    body = r.json()
    assert (body["provider"], body["voice"], body["duration_ms"]) == ("fake-tts", "af_heart", 1000)
    wav = client.get(body["url"])
    assert wav.headers["content-type"] == "audio/wav"
    assert wav.content[:4] == b"RIFF"


def test_render_refuses_scripts_needing_review(db: str, tmp_path: Path) -> None:
    client = _audio_client(db, tmp_path, [BAD, BAD])
    script = client.post("/scripts", json={"member_id": "m-gap"}).json()
    assert client.post(f"/scripts/{script['id']}/audio").status_code == 409
    assert client.post("/scripts/999999/audio").status_code == 404
