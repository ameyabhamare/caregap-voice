"""Claude provider (paid, optional). Only for final evals and demo recordings.

Notes for the current Claude models:
- No sampling parameters: temperature/top_p/top_k are rejected, so none are sent.
- Thinking cannot be disabled on Opus 5.5; depth is set with ``output_config.effort``.
  Thinking tokens are billed as output tokens, so ``max_output_tokens`` must leave room.
- Server-side refusal fallbacks (``fallbacks="default"``) are enabled; the ledger charges
  the serving model's price if it is higher.
"""

from __future__ import annotations

from typing import Any, Literal

import anthropic

from caregap.llm.base import Completion, Message, ProviderError, RefusalError

Effort = Literal["low", "medium", "high", "xhigh", "max"]
FALLBACK_BETA = "server-side-fallback-2026-07-01"


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        effort: Effort = "medium",
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.effort = effort
        self._client = client or anthropic.Anthropic(api_key=api_key)

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        turns: list[Any] = [
            {"role": m.role, "content": m.content} for m in messages if m.role != "system"
        ]
        try:
            response = self._client.beta.messages.create(
                model=self.model,
                max_tokens=max_output_tokens,
                system=system,
                messages=turns,
                output_config={"effort": self.effort},
                betas=[FALLBACK_BETA],
                fallbacks="default",
            )
        except anthropic.RateLimitError as exc:
            raise ProviderError(f"Claude rate limited: {exc.message}") from exc
        except anthropic.APIStatusError as exc:
            raise ProviderError(f"Claude HTTP {exc.status_code}: {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise ProviderError(f"Claude unreachable: {exc}") from exc

        if response.stop_reason == "refusal":
            raise RefusalError(f"Claude declined: {response.stop_details}")
        text = "".join(block.text for block in response.content if block.type == "text")
        return Completion(
            text=text,
            model=response.model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=str(response.stop_reason),
        )
