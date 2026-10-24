"""Tracing (Phase 10): off is a no-op; on, traces nest and are tagged; it never fails a
request. Uses a throwaway local MLflow file store, never the dev server."""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import mlflow
import pytest

from caregap import observability
from caregap.cost import CostLedger, InMemoryLedgerStore
from caregap.llm.base import LLMClient, Message
from tests.unit.llm.fakes import FakeProvider


@pytest.fixture
def traced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("MLFLOW_ALLOW_FILE_STORE", "true")
    before = mlflow.get_tracking_uri()
    assert observability.configure(True, (tmp_path / "mlruns").as_uri())
    try:
        yield
    finally:
        observability.configure(False)
        mlflow.set_tracking_uri(before)


def test_off_is_a_no_op() -> None:
    observability.configure(False)
    with observability.span("x", root=True, tags={"member_id": "m"}) as s:
        s.set_outputs("ignored")
    assert observability.trace_id() is None and not observability.enabled()


def test_unreachable_mlflow_turns_tracing_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MLFLOW_ALLOW_FILE_STORE", raising=False)
    before = mlflow.get_tracking_uri()
    try:
        assert observability.configure(True, "file:///nonexistent/mlruns") is False
    finally:
        mlflow.set_tracking_uri(before)


@pytest.mark.usefixtures("traced")
def test_model_calls_nest_in_the_root_trace_with_tags_and_cost() -> None:
    llm = LLMClient(FakeProvider(["hi"], name="claude", model="claude-sonnet-5-5"),
                    CostLedger(InMemoryLedgerStore(), Decimal("5")))  # fmt: skip
    tags = {"customer": "sunrise", "member_id": "m-1", "campaign_id": None}
    with observability.span("prepare", "CHAIN", root=True, tags=tags):
        c = llm.complete([Message("user", "x")], max_output_tokens=10, purpose="t")
        tid = observability.trace_id()
    assert tid is not None and c.cost_usd == Decimal("0.0007")
    mlflow.flush_trace_async_logging()
    trace = mlflow.get_trace(tid)
    assert trace is not None
    assert [s.name for s in trace.data.spans] == ["prepare", "llm"]
    assert trace.info.tags["member_id"] == "m-1" and "campaign_id" not in trace.info.tags
    llm_span = trace.data.spans[1]
    assert Decimal(llm_span.attributes["cost_usd"]) == Decimal("0.0007")
    assert llm_span.attributes["purpose"] == "t"


@pytest.mark.usefixtures("traced")
def test_calls_outside_a_trace_start_no_trace_and_errors_still_raise() -> None:
    with observability.span("stray", "LLM") as s:
        assert type(s).__name__ == "_NoSpan"
    with pytest.raises(ValueError, match="boom"), observability.span("x", root=True):
        raise ValueError("boom")
    mlflow.flush_trace_async_logging()
    last = mlflow.get_trace(mlflow.get_last_active_trace_id() or "")
    assert last is not None and last.info.state == "ERROR"


def test_llm_client_reports_what_the_ledger_charged() -> None:
    free = LLMClient(FakeProvider(["a"]), CostLedger(InMemoryLedgerStore(), Decimal("5")))
    assert free.complete([Message("user", "x")], max_output_tokens=5, purpose="t").cost_usd == 0
