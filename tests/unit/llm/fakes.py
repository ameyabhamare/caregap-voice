from __future__ import annotations

from collections.abc import Callable

from caregap.llm.base import Completion, Message


class FakeProvider:
    """Returns scripted replies in order; records every request."""

    def __init__(
        self,
        replies: list[str] | Callable[[list[Message]], str],
        *,
        name: str = "ollama",
        model: str = "fake-model",
    ) -> None:
        self.name = name
        self.model = model
        self._replies = replies
        self.calls: list[list[Message]] = []

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion:
        self.calls.append(messages)
        if callable(self._replies):
            text = self._replies(messages)
        else:
            text = self._replies[len(self.calls) - 1]
        return Completion(text=text, model=self.model, input_tokens=100, output_tokens=50,
                          stop_reason="stop")  # fmt: skip
