"""Cost ledger errors."""

from __future__ import annotations

from decimal import Decimal


class BudgetExceededError(RuntimeError):
    """Raised when a paid call would push committed spend over the budget cap."""

    def __init__(self, *, cap: Decimal, committed: Decimal, requested: Decimal) -> None:
        self.cap = cap
        self.committed = committed
        self.requested = requested
        super().__init__(
            f"budget cap ${cap} would be exceeded: committed ${committed}, requested ${requested}"
        )


class UnknownModelPriceError(KeyError):
    """Raised for a paid provider/model with no price entry. Fails closed: no price, no call."""
