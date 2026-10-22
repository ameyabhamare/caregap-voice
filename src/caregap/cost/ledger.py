"""Cost ledger with a hard budget cap.

Every runtime model call goes through ``CostLedger.guard``:

1. ``reserve`` holds the call's worst-case cost (prompt tokens plus ``max_output_tokens``)
   against the budget. It refuses with ``BudgetExceededError`` if settled spend plus
   outstanding reservations plus this estimate would exceed the cap.
2. ``settle`` replaces the estimate with the actual cost from the provider's usage report.
3. If the call fails without reporting usage, the estimate is kept as the charge: we cannot
   tell whether the provider billed it, and a hard cap must overcount, never undercount.
   Callers that know a request never left the process can ``release`` it instead.

Actual cost can exceed the estimate if the prompt-token estimate was low. Committed spend
may then end slightly above the cap; every later paid call is refused.

Local providers cost $0 and are always allowed, even once the cap is hit, so the default
local stack keeps working. They are still recorded, which keeps the ledger a full call log.

The check and the insert happen atomically in the store, so concurrent workers cannot
overspend between them.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import psycopg

from caregap.cost.errors import BudgetExceededError
from caregap.cost.pricing import cost_usd, is_free, price_for


@dataclass(frozen=True)
class Reservation:
    id: int
    provider: str
    model: str
    estimated_usd: Decimal


@dataclass
class Usage:
    """Filled in by the caller inside ``CostLedger.guard`` once the provider reports usage."""

    input_tokens: int | None = None
    output_tokens: int | None = None


class LedgerStore(Protocol):
    def try_reserve(
        self, *, provider: str, model: str, purpose: str, estimated_usd: Decimal, cap: Decimal
    ) -> int | None:
        """Atomically insert a reservation if it fits under ``cap``.

        Returns the new entry id, or None if it would exceed the cap.
        """

    def settle(
        self,
        entry_id: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_usd: Decimal,
    ) -> None: ...

    def release(self, entry_id: int) -> None: ...

    def committed_usd(self) -> Decimal:
        """Settled spend plus outstanding reservations."""


class CostLedger:
    def __init__(self, store: LedgerStore, cap_usd: Decimal) -> None:
        if cap_usd < 0:
            raise ValueError("budget cap must be non-negative")
        self._store = store
        self.cap_usd = cap_usd

    def committed_usd(self) -> Decimal:
        return self._store.committed_usd()

    def remaining_usd(self) -> Decimal:
        return max(Decimal(0), self.cap_usd - self.committed_usd())

    def reserve(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        max_output_tokens: int,
        purpose: str,
    ) -> Reservation:
        if is_free(provider):
            estimate = Decimal(0)
        else:
            # Raises UnknownModelPriceError for unpriced paid models: fail closed.
            estimate = price_for(provider, model).cost(input_tokens, max_output_tokens)
        # Free calls get an unlimited cap so they never get blocked.
        cap = self.cap_usd if estimate > 0 else Decimal("Infinity")
        entry_id = self._store.try_reserve(
            provider=provider, model=model, purpose=purpose, estimated_usd=estimate, cap=cap
        )
        if entry_id is None:
            raise BudgetExceededError(
                cap=self.cap_usd, committed=self.committed_usd(), requested=estimate
            )
        return Reservation(entry_id, provider, model, estimate)

    def settle(self, reservation: Reservation, *, input_tokens: int, output_tokens: int) -> Decimal:
        actual = cost_usd(reservation.provider, reservation.model, input_tokens, output_tokens)
        self._store.settle(
            reservation.id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            actual_usd=actual,
        )
        return actual

    def release(self, reservation: Reservation) -> None:
        self._store.release(reservation.id)

    @contextmanager
    def guard(
        self,
        *,
        provider: str,
        model: str,
        input_tokens: int,
        max_output_tokens: int,
        purpose: str,
    ) -> Iterator[Usage]:
        """Reserve before the call and settle after it.

        The caller must fill in the yielded ``Usage``. If the block raises before usage is
        reported, the worst-case estimate is charged.
        """
        reservation = self.reserve(
            provider=provider,
            model=model,
            input_tokens=input_tokens,
            max_output_tokens=max_output_tokens,
            purpose=purpose,
        )
        usage = Usage()
        try:
            yield usage
        except BaseException:
            if usage.input_tokens is not None and usage.output_tokens is not None:
                self.settle(
                    reservation, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
                )
            else:
                self._store.settle(
                    reservation.id,
                    input_tokens=None,
                    output_tokens=None,
                    actual_usd=reservation.estimated_usd,
                )
            raise
        if usage.input_tokens is None or usage.output_tokens is None:
            self._store.settle(
                reservation.id,
                input_tokens=None,
                output_tokens=None,
                actual_usd=reservation.estimated_usd,
            )
            raise RuntimeError("CostLedger.guard block finished without reporting usage")
        self.settle(reservation, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens)


@dataclass
class _Entry:
    estimated_usd: Decimal
    status: str = "reserved"
    actual_usd: Decimal | None = None


@dataclass
class InMemoryLedgerStore:
    """Process-local store for unit tests and offline tools."""

    _entries: dict[int, _Entry] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_reserve(
        self, *, provider: str, model: str, purpose: str, estimated_usd: Decimal, cap: Decimal
    ) -> int | None:
        with self._lock:
            if self._committed() + estimated_usd > cap:
                return None
            entry_id = len(self._entries) + 1
            self._entries[entry_id] = _Entry(estimated_usd)
            return entry_id

    def settle(
        self,
        entry_id: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_usd: Decimal,
    ) -> None:
        with self._lock:
            entry = self._reserved(entry_id)
            entry.status, entry.actual_usd = "settled", actual_usd

    def release(self, entry_id: int) -> None:
        with self._lock:
            self._reserved(entry_id).status = "released"

    def committed_usd(self) -> Decimal:
        with self._lock:
            return self._committed()

    def _reserved(self, entry_id: int) -> _Entry:
        entry = self._entries[entry_id]
        if entry.status != "reserved":
            raise ValueError(f"ledger entry {entry_id} is not an open reservation ({entry.status})")
        return entry

    def _committed(self) -> Decimal:
        total = Decimal(0)
        for e in self._entries.values():
            if e.status == "settled" and e.actual_usd is not None:
                total += e.actual_usd
            elif e.status == "reserved":
                total += e.estimated_usd
        return total


# Arbitrary constant key for the transaction-scoped advisory lock that serializes reservations.
_LEDGER_LOCK_KEY = 0x6361_7265_6761_70  # "caregap"

_COMMITTED_SQL = b"""
    SELECT COALESCE(SUM(CASE status
        WHEN 'settled' THEN actual_usd
        WHEN 'reserved' THEN estimated_usd
        ELSE 0 END), 0)
    FROM cost_ledger
"""


class PostgresLedgerStore:
    """Ledger store backed by the ``cost_ledger`` table (see caregap/db/schema.sql)."""

    def __init__(self, conninfo: str) -> None:
        self._conninfo = conninfo

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._conninfo)

    def try_reserve(
        self, *, provider: str, model: str, purpose: str, estimated_usd: Decimal, cap: Decimal
    ) -> int | None:
        with self._connect() as conn, conn.transaction():
            conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LEDGER_LOCK_KEY,))
            row = conn.execute(_COMMITTED_SQL).fetchone()
            committed = Decimal(row[0]) if row else Decimal(0)
            if committed + estimated_usd > cap:
                return None
            inserted = conn.execute(
                """
                INSERT INTO cost_ledger (provider, model, purpose, status, estimated_usd)
                VALUES (%s, %s, %s, 'reserved', %s)
                RETURNING id
                """,
                (provider, model, purpose, estimated_usd),
            ).fetchone()
            if inserted is None:
                raise RuntimeError("cost_ledger insert returned no id")
            return int(inserted[0])

    def settle(
        self,
        entry_id: int,
        *,
        input_tokens: int | None,
        output_tokens: int | None,
        actual_usd: Decimal,
    ) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE cost_ledger
                SET status = 'settled', settled_at = now(), actual_usd = %s,
                    input_tokens = %s, output_tokens = %s
                WHERE id = %s AND status = 'reserved'
                """,
                (actual_usd, input_tokens, output_tokens, entry_id),
            )
            if cur.rowcount != 1:
                raise ValueError(f"ledger entry {entry_id} is not an open reservation")

    def release(self, entry_id: int) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "UPDATE cost_ledger SET status = 'released', settled_at = now() "
                "WHERE id = %s AND status = 'reserved'",
                (entry_id,),
            )
            if cur.rowcount != 1:
                raise ValueError(f"ledger entry {entry_id} is not an open reservation")

    def committed_usd(self) -> Decimal:
        with self._connect() as conn:
            row = conn.execute(_COMMITTED_SQL).fetchone()
            return Decimal(row[0]) if row else Decimal(0)
