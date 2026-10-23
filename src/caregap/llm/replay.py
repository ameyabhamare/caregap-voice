"""Record model responses at dev time, replay them in CI.

``LLM_FIXTURES=record`` wraps the real providers and appends every response to
``fixtures/ci/{llm,embeddings}.jsonl``, keyed by a hash of the whole request (provider,
model, messages or texts, token limit). ``LLM_FIXTURES=replay`` serves only those
responses: a request that was not recorded raises, so CI can never fall through to a
model, and any change to a prompt, a member or the code that builds requests shows up
as a missing recording.
"""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any

from caregap.llm.base import Completion, LLMProvider, Message, ProviderError
from caregap.llm.embeddings import EmbeddingProvider, EmbeddingResult

FIXTURES_DIR = Path("fixtures/ci")


class MissingRecordingError(ProviderError):
    pass


def request_key(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


class FixtureFile:
    """JSON lines of ``{"key": ..., "preview": ..., "response": {...}}``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.entries: dict[str, dict[str, Any]] = {}
        if path.exists():
            for line in path.read_text().splitlines():
                if line.strip():
                    e = json.loads(line)
                    self.entries[e["key"]] = e

    def get(self, key: str) -> dict[str, Any] | None:
        e = self.entries.get(key)
        return None if e is None else dict(e["response"])

    def put(self, key: str, preview: str, response: dict[str, Any]) -> None:
        with self._lock:
            if key in self.entries:
                return
            entry = {"key": key, "preview": preview[:120], "response": response}
            self.entries[key] = entry
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _chat_payload(
    name: str, model: str, messages: list[Message], max_tokens: int
) -> dict[str, Any]:
    return {
        "provider": name,
        "model": model,
        "max_output_tokens": max_tokens,
        "messages": [[m.role, m.content] for m in messages],
    }


class RecordingProvider:
    def __init__(self, inner: LLMProvider, fixtures: FixtureFile) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.fixtures = fixtures

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion:
        c = self.inner.complete(messages, max_output_tokens=max_output_tokens)
        key = request_key(_chat_payload(self.name, self.model, messages, max_output_tokens))
        self.fixtures.put(key, messages[-1].content, {
            "text": c.text, "model": c.model, "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens, "stop_reason": c.stop_reason,
        })  # fmt: skip
        return c


class ReplayProvider:
    def __init__(self, name: str, model: str, fixtures: FixtureFile) -> None:
        self.name = name
        self.model = model
        self.fixtures = fixtures

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion:
        key = request_key(_chat_payload(self.name, self.model, messages, max_output_tokens))
        r = self.fixtures.get(key)
        if r is None:
            raise MissingRecordingError(
                f"no recorded response for {key} ({messages[-1].content[:60]!r}); "
                "run make record-fixtures"
            )
        return Completion(**r)


class RecordingEmbeddingProvider:
    def __init__(self, inner: EmbeddingProvider, fixtures: FixtureFile) -> None:
        self.inner = inner
        self.name = inner.name
        self.model = inner.model
        self.fixtures = fixtures

    def embed(self, texts: list[str]) -> EmbeddingResult:
        result = self.inner.embed(texts)
        for text, vector in zip(texts, result.vectors, strict=True):
            key = request_key({"provider": self.name, "model": self.model, "text": text})
            # Rounded: the fixture stays small and retrieval order does not change.
            self.fixtures.put(key, text, {"vector": [round(v, 5) for v in vector]})
        return result


class ReplayEmbeddingProvider:
    def __init__(self, name: str, model: str, fixtures: FixtureFile) -> None:
        self.name = name
        self.model = model
        self.fixtures = fixtures

    def embed(self, texts: list[str]) -> EmbeddingResult:
        vectors = []
        for text in texts:
            key = request_key({"provider": self.name, "model": self.model, "text": text})
            r = self.fixtures.get(key)
            if r is None:
                raise MissingRecordingError(
                    f"no recorded embedding for {key} ({text[:60]!r}); run make record-fixtures"
                )
            vectors.append(r["vector"])
        return EmbeddingResult(vectors, sum(len(t) // 4 for t in texts))
