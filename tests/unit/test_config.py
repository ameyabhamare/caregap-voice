from decimal import Decimal

import pytest

from caregap.config import Settings


def test_defaults_are_local_and_capped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("LLM_BUDGET_USD", raising=False)
    s = Settings(_env_file=None)
    assert s.llm_provider == "ollama"
    assert s.llm_budget_usd == Decimal("5.00")


def test_budget_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUDGET_USD", "1.25")
    assert Settings(_env_file=None).llm_budget_usd == Decimal("1.25")


def test_negative_budget_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_BUDGET_USD", "-1")
    with pytest.raises(ValueError, match="greater than or equal"):
        Settings(_env_file=None)


def test_unknown_provider_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError, match="ollama"):
        Settings(_env_file=None)
