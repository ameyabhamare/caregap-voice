"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://caregap:caregap@localhost:5432/caregap"

    llm_provider: Literal["ollama", "claude"] = "ollama"
    # Hard cap on total paid spend across all runs, enforced by caregap.cost.CostLedger.
    llm_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0)

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"
    #: Local embedding model for benefit documents (always Ollama). The vector column is
    #: 768-dimensional, so a model with another size needs a schema change.
    embed_model: str = "nomic-embed-text"

    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    #: off | record (save every model response) | replay (serve only saved ones; CI).
    llm_fixtures: Literal["off", "record", "replay"] = "off"
    llm_fixtures_dir: Path = Path("fixtures/ci")

    mlflow_tracking_uri: str = "http://localhost:5000"
    #: Trace core operations to MLflow (Phase 10). Off unless set: tests and CI never trace.
    mlflow_tracing: bool = False
    fhir_base_url: str = "http://localhost:8080/fhir"

    #: Customer configs (one YAML per health plan); validated at startup.
    customers_dir: Path = Path("customers")

    # Generated data (Synthea output, NDJSON export, caches). Gitignored.
    data_dir: Path = Path("data")

    # Voice (Phase 4). Kokoro is the only enabled TTS; paid providers are stubs.
    tts_provider: Literal["kokoro"] = "kokoro"
    tts_voice: str = "af_heart"
    tts_pronunciation_overrides: bool = True
    stt_model: str = "small.en"

    # Campaigns and telephony (Phase 5). Defaults are for running on the host; compose
    # overrides the URLs with service names. See .env.example.
    #: Start the service clock at this instant (with offset) and let it run; demo only.
    campaign_clock: datetime | None = None
    #: The core service as Twilio reaches it (TwiML and audio URLs).
    public_base_url: str = "http://localhost:8000"
    #: Where Twilio sends call status and DTMF callbacks (the n8n webhook).
    twilio_callback_url: str = "http://localhost:5678/webhook/twilio-events"
    #: Twilio REST API base; the local mock by default.
    twilio_api_base_url: str = "http://localhost:8100"
    #: The core service as the MCP server (caregap.mcp, Phase 9) reaches it.
    core_url: str = "http://localhost:8000"
    twilio_account_sid: str = "ACmock0000000000000000000000000000"
    twilio_auth_token: SecretStr = SecretStr("mock-auth-token")
    twilio_from_number: str = "+15550100000"
    #: Slack-compatible incoming webhook for alerts and escalations ({"text": ...}).
    #: Defaults to the core service's own log sink.
    notify_webhook_url: str = "http://localhost:8000/notifications"

    @field_validator("campaign_clock", mode="before")
    @classmethod
    def _empty_clock_is_unset(cls, v: object) -> object:
        return None if v == "" else v


@lru_cache
def get_settings() -> Settings:
    return Settings()
