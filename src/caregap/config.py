"""Runtime settings, read from environment variables (see .env.example)."""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql://caregap:caregap@localhost:5432/caregap"

    llm_provider: Literal["ollama", "claude"] = "ollama"
    # Hard cap on total paid spend across all runs, enforced by caregap.cost.CostLedger.
    llm_budget_usd: Decimal = Field(default=Decimal("5.00"), ge=0)

    ollama_base_url: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:3b"

    anthropic_api_key: SecretStr | None = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "medium"

    mlflow_tracking_uri: str = "http://localhost:5000"
    fhir_base_url: str = "http://localhost:8080/fhir"

    # Generated data (Synthea output, NDJSON export, caches). Gitignored.
    data_dir: Path = Path("data")


@lru_cache
def get_settings() -> Settings:
    return Settings()
