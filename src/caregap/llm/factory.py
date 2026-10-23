"""Build the configured LLM client from settings."""

from __future__ import annotations

from caregap.config import Settings
from caregap.cost import CostLedger, PostgresLedgerStore
from caregap.llm.base import LLMClient, LLMProvider
from caregap.llm.claude import ClaudeProvider
from caregap.llm.embeddings import EmbeddingClient, EmbeddingProvider, OllamaEmbeddingProvider
from caregap.llm.ollama import OllamaProvider


def build_provider(settings: Settings) -> LLMProvider:
    """The configured provider, wrapped for recording or replay if LLM_FIXTURES says so."""
    from caregap.llm import replay

    model = settings.claude_model if settings.llm_provider == "claude" else settings.ollama_model
    fixtures_file = settings.llm_fixtures_dir / "llm.jsonl"
    if settings.llm_fixtures == "replay":
        return replay.ReplayProvider(
            settings.llm_provider, model, replay.FixtureFile(fixtures_file)
        )
    provider = _live_provider(settings)
    if settings.llm_fixtures == "record":
        return replay.RecordingProvider(provider, replay.FixtureFile(fixtures_file))
    return provider


def _live_provider(settings: Settings) -> LLMProvider:
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
    from caregap.llm import replay

    ledger = CostLedger(PostgresLedgerStore(settings.database_url), settings.llm_budget_usd)
    fixtures_file = settings.llm_fixtures_dir / "embeddings.jsonl"
    provider: EmbeddingProvider
    if settings.llm_fixtures == "replay":
        provider = replay.ReplayEmbeddingProvider(
            "ollama", settings.embed_model, replay.FixtureFile(fixtures_file)
        )
    else:
        provider = OllamaEmbeddingProvider(settings.ollama_base_url, settings.embed_model)
        if settings.llm_fixtures == "record":
            recordings = replay.FixtureFile(fixtures_file)
            provider = replay.RecordingEmbeddingProvider(provider, recordings)
    return EmbeddingClient(provider, ledger)
