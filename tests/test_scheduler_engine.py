from datetime import UTC, datetime, timedelta

import pytest

from src.scheduler_engine import SchedulerEngine


class _Store:
    async def query_events(self, start, end):
        return []

    async def query_tasks(self, *args, **kwargs):
        return []


class _CachedCalendar:
    """Simulate a stale ordinary read and a fresh upstream event."""

    _last_query_complete = True

    def __init__(self, event):
        self.event = event
        self.force_refreshes = 0

    async def list_events(self, start, end, *, force_refresh=False):
        if force_refresh:
            self.force_refreshes += 1
            return [self.event]
        return []


@pytest.mark.asyncio
async def test_prewrite_availability_forces_calendar_refresh() -> None:
    start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(hours=2)
    end = start + timedelta(minutes=60)
    calendar = _CachedCalendar({
        "id": "upstream-event", "title": "New meeting",
        "start_time": start + timedelta(minutes=15),
        "end_time": end - timedelta(minutes=15),
    })
    engine = SchedulerEngine(_Store(), calendar, client=object())

    assert await engine._actual_free(123, start, end) is False
    assert calendar.force_refreshes == 1
