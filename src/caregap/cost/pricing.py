"""Per-model token prices.

Prices are hard-coded and must be re-checked against the provider's pricing page before any
paid run (Phase 11). Last checked: 2026-10-06 (Anthropic first-party API rates).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from caregap.cost.errors import UnknownModelPriceError

# Providers that run locally and never cost money.
LOCAL_PROVIDERS = frozenset({"ollama", "kokoro", "faster_whisper"})

_MTOK = Decimal(1_000_000)


@dataclass(frozen=True)
class ModelPrice:
    input_per_mtok: Decimal
    output_per_mtok: Decimal
    # Some models bill all tokens at a higher rate once the prompt passes a threshold.
    long_context_threshold: int | None = None
    long_input_per_mtok: Decimal | None = None
    long_output_per_mtok: Decimal | None = None

    def __post_init__(self) -> None:
        long_rates = (self.long_input_per_mtok, self.long_output_per_mtok)
        if (self.long_context_threshold is None) != (None in long_rates):
            raise ValueError("long-context threshold and both long-context rates go together")

    def cost(self, input_tokens: int, output_tokens: int) -> Decimal:
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError("token counts must be non-negative")
        in_rate, out_rate = self.input_per_mtok, self.output_per_mtok
        if (
            self.long_context_threshold is not None
            and self.long_input_per_mtok is not None
            and self.long_output_per_mtok is not None
            and input_tokens > self.long_context_threshold
        ):
            in_rate, out_rate = self.long_input_per_mtok, self.long_output_per_mtok
        return (input_tokens * in_rate + output_tokens * out_rate) / _MTOK


PRICES: dict[tuple[str, str], ModelPrice] = {
    ("claude", "claude-opus-5-5"): ModelPrice(Decimal("4.00"), Decimal("20.00")),
    ("claude", "claude-sonnet-5-5"): ModelPrice(Decimal("2.00"), Decimal("10.00")),
    ("claude", "claude-haiku-5-5"): ModelPrice(
        Decimal("0.10"),
        Decimal("0.50"),
        long_context_threshold=100_000,
        long_input_per_mtok=Decimal("0.50"),
        long_output_per_mtok=Decimal("2.50"),
    ),
}


def is_free(provider: str) -> bool:
    return provider in LOCAL_PROVIDERS


def price_for(provider: str, model: str) -> ModelPrice:
    try:
        return PRICES[(provider, model)]
    except KeyError:
        raise UnknownModelPriceError(
            f"no price for provider={provider!r} model={model!r}"
        ) from None


def cost_usd(provider: str, model: str, input_tokens: int, output_tokens: int) -> Decimal:
    if is_free(provider):
        return Decimal(0)
    return price_for(provider, model).cost(input_tokens, output_tokens)
