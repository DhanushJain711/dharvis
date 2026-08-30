"""Deterministic edge-case coverage for free/busy computation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from src.calendar_service import CalendarError
from src.freebusy import (
    CalendarQueryIncompleteError,
    FreeBlock,
    ScheduleBlock,
    compute_free_blocks,
    has_conflict,
    is_free,
    merge_blocks,
    next_free_block,
    overlapping_blocks,
    query_schedule,
)


CHICAGO = ZoneInfo("America/Chicago")


def utc(hour: int, minute: int = 0, *, day: int = 10) -> datetime:
    return datetime(2026, 1, day, hour, minute, tzinfo=UTC)


def local(
    month: int,
    day: int,
    hour: int = 0,
    minute: int = 0,
) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=CHICAGO)


def constraints(
    busy=(),
    *,
    waking=("00:00", "00:00"),
    quiet=("00:00", "00:00"),
    buffer_minutes=0,
) -> dict:
    return {
        "busy_intervals": list(busy),
        "waking_hours": waking,
        "quiet_hours": quiet,
        "buffer_minutes": buffer_minutes,
        "timezone": "America/Chicago",
    }


def test_empty_calendar_returns_entire_requested_interval():
    start = utc(9)
    end = utc(18)

    assert compute_free_blocks(start, end, 30, constraints()) == [
        FreeBlock(start, end)
    ]


def test_overlapping_and_nested_busy_intervals_merge_before_subtraction():
    busy = [
        (utc(10), utc(12), "Physics lecture"),
        (utc(11), utc(13), "Office hours"),
        (utc(11, 30), utc(12, 30), "Nested meeting"),
        (utc(12, 30), utc(14), "Practice"),
    ]

    free = compute_free_blocks(utc(9), utc(18), 30, constraints(busy))

    assert free == [
        FreeBlock(utc(9), utc(10), before="Physics lecture"),
        FreeBlock(utc(14), utc(18), after="Practice"),
    ]


def test_touching_busy_intervals_are_one_continuous_blocker():
    free = compute_free_blocks(
        utc(9),
        utc(14),
        30,
        constraints(
            [
                (utc(10), utc(11), "Class"),
                (utc(11), utc(12), "Lab"),
            ]
        ),
    )

    assert free == [
        FreeBlock(utc(9), utc(10), before="Class"),
        FreeBlock(utc(12), utc(14), after="Lab"),
    ]


def test_busy_event_crossing_midnight_is_subtracted_from_overnight_waking_window():
    start = local(1, 10, 9)
    end = local(1, 11, 1)
    overnight_start = local(1, 10, 23, 30)
    overnight_end = local(1, 11, 0, 30)

    free = compute_free_blocks(
        start,
        end,
        30,
        constraints(
            [(overnight_start, overnight_end, "Late shift")],
            waking=("09:00", "01:00"),
        ),
    )

    assert free == [
        FreeBlock(start.astimezone(UTC), overnight_start.astimezone(UTC), before="Late shift"),
        FreeBlock(overnight_end.astimezone(UTC), end.astimezone(UTC), after="Late shift"),
    ]


def test_all_day_and_multi_day_events_use_local_midnight_boundaries():
    start = local(1, 9, 12)
    end = local(1, 13, 12)
    busy = [
        {
            "start": {"date": "2026-01-10"},
            "end": {"date": "2026-01-12"},
            "title": "Conference",
        },
        {
            "start": "2026-01-12",
            "end": "2026-01-13",
            "title": "Travel day",
        },
    ]

    free = compute_free_blocks(start, end, 30, constraints(busy))

    assert [(block.start, block.end) for block in free] == [
        (start.astimezone(UTC), local(1, 10).astimezone(UTC)),
        (local(1, 13).astimezone(UTC), end.astimezone(UTC)),
    ]


def test_fully_booked_range_returns_no_blocks():
    assert compute_free_blocks(
        utc(9),
        utc(18),
        15,
        constraints([(utc(8), utc(20), "On call")]),
    ) == []


def test_default_fifteen_minute_buffer_is_applied_on_both_sides():
    policy = constraints([(utc(10), utc(11), "Seminar")])
    policy.pop("buffer_minutes")

    assert compute_free_blocks(utc(9), utc(13), 30, policy) == [
        FreeBlock(utc(9), utc(9, 45), before="Seminar"),
        FreeBlock(utc(11, 15), utc(13), after="Seminar"),
    ]


def test_minimum_duration_filters_short_gaps_after_buffering():
    free = compute_free_blocks(
        utc(9),
        utc(13),
        45,
        constraints(
            [(utc(10), utc(11), "Class"), (utc(12), utc(13), "Lunch")],
            buffer_minutes=15,
        ),
    )

    assert free == [FreeBlock(utc(9), utc(9, 45), before="Class")]


def test_overnight_waking_hours_and_quiet_hours_are_intersected_per_local_day():
    start = local(1, 10)
    end = local(1, 12)

    free = compute_free_blocks(
        start,
        end,
        60,
        constraints(
            waking=("09:00", "01:00"),
            quiet=("22:00", "07:00"),
        ),
    )

    assert free == [
        FreeBlock(
            local(1, 10, 9).astimezone(UTC),
            local(1, 10, 22).astimezone(UTC),
            after="waking hours begin",
            before="quiet hours",
        ),
        FreeBlock(
            local(1, 11, 9).astimezone(UTC),
            local(1, 11, 22).astimezone(UTC),
            after="waking hours begin",
            before="quiet hours",
        ),
    ]


@pytest.mark.parametrize(
    ("start", "end", "expected_hours"),
    [
        (local(3, 8), local(3, 9), 23),
        (local(11, 1), local(11, 2), 25),
    ],
    ids=["spring-forward", "fall-back"],
)
def test_full_local_day_has_real_dst_duration(start: datetime, end: datetime, expected_hours: int):
    free = compute_free_blocks(start, end, 30, constraints())

    assert len(free) == 1
    assert free[0].start == start.astimezone(UTC)
    assert free[0].end == end.astimezone(UTC)
    assert free[0].end - free[0].start == timedelta(hours=expected_hours)


def test_annotations_name_the_events_bounding_each_gap():
    free = compute_free_blocks(
        utc(8),
        utc(18),
        30,
        constraints(
            [
                (utc(9), utc(10), "Physics lecture"),
                (utc(12), utc(13), "Lunch"),
                (utc(16), utc(17), "Practice"),
            ]
        ),
    )

    assert [(block.after, block.before) for block in free] == [
        (None, "Physics lecture"),
        ("Physics lecture", "Lunch"),
        ("Lunch", "Practice"),
        ("Practice", None),
    ]
    assert free[1].after_title == "Physics lecture"
    assert free[1].before_title == "Lunch"


def test_is_free_respects_busy_waking_quiet_and_buffer_constraints():
    policy = constraints([(utc(10), utc(11), "Class")], buffer_minutes=15)

    assert is_free(utc(9), utc(9, 45), policy) is True
    assert is_free(utc(9, 50), utc(10), policy) is False
    assert is_free(utc(11), utc(11, 10), policy) is False
    assert is_free(utc(11, 15), utc(12), policy) is True


def test_next_free_block_returns_earliest_suitable_gap_and_none_when_full():
    policy = constraints(
        [(utc(9), utc(10), "Class"), (utc(11), utc(12), "Practice")]
    )

    assert next_free_block(utc(9), 30, policy, search_end=utc(13)) == FreeBlock(
        utc(10), utc(11), after="Class", before="Practice"
    )
    assert next_free_block(
        utc(9),
        30,
        constraints([(utc(8), utc(14), "Booked")]),
        search_end=utc(13),
    ) is None


def test_merge_blocks_handles_overlap_nesting_and_touching_and_keeps_contributors():
    blocks = [
        ScheduleBlock(utc(10), utc(12), "A", "gcal", "a", {}),
        ScheduleBlock(utc(11), utc(11, 30), "Nested", "event", "b", {}),
        ScheduleBlock(utc(12), utc(13), "Touching", "task", "c", {}),
    ]

    merged = merge_blocks(blocks)

    assert len(merged) == 1
    assert (merged[0].start, merged[0].end) == (utc(10), utc(13))
    assert merged[0].metadata["titles"] == ["A", "Nested", "Touching"]
    assert len(merged[0].metadata["merged_blocks"]) == 3


@pytest.mark.asyncio
async def test_query_schedule_aggregates_google_local_events_and_scheduled_tasks():
    calendar = AsyncMock()
    calendar._last_query_complete = True
    calendar.list_events.return_value = [
        {
            "id": "g-1",
            "title": "Lecture",
            "start_time": utc(10).isoformat(),
            "end_time": utc(11).isoformat(),
        }
    ]
    store = AsyncMock()
    store.query_events.return_value = [
        {
            "id": 2,
            "title": "Dentist",
            "start_time": utc(12),
            "end_time": utc(13),
        }
    ]
    store.query_tasks.return_value = [
        {
            "id": 3,
            "title": "Pset",
            "scheduled_start": utc(14).isoformat(),
            "scheduled_end": utc(15).isoformat(),
        },
        {"id": 4, "title": "Unscheduled", "scheduled_start": None, "scheduled_end": None},
        {
            "id": 5,
            "title": "Outside",
            "scheduled_start": utc(19).isoformat(),
            "scheduled_end": utc(20).isoformat(),
        },
    ]

    blocks = await query_schedule(store, calendar, utc(9), utc(18))

    assert [(block.source, block.source_id, block.title) for block in blocks] == [
        ("gcal", "g-1", "Lecture"),
        ("event", "2", "Dentist"),
        ("task", "3", "Pset"),
    ]
    calendar.list_events.assert_awaited_once_with(utc(9), utc(18))
    store.query_events.assert_awaited_once_with(utc(9), utc(18))


@pytest.mark.asyncio
async def test_query_schedule_deduplicates_task_backed_google_work_block():
    calendar = AsyncMock()
    calendar._last_query_complete = True
    calendar.list_events.return_value = [
        {
            "id": "gcal-block",
            "gcal_event_id": "gcal-block",
            "title": "Math pset",
            "start_time": utc(14).isoformat(),
            "end_time": utc(15).isoformat(),
        }
    ]
    store = AsyncMock()
    store.query_events.return_value = []
    store.query_tasks.return_value = [
        {
            "id": 7,
            "gcal_event_id": "gcal-block",
            "title": "Math pset",
            "scheduled_start": utc(14).isoformat(),
            "scheduled_end": utc(15).isoformat(),
        }
    ]

    blocks = await query_schedule(store, calendar, utc(9), utc(18))

    assert len(blocks) == 1


@pytest.mark.asyncio
async def test_query_schedule_forwards_force_refresh_to_calendar():
    calendar = AsyncMock()
    calendar._last_query_complete = True
    calendar.list_events.return_value = []
    store = AsyncMock()
    store.query_events.return_value = []
    store.query_tasks.return_value = []

    await query_schedule(store, calendar, utc(9), utc(18), force_refresh=True)

    calendar.list_events.assert_awaited_once_with(
        utc(9), utc(18), force_refresh=True
    )


@pytest.mark.asyncio
async def test_query_schedule_fails_closed_when_calendar_read_is_incomplete():
    calendar = AsyncMock()
    calendar._last_query_complete = False
    calendar.list_events.return_value = []
    store = AsyncMock()

    with pytest.raises(CalendarQueryIncompleteError, match="incomplete"):
        await query_schedule(store, calendar, utc(9), utc(18))
    store.query_events.assert_not_awaited()
    store.query_tasks.assert_not_awaited()


@pytest.mark.asyncio
async def test_query_schedule_propagates_calendar_boundary_failure():
    calendar = AsyncMock()
    calendar.list_events.side_effect = CalendarError("unavailable")
    store = AsyncMock()

    with pytest.raises(CalendarError, match="unavailable"):
        await query_schedule(store, calendar, utc(9), utc(18))


def test_has_conflict_uses_half_open_interval_semantics():
    blocks = [ScheduleBlock(utc(10), utc(11), "Class", "gcal", "1", {})]

    assert has_conflict(blocks, utc(9, 30), utc(10)) is False
    assert has_conflict(blocks, utc(11), utc(12)) is False
    assert has_conflict(blocks, utc(10, 30), utc(11, 30)) is True


def test_overlapping_blocks_returns_conflict_details_in_time_order():
    blocks = [
        ScheduleBlock(utc(11), utc(12), "Later", "event", "2", {}),
        ScheduleBlock(utc(9), utc(10), "Touching", "gcal", "1", {}),
        ScheduleBlock(utc(10), utc(11), "Class", "gcal", "3", {}),
    ]

    assert overlapping_blocks(blocks, utc(10), utc(11, 30)) == [
        blocks[2],
        blocks[0],
    ]


@pytest.mark.parametrize(
    "call",
    [
        lambda: compute_free_blocks(datetime(2026, 1, 1), utc(10), 30, constraints()),
        lambda: compute_free_blocks(utc(10), utc(9), 30, constraints()),
        lambda: compute_free_blocks(utc(9), utc(10), 0, constraints()),
        lambda: next_free_block(utc(9), 30, constraints(), search_end=utc(9)),
    ],
)
def test_invalid_ranges_and_naive_datetimes_are_rejected(call):
    with pytest.raises(ValueError):
        call()
