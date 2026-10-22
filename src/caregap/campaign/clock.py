"""The service's notion of "now".

``CAMPAIGN_CLOCK`` (ISO datetime with offset) starts the clock at a fixed instant and then
lets it run in real time. The demo uses it so a campaign runs inside the call window
whatever the real time is, while retry delays still elapse normally.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class ShiftedClock:
    def __init__(self, start: datetime) -> None:
        if start.tzinfo is None:
            raise ValueError("CAMPAIGN_CLOCK needs a UTC offset, e.g. 2026-01-13T10:00:00-05:00")
        self.start = start
        self._t0 = time.monotonic()

    def now(self) -> datetime:
        return (self.start + timedelta(seconds=time.monotonic() - self._t0)).astimezone(UTC)


class FixedClock:
    """For tests: time moves only when told to."""

    def __init__(self, at: datetime) -> None:
        self.at = at

    def now(self) -> datetime:
        return self.at

    def advance(self, **kwargs: float) -> None:
        self.at += timedelta(**kwargs)


def build_clock(start: datetime | None) -> Clock:
    return ShiftedClock(start) if start else SystemClock()
