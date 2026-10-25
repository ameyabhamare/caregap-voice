from __future__ import annotations

from pathlib import Path

import pytest

from caregap.config import Settings
from caregap.llm.base import Message
from caregap.llm.factory import build_provider
from caregap.llm.replay import (
    FixtureFile,
    MissingRecordingError,
    RecordingEmbeddingProvider,
    RecordingProvider,
    ReplayEmbeddingProvider,
    ReplayProvider,
)
from tests.unit.llm.fakes import FakeProvider
from tests.unit.llm.test_embeddings import FakeEmbedder

MESSAGES = [Message("system", "be brief"), Message("user", "hi")]


def test_record_then_replay_chat(tmp_path: Path) -> None:
    path = tmp_path / "llm.jsonl"
    rec = RecordingProvider(FakeProvider(["hello", "again"]), FixtureFile(path))
    first = rec.complete(MESSAGES, max_output_tokens=50)
    rec.complete(MESSAGES, max_output_tokens=50)  # same request: recorded once
    assert len(path.read_text().splitlines()) == 1

    replay = ReplayProvider("ollama", "fake-model", FixtureFile(path))
    again = replay.complete(MESSAGES, max_output_tokens=50)
    assert (again.text, again.input_tokens, again.model) == ("hello", 100, first.model)
    with pytest.raises(MissingRecordingError, match="make record-fixtures"):
        replay.complete(MESSAGES, max_output_tokens=51)  # any change is a different request
    with pytest.raises(MissingRecordingError):
        replay.complete([Message("user", "other")], max_output_tokens=50)


def test_record_then_replay_embeddings(tmp_path: Path) -> None:
    path = tmp_path / "emb.jsonl"
    rec = RecordingEmbeddingProvider(FakeEmbedder(), FixtureFile(path))
    rec.embed(["a", "bb"])
    replay = ReplayEmbeddingProvider("ollama", "fake-embed", FixtureFile(path))
    assert replay.embed(["bb", "a"]).vectors == [[2.0, 1.0], [1.0, 1.0]]
    with pytest.raises(MissingRecordingError):
        replay.embed(["c"])


def test_factory_replay_never_builds_a_live_provider(tmp_path: Path) -> None:
    settings = Settings(_env_file=None, llm_fixtures="replay", llm_fixtures_dir=tmp_path,
                        ollama_base_url="http://unreachable.invalid")  # fmt: skip
    provider = build_provider(settings)
    assert isinstance(provider, ReplayProvider)
    with pytest.raises(MissingRecordingError):
        provider.complete(MESSAGES, max_output_tokens=10)
