"""Postgres-only ledger behaviour: concurrent reservations must never overspend."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from caregap.cost import BudgetExceededError, CostLedger, PostgresLedgerStore

pytestmark = pytest.mark.integration


def test_concurrent_reservations_never_exceed_cap(pg_store: PostgresLedgerStore) -> None:
    # 40 workers each try to reserve $1 against a $5 cap, each on its own connection.
    ledger = CostLedger(pg_store, Decimal("5"))

    def attempt(_: int) -> bool:
        try:
            ledger.reserve(
                provider="claude",
                model="claude-sonnet-5-5",
                input_tokens=500_000,
                max_output_tokens=0,
                purpose="race",
            )
        except BudgetExceededError:
            return False
        return True

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(attempt, range(40)))

    assert sum(results) == 5
    assert ledger.committed_usd() == Decimal("5")


def test_ledger_survives_new_store_instance(pg_store: PostgresLedgerStore, pg_url: str) -> None:
    # Spend is durable: a fresh process sees what earlier runs committed.
    CostLedger(pg_store, Decimal("5")).reserve(
        provider="claude",
        model="claude-sonnet-5-5",
        input_tokens=1_000_000,
        max_output_tokens=0,
        purpose="t",
    )
    assert CostLedger(PostgresLedgerStore(pg_url), Decimal("5")).committed_usd() == Decimal("2")
