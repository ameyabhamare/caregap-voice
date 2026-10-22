"""Ledger contract tests. Each runs against the in-memory and the Postgres store."""

from decimal import Decimal
from typing import TypedDict

import pytest

from caregap.cost import BudgetExceededError, CostLedger, LedgerStore, UnknownModelPriceError


class Model(TypedDict):
    provider: str
    model: str


class SizedCall(Model):
    input_tokens: int
    max_output_tokens: int


SONNET = Model(provider="claude", model="claude-sonnet-5-5")
# 1M input tokens and 0 output tokens on Sonnet 5.5 cost exactly $2.
TWO_DOLLARS = SizedCall(**SONNET, input_tokens=1_000_000, max_output_tokens=0)


def test_reserve_holds_worst_case_estimate(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    r = ledger.reserve(**SONNET, input_tokens=1_000, max_output_tokens=500, purpose="t")
    assert r.estimated_usd == Decimal("0.007")
    assert ledger.committed_usd() == Decimal("0.007")


def test_settle_replaces_estimate_with_actual(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    r = ledger.reserve(**SONNET, input_tokens=1_000, max_output_tokens=1_000, purpose="t")
    actual = ledger.settle(r, input_tokens=1_000, output_tokens=100)
    assert actual == Decimal("0.003")
    assert ledger.committed_usd() == Decimal("0.003")


def test_release_frees_the_hold(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    r = ledger.reserve(**TWO_DOLLARS, purpose="t")
    ledger.release(r)
    assert ledger.committed_usd() == 0


def test_refuses_call_that_would_exceed_cap(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    ledger.reserve(**TWO_DOLLARS, purpose="t")
    ledger.reserve(**TWO_DOLLARS, purpose="t")
    with pytest.raises(BudgetExceededError) as exc:
        ledger.reserve(**TWO_DOLLARS, purpose="t")
    assert exc.value.committed == Decimal("4")
    assert exc.value.requested == Decimal("2")
    # The refused call left no trace in committed spend.
    assert ledger.committed_usd() == Decimal("4")


def test_call_landing_exactly_on_cap_is_allowed(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("4"))
    ledger.reserve(**TWO_DOLLARS, purpose="t")
    ledger.reserve(**TWO_DOLLARS, purpose="t")
    assert ledger.remaining_usd() == 0


def test_outstanding_reservations_count_against_cap(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("3"))
    ledger.reserve(**TWO_DOLLARS, purpose="in flight")
    with pytest.raises(BudgetExceededError):
        ledger.reserve(**TWO_DOLLARS, purpose="t")


def test_local_calls_allowed_after_cap_is_hit(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("2"))
    ledger.reserve(**TWO_DOLLARS, purpose="t")
    r = ledger.reserve(
        provider="ollama",
        model="qwen2.5:3b",
        input_tokens=10**6,
        max_output_tokens=10**6,
        purpose="t",
    )
    assert r.estimated_usd == 0
    assert ledger.settle(r, input_tokens=10, output_tokens=10) == 0


def test_zero_cap_blocks_all_paid_calls(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("0"))
    with pytest.raises(BudgetExceededError):
        ledger.reserve(**SONNET, input_tokens=1, max_output_tokens=0, purpose="t")


def test_unpriced_paid_model_is_refused_before_reserving(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    with pytest.raises(UnknownModelPriceError):
        ledger.reserve(
            provider="claude",
            model="claude-unknown",
            input_tokens=1,
            max_output_tokens=1,
            purpose="t",
        )
    assert ledger.committed_usd() == 0


def test_cannot_settle_twice(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    r = ledger.reserve(**TWO_DOLLARS, purpose="t")
    ledger.settle(r, input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError, match="reserv"):
        ledger.settle(r, input_tokens=1, output_tokens=1)
    with pytest.raises(ValueError, match="reserv"):
        ledger.release(r)


def test_guard_settles_reported_usage(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    with ledger.guard(**SONNET, input_tokens=1_000, max_output_tokens=1_000, purpose="t") as usage:
        usage.input_tokens, usage.output_tokens = 1_000, 100
    assert ledger.committed_usd() == Decimal("0.003")


def test_guard_charges_estimate_when_call_fails_without_usage(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    with (
        pytest.raises(ConnectionError),
        ledger.guard(**SONNET, input_tokens=1_000, max_output_tokens=500, purpose="t"),
    ):
        raise ConnectionError("timed out")
    assert ledger.committed_usd() == Decimal("0.007")


def test_guard_settles_actual_when_failure_follows_usage(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    with (
        pytest.raises(ValueError, match="parse"),
        ledger.guard(**SONNET, input_tokens=1_000, max_output_tokens=1_000, purpose="t") as usage,
    ):
        usage.input_tokens, usage.output_tokens = 1_000, 100
        raise ValueError("could not parse model output")
    assert ledger.committed_usd() == Decimal("0.003")


def test_guard_requires_usage(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("5"))
    with (
        pytest.raises(RuntimeError, match="without reporting usage"),
        ledger.guard(**SONNET, input_tokens=1_000, max_output_tokens=500, purpose="t"),
    ):
        pass
    assert ledger.committed_usd() == Decimal("0.007")


def test_guard_refuses_before_running_block(store: LedgerStore) -> None:
    ledger = CostLedger(store, Decimal("1"))
    ran = False
    with pytest.raises(BudgetExceededError), ledger.guard(**TWO_DOLLARS, purpose="t"):
        ran = True
    assert not ran


def test_negative_cap_rejected(store: LedgerStore) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CostLedger(store, Decimal("-1"))
