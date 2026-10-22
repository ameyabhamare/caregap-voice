from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import anthropic
import httpx
import httpx2
import pytest

from caregap.cost import BudgetExceededError, CostLedger, InMemoryLedgerStore
from caregap.llm.base import LLMClient, Message, ProviderError, RefusalError
from caregap.llm.claude import FALLBACK_BETA, ClaudeProvider
from caregap.llm.ollama import OllamaProvider

from .fakes import FakeProvider

MESSAGES = [Message("system", "Be brief."), Message("user", "Hello")]


# ----------------------------------------------------------------------------- Ollama


def test_ollama_request_and_response() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={
            "message": {"role": "assistant", "content": "Hi there"},
            "prompt_eval_count": 21, "eval_count": 4, "done_reason": "stop",
        })  # fmt: skip

    provider = OllamaProvider("http://ollama", "qwen2.5:3b", transport=httpx.MockTransport(handler))
    c = provider.complete(MESSAGES, max_output_tokens=200)
    assert (c.text, c.input_tokens, c.output_tokens, c.model) == ("Hi there", 21, 4, "qwen2.5:3b")
    assert seen["stream"] is False
    assert seen["options"] == {"temperature": 0.3, "seed": 7, "num_predict": 200}
    assert seen["messages"][0] == {"role": "system", "content": "Be brief."}


def test_ollama_missing_model_explains_fix() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404, json={"error": "not found"}))
    provider = OllamaProvider("http://ollama", "qwen2.5:3b", transport=transport)
    with pytest.raises(ProviderError, match="make pull-model"):
        provider.complete(MESSAGES, max_output_tokens=10)


# ----------------------------------------------------------------------------- Claude


def _claude(handler: Any) -> ClaudeProvider:
    client = anthropic.Anthropic(
        api_key="test-key",
        base_url="http://claude.test",
        max_retries=0,
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    return ClaudeProvider("claude-opus-5-5", effort="low", client=client)


def _message(
    content: list[Any], stop_reason: str = "end_turn", model: str = "claude-opus-5-5"
) -> Any:
    return {
        "id": "msg_1", "type": "message", "role": "assistant", "model": model,
        "content": content, "stop_reason": stop_reason, "stop_sequence": None,
        "usage": {"input_tokens": 30, "output_tokens": 70},
    }  # fmt: skip


def test_claude_request_shape_and_text_extraction() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen["body"] = json.loads(request.content)
        seen["beta"] = request.headers.get("anthropic-beta", "")
        return httpx2.Response(200, json=_message([
            {"type": "thinking", "thinking": "", "signature": "sig"},
            {"type": "text", "text": "Hello, "},
            {"type": "text", "text": "Ava."},
        ]))  # fmt: skip

    c = _claude(handler).complete(MESSAGES, max_output_tokens=2000)
    assert c.text == "Hello, Ava."
    assert (c.input_tokens, c.output_tokens) == (30, 70)
    body = seen["body"]
    assert body["system"] == "Be brief."
    assert body["messages"] == [{"role": "user", "content": "Hello"}]
    assert body["output_config"] == {"effort": "low"}
    assert body["fallbacks"] == "default"
    assert FALLBACK_BETA in seen["beta"]
    # Current Claude models reject sampling parameters.
    assert not {"temperature", "top_p", "top_k"} & body.keys()


def test_claude_reports_serving_model_after_fallback() -> None:
    reply = _message([{"type": "text", "text": "ok"}], model="claude-opus-4-8")
    c = _claude(lambda _: httpx2.Response(200, json=reply)).complete(MESSAGES, max_output_tokens=50)
    assert c.model == "claude-opus-4-8"


def test_claude_refusal_raises() -> None:
    reply = _message([], stop_reason="refusal")
    with pytest.raises(RefusalError):
        _claude(lambda _: httpx2.Response(200, json=reply)).complete(MESSAGES, max_output_tokens=50)


def test_claude_api_error_is_provider_error() -> None:
    error = {"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}}
    with pytest.raises(ProviderError, match="400"):
        _claude(lambda _: httpx2.Response(400, json=error)).complete(MESSAGES, max_output_tokens=50)


# ----------------------------------------------------------------------------- LLMClient


def test_client_meters_paid_calls_through_the_ledger() -> None:
    ledger = CostLedger(InMemoryLedgerStore(), Decimal("5"))
    client = LLMClient(FakeProvider(["hi"], name="claude", model="claude-opus-5-5"), ledger)
    c = client.complete(MESSAGES, max_output_tokens=100, purpose="test")
    assert c.text == "hi"
    # 100 in x $4/MTok + 50 out x $20/MTok
    assert ledger.committed_usd() == Decimal("0.0014")


def test_client_refuses_before_calling_when_budget_is_spent() -> None:
    provider = FakeProvider(["hi"], name="claude", model="claude-opus-5-5")
    client = LLMClient(provider, CostLedger(InMemoryLedgerStore(), Decimal("0")))
    with pytest.raises(BudgetExceededError):
        client.complete(MESSAGES, max_output_tokens=100, purpose="test")
    assert provider.calls == []


def test_client_local_calls_cost_nothing_even_with_zero_budget() -> None:
    ledger = CostLedger(InMemoryLedgerStore(), Decimal("0"))
    LLMClient(FakeProvider(["hi"]), ledger).complete(MESSAGES, max_output_tokens=100, purpose="t")
    assert ledger.committed_usd() == 0
