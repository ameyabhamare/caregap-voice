"""Provider interface and the metered client every runtime model call goes through."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Literal, Protocol

from caregap.cost import CostLedger

Role = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class Message:
    role: Role
    content: str


@dataclass(frozen=True)
class Completion:
    text: str
    model: str  # the model that actually served the call
    input_tokens: int
    output_tokens: int
    stop_reason: str
    latency_s: float = 0.0


class ProviderError(RuntimeError):
    """The provider failed or returned something unusable."""


class RefusalError(ProviderError):
    """The model declined the request."""


class LLMProvider(Protocol):
    #: Name used for pricing and the ledger (``ollama``, ``claude``, ...).
    name: str
    model: str

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion: ...


def estimate_input_tokens(messages: list[Message]) -> int:
    """Deliberately high estimate (~3 chars per token plus overhead) for budget reservation."""
    return sum(math.ceil(len(m.content) / 3) + 10 for m in messages)


class LLMClient:
    """A provider plus the cost ledger. The only way application code calls a model."""

    def __init__(self, provider: LLMProvider, ledger: CostLedger) -> None:
        self.provider = provider
        self.ledger = ledger

    @property
    def model(self) -> str:
        return self.provider.model

    def complete(
        self, messages: list[Message], *, max_output_tokens: int, purpose: str
    ) -> Completion:
        with self.ledger.guard(
            provider=self.provider.name,
            model=self.provider.model,
            input_tokens=estimate_input_tokens(messages),
            max_output_tokens=max_output_tokens,
            purpose=purpose,
        ) as usage:
            started = time.monotonic()
            completion = self.provider.complete(messages, max_output_tokens=max_output_tokens)
            usage.input_tokens = completion.input_tokens
            usage.output_tokens = completion.output_tokens
            usage.served_model = completion.model
        return Completion(
            text=completion.text,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            stop_reason=completion.stop_reason,
            latency_s=time.monotonic() - started,
        )
