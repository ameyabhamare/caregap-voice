"""Build the configured LLM client from settings."""

from __future__ import annotations

from caregap.config import Settings
from caregap.cost import CostLedger, PostgresLedgerStore
from caregap.llm.base import LLMClient, LLMProvider
from caregap.llm.claude import ClaudeProvider
from caregap.llm.embeddings import EmbeddingClient, OllamaEmbeddingProvider
from caregap.llm.ollama import OllamaProvider


def build_provider(settings: Settings) -> LLMProvider:
    if settings.llm_provider == "claude":
        if settings.anthropic_api_key is None:
            raise RuntimeError("LLM_PROVIDER=claude needs ANTHROPIC_API_KEY")
        return ClaudeProvider(
            settings.claude_model,
            api_key=settings.anthropic_api_key.get_secret_value(),
            effort=settings.claude_effort,
        )
    return OllamaProvider(settings.ollama_base_url, settings.ollama_model)


def build_client(settings: Settings) -> LLMClient:
    ledger = CostLedger(PostgresLedgerStore(settings.database_url), settings.llm_budget_usd)
    return LLMClient(build_provider(settings), ledger)


def build_embeddings(settings: Settings) -> EmbeddingClient:
    ledger = CostLedger(PostgresLedgerStore(settings.database_url), settings.llm_budget_usd)
    return EmbeddingClient(
        OllamaEmbeddingProvider(settings.ollama_base_url, settings.embed_model), ledger
    )
