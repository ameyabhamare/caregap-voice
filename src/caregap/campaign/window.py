"""Call windows in the member's local time."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from caregap.outreach.profile import CampaignPolicy


class CallWindow:
    """Calls are allowed on ``call_days`` from ``call_window_start`` (inclusive) to
    ``call_window_end`` (exclusive), local time. Wall-clock times, so DST is handled by
    the time zone database."""

    def __init__(self, policy: CampaignPolicy) -> None:
        self.tz = ZoneInfo(policy.timezone)
        self.start = policy.call_window_start
        self.end = policy.call_window_end
        self.days = policy.call_days

    def is_open(self, at: datetime) -> bool:
        local = at.astimezone(self.tz)
        return local.isoweekday() in self.days and self.start <= local.time() < self.end

    def next_open(self, at: datetime) -> datetime:
        """``at`` if the window is open then, else the next time it opens."""
        if self.is_open(at):
            return at
        local = at.astimezone(self.tz)
        day = local.date()
        if local.time() >= self.start:
            day += timedelta(days=1)
        for _ in range(7):
            if day.isoweekday() in self.days:
                return datetime.combine(day, self.start, self.tz).astimezone(at.tzinfo)
            day += timedelta(days=1)
        raise AssertionError("unreachable: call_days is validated non-empty")
