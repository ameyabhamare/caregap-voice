from decimal import Decimal

import pytest

from caregap.cost import UnknownModelPriceError, cost_usd, is_free
from caregap.cost.pricing import ModelPrice


def test_local_providers_are_free_for_any_model() -> None:
    for provider in ("ollama", "kokoro", "faster_whisper"):
        assert is_free(provider)
        assert cost_usd(provider, "anything", 1_000_000, 1_000_000) == 0


def test_claude_cost_uses_per_mtok_rates() -> None:
    # Sonnet 5.5: $2 in, $10 out per MTok.
    assert cost_usd("claude", "claude-sonnet-5-5", 1_000_000, 0) == Decimal("2")
    assert cost_usd("claude", "claude-sonnet-5-5", 1_000, 500) == Decimal("0.007")


def test_haiku_long_context_rate_applies_above_threshold() -> None:
    short = cost_usd("claude", "claude-haiku-5-5", 100_000, 1_000)
    long = cost_usd("claude", "claude-haiku-5-5", 100_001, 1_000)
    assert short == Decimal("0.0105")  # 0.1*0.1 + 0.001*0.5
    assert long > short * 4


def test_unknown_paid_model_fails_closed() -> None:
    with pytest.raises(UnknownModelPriceError):
        cost_usd("claude", "claude-made-up", 1, 1)
    with pytest.raises(UnknownModelPriceError):
        cost_usd("openai_tts", "tts-1", 1, 1)


def test_negative_tokens_rejected() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        cost_usd("claude", "claude-sonnet-5-5", -1, 0)


def test_partial_long_context_config_rejected() -> None:
    with pytest.raises(ValueError, match="go together"):
        ModelPrice(Decimal(1), Decimal(1), long_context_threshold=10)
