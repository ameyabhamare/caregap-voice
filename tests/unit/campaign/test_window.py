from __future__ import annotations

from datetime import UTC, datetime, time
from zoneinfo import ZoneInfo

import pytest

from caregap.campaign.clock import FixedClock, ShiftedClock, build_clock
from caregap.campaign.window import CallWindow
from caregap.outreach.profile import DEFAULT_PROFILE, CampaignPolicy

NY = ZoneInfo("America/New_York")
W = CallWindow(DEFAULT_PROFILE.campaign)  # Mon-Sat 09:00-20:00 New York


def ny(y: int, mo: int, d: int, h: int = 0, mi: int = 0, s: int = 0) -> datetime:
    return datetime(y, mo, d, h, mi, s, tzinfo=NY)


@pytest.mark.parametrize(
    ("at", "is_open"),
    [
        (ny(2026, 1, 13, 9, 0), True),  # Tuesday, opening minute
        (ny(2026, 1, 13, 8, 59, 59), False),
        (ny(2026, 1, 13, 19, 59, 59), True),
        (ny(2026, 1, 13, 20, 0), False),  # end is exclusive
        (ny(2026, 1, 17, 12, 0), True),  # Saturday
        (ny(2026, 1, 18, 12, 0), False),  # Sunday
    ],
)
def test_is_open(at: datetime, is_open: bool) -> None:
    assert W.is_open(at) is is_open
    assert W.is_open(at.astimezone(UTC)) is is_open  # same instant, any zone


def test_next_open() -> None:
    assert W.next_open(ny(2026, 1, 13, 10, 0)) == ny(2026, 1, 13, 10, 0)
    assert W.next_open(ny(2026, 1, 13, 7, 0)) == ny(2026, 1, 13, 9, 0)  # later today
    assert W.next_open(ny(2026, 1, 13, 21, 0)) == ny(2026, 1, 14, 9, 0)  # tomorrow
    assert W.next_open(ny(2026, 1, 17, 20, 30)) == ny(2026, 1, 19, 9, 0)  # skips Sunday


def test_next_open_keeps_the_callers_zone() -> None:
    at = ny(2026, 1, 13, 21, 0).astimezone(UTC)
    nxt = W.next_open(at)
    assert nxt.tzinfo is UTC
    assert nxt == ny(2026, 1, 14, 9, 0)


def test_window_follows_local_time_across_dst() -> None:
    # 2026-03-08: clocks go forward in New York; 09:00 local is 13:00 UTC, not 14:00.
    assert W.next_open(ny(2026, 3, 7, 21, 0)) == datetime(2026, 3, 9, 13, 0, tzinfo=UTC)
    assert W.next_open(ny(2026, 3, 6, 21, 0)) == datetime(2026, 3, 7, 14, 0, tzinfo=UTC)


def test_policy_validation() -> None:
    base = DEFAULT_PROFILE.campaign.model_dump()
    with pytest.raises(ValueError, match="before"):
        CampaignPolicy(**{**base, "call_window_start": time(20), "call_window_end": time(9)})
    with pytest.raises(ValueError, match="weekdays"):
        CampaignPolicy(**{**base, "call_days": frozenset({0})})


def test_clocks() -> None:
    start = ny(2026, 1, 13, 10, 0)
    shifted = ShiftedClock(start)
    assert 0 <= (shifted.now() - start).total_seconds() < 5
    assert shifted.now().tzinfo is UTC
    assert isinstance(build_clock(None).now(), datetime)
    with pytest.raises(ValueError, match="offset"):
        ShiftedClock(datetime(2026, 1, 13, 10))  # noqa: DTZ001
    fixed = FixedClock(start)
    fixed.advance(minutes=5)
    assert fixed.now() == ny(2026, 1, 13, 10, 5)
