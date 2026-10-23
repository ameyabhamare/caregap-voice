from __future__ import annotations

import json
from decimal import Decimal

import httpx
import pytest

from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.llm.base import ProviderError
from caregap.llm.embeddings import (
    EmbeddingClient,
    EmbeddingResult,
    LangChainEmbeddings,
    OllamaEmbeddingProvider,
)


class FakeEmbedder:
    name = "ollama"
    model = "fake-embed"

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, texts: list[str]) -> EmbeddingResult:
        self.calls.append(texts)
        return EmbeddingResult([[float(len(t)), 1.0] for t in texts], 7)


def test_ollama_embed_request_and_errors() -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, json={"embeddings": [[0.1, 0.2]], "prompt_eval_count": 3})

    provider = OllamaEmbeddingProvider("http://o", "nomic-embed-text",
                                       transport=httpx.MockTransport(handler))  # fmt: skip
    assert provider.embed(["hi"]) == EmbeddingResult([[0.1, 0.2]], 3)
    assert seen == [{"model": "nomic-embed-text", "input": ["hi"]}]
    with pytest.raises(ProviderError, match="1 embeddings for 2"):
        provider.embed(["a", "b"])
    missing = OllamaEmbeddingProvider(
        "http://o", "x", transport=httpx.MockTransport(lambda _: httpx.Response(404))
    )
    with pytest.raises(ProviderError, match="not found"):
        missing.embed(["a"])


def test_embeddings_are_metered_and_prefixed_for_nomic() -> None:
    fake = FakeEmbedder()
    ledger = CostLedger(InMemoryLedgerStore(), Decimal("0"))  # local: allowed at $0
    emb = LangChainEmbeddings(EmbeddingClient(fake, ledger))
    assert emb.embed_documents(["a b"]) == [[float(len("search_document: a b")), 1.0]]
    emb.embed_query("q")
    assert fake.calls == [["search_document: a b"], ["search_query: q"]]
    assert ledger.committed_usd() == 0


def test_paid_embedding_providers_are_refused_without_a_price() -> None:
    fake = FakeEmbedder()
    fake.name = "claude"
    client = EmbeddingClient(fake, CostLedger(InMemoryLedgerStore(), Decimal("5")))
    with pytest.raises(KeyError):
        client.embed(["a"], purpose="t")
    assert fake.calls == []
