"""Text embeddings, metered by the cost ledger like every other model call.

Embeddings always run locally on Ollama (``nomic-embed-text``), whatever ``LLM_PROVIDER``
is: Claude has no embedding model, and local embeddings keep the vector store free.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol

import httpx
from langchain_core.embeddings import Embeddings

from caregap import observability
from caregap.cost import CostLedger
from caregap.llm.base import ProviderError


@dataclass(frozen=True)
class EmbeddingResult:
    vectors: list[list[float]]
    input_tokens: int


class EmbeddingProvider(Protocol):
    name: str
    model: str

    def embed(self, texts: list[str]) -> EmbeddingResult: ...


class OllamaEmbeddingProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        timeout_s: float = 120,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def embed(self, texts: list[str]) -> EmbeddingResult:
        try:
            r = self._client.post("/api/embed", json={"model": self.model, "input": texts})
        except httpx.TransportError as exc:
            raise ProviderError(f"Ollama unreachable: {exc}") from exc
        if r.status_code == 404:
            raise ProviderError(f"Ollama embedding model {self.model!r} not found")
        if r.is_error:
            raise ProviderError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        vectors = data.get("embeddings") or []
        if len(vectors) != len(texts):
            raise ProviderError(f"Ollama returned {len(vectors)} embeddings for {len(texts)}")
        return EmbeddingResult(vectors, int(data.get("prompt_eval_count", 0)))


class EmbeddingClient:
    """An embedding provider plus the cost ledger. The only way code embeds text."""

    def __init__(self, provider: EmbeddingProvider, ledger: CostLedger) -> None:
        self.provider = provider
        self.ledger = ledger

    @property
    def model(self) -> str:
        return self.provider.model

    def embed(self, texts: list[str], *, purpose: str) -> list[list[float]]:
        attrs = {"provider": self.provider.name, "model": self.provider.model, "purpose": purpose}
        with (
            observability.span("embed", "EMBEDDING", attributes=attrs) as span,
            self.ledger.guard(
                provider=self.provider.name,
                model=self.provider.model,
                input_tokens=sum(math.ceil(len(t) / 3) + 10 for t in texts),
                max_output_tokens=0,
                purpose=purpose,
            ) as usage,
        ):
            span.set_inputs({"texts": len(texts)})
            result = self.provider.embed(texts)
            usage.input_tokens = result.input_tokens
            usage.output_tokens = 0
            span.set_attribute("input_tokens", result.input_tokens)
        return result.vectors


class LangChainEmbeddings(Embeddings):
    """LangChain's embeddings interface over the metered client. ``nomic-embed-text``
    expects task prefixes on documents and queries."""

    def __init__(self, client: EmbeddingClient, *, purpose: str = "rag") -> None:
        self.client = client
        self.purpose = purpose

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        docs = [f"search_document: {t}" for t in texts]
        return self.client.embed(docs, purpose=f"{self.purpose}:documents")

    def embed_query(self, text: str) -> list[float]:
        return self.client.embed([f"search_query: {text}"], purpose=f"{self.purpose}:query")[0]
