"""Ollama provider (local, free). The default for all development."""

from __future__ import annotations

from typing import Any

import httpx

from caregap.llm.base import Completion, Message, ProviderError


class OllamaProvider:
    name = "ollama"

    def __init__(
        self,
        base_url: str,
        model: str,
        *,
        temperature: float = 0.3,
        seed: int = 7,
        timeout_s: float = 300,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = model
        self.temperature = temperature
        self.seed = seed
        self._client = httpx.Client(base_url=base_url, timeout=timeout_s, transport=transport)

    def complete(self, messages: list[Message], *, max_output_tokens: int) -> Completion:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "stream": False,
            "options": {
                "temperature": self.temperature,
                "seed": self.seed,
                "num_predict": max_output_tokens,
            },
        }
        try:
            r = self._client.post("/api/chat", json=body)
        except httpx.TransportError as exc:
            raise ProviderError(f"Ollama unreachable: {exc}") from exc
        if r.status_code == 404:
            raise ProviderError(f"Ollama model {self.model!r} not found; run `make pull-model`")
        if r.is_error:
            raise ProviderError(f"Ollama HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        return Completion(
            text=data["message"]["content"],
            model=self.model,
            input_tokens=int(data.get("prompt_eval_count", 0)),
            output_tokens=int(data.get("eval_count", 0)),
            stop_reason=str(data.get("done_reason", "stop")),
        )
