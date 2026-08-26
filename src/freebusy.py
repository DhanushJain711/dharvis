"""Deterministic schedule merging and timezone-safe free-time computation."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Literal, Mapping, Sequence
from zoneinfo import ZoneInfo

from .calendar_service import CalendarError, CalendarService
from .config import config
from .store import Store


@dataclass(frozen=True, slots=True)
class ScheduleBlock:
    """One occupied interval from any schedule source."""

    start: datetime
    end: datetime
    title: str
    source: Literal["gcal", "event", "task"]
    source_id: str
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FreeBlock:
    """One available aware UTC half-open interval with boundary context."""

    start: datetime
    end: datetime
    after: str | None = None
    before: str | None = None

    @property
    def after_title(self) -> str | None:
        """Compatibility spelling for the lower-bound annotation."""
        return self.after

    @property
    def before_title(self) -> str | None:
        """Compatibility spelling for the upper-bound annotation."""
        return self.before


class CalendarQueryIncompleteError(CalendarError):
    """A calendar read was partial, so availability cannot be determined safely."""


@dataclass(frozen=True, slots=True)
class _Blocker:
    start: datetime
    end: datetime
    start_title: str
    end_title: str


def _to_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are not allowed")
    return value.astimezone(UTC)


def _validate_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start_utc = _to_utc(start, "start")
    end_utc = _to_utc(end, "end")
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    return start_utc, end_utc


def _parse_datetime(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _to_utc(value, name)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
        return _to_utc(parsed, name)
    raise ValueError(f"{name} must be a timezone-aware datetime")


def _setting(constraints: Any, key: str, default: Any = None) -> Any:
    if isinstance(constraints, Mapping):
        return constraints.get(key, default)
    return getattr(constraints, key, default) if constraints is not None else default


def _parse_clock(value: str | time, name: str) -> time:
    if isinstance(value, time):
        return value.replace(tzinfo=None)
    if not isinstance(value, str):
        raise ValueError(f"{name} must be HH:MM")
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be HH:MM") from exc


def _clock_range(
    value: Any,
    *,
    default_start: str,
    default_end: str,
    prefix: str,
    constraints: Any,
) -> tuple[time, time]:
    if value is None:
        raw_start = _setting(constraints, f"{prefix}_start", default_start)
        raw_end = _setting(constraints, f"{prefix}_end", default_end)
    elif isinstance(value, Mapping):
        raw_start = value.get("start", value.get("start_time", default_start))
        raw_end = value.get("end", value.get("end_time", default_end))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) == 2:
        raw_start, raw_end = value
    else:
        raw_start = getattr(value, "start", default_start)
        raw_end = getattr(value, "end", default_end)
    return _parse_clock(raw_start, f"{prefix}_start"), _parse_clock(
        raw_end, f"{prefix}_end"
    )


def _local_interval(local_date: date, start: time, end: time, zone: ZoneInfo) -> tuple[datetime, datetime]:
    """Build a wall-clock interval; UTC conversion gives DST days their real length."""
    local_start = datetime.combine(local_date, start, tzinfo=zone)
    if start == end:
        end_date = local_date + timedelta(days=1)
    else:
        end_date = local_date + (timedelta(days=1) if end < start else timedelta())
    local_end = datetime.combine(end_date, end, tzinfo=zone)
    return local_start.astimezone(UTC), local_end.astimezone(UTC)


def _dates_covering(start: datetime, end: datetime, zone: ZoneInfo) -> list[date]:
    first = start.astimezone(zone).date() - timedelta(days=1)
    last = end.astimezone(zone).date()
    days: list[date] = []
    current = first
    while current <= last:
        days.append(current)
        current += timedelta(days=1)
    return days


def _event_datetime(value: Any, name: str, zone: ZoneInfo) -> datetime:
    if isinstance(value, Mapping):
        if value.get("dateTime"):
            return _parse_datetime(value["dateTime"], name)
        if value.get("date"):
            value = value["date"]
    if isinstance(value, date) and not isinstance(value, datetime):
        return datetime.combine(value, time.min, tzinfo=zone).astimezone(UTC)
    if isinstance(value, str) and "T" not in value:
        try:
            local_date = date.fromisoformat(value)
        except ValueError:
            pass
        else:
            return datetime.combine(local_date, time.min, tzinfo=zone).astimezone(UTC)
    return _parse_datetime(value, name)


def _as_blocker(value: Any, zone: ZoneInfo, index: int) -> _Blocker:
    if isinstance(value, ScheduleBlock):
        start, end = _validate_range(value.start, value.end)
        title = value.title
    elif isinstance(value, Mapping):
        raw_start = value.get("start", value.get("start_time"))
        raw_end = value.get("end", value.get("end_time"))
        start = _event_datetime(raw_start, f"busy_intervals[{index}].start", zone)
        end = _event_datetime(raw_end, f"busy_intervals[{index}].end", zone)
        if end <= start:
            raise ValueError(f"busy_intervals[{index}] end must be later than start")
        title = str(value.get("title", value.get("summary", "busy")))
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) not in (2, 3):
            raise ValueError("busy interval tuples require start, end, and optional title")
        start = _event_datetime(value[0], f"busy_intervals[{index}].start", zone)
        end = _event_datetime(value[1], f"busy_intervals[{index}].end", zone)
        if end <= start:
            raise ValueError(f"busy_intervals[{index}] end must be later than start")
        title = str(value[2]) if len(value) == 3 else "busy"
    else:
        start = _event_datetime(getattr(value, "start"), f"busy_intervals[{index}].start", zone)
        end = _event_datetime(getattr(value, "end"), f"busy_intervals[{index}].end", zone)
        if end <= start:
            raise ValueError(f"busy_intervals[{index}] end must be later than start")
        title = str(getattr(value, "title", "busy"))
    return _Blocker(start, end, title, title)


def _merge_blockers(blockers: list[_Blocker]) -> list[_Blocker]:
    if not blockers:
        return []
    ordered = sorted(blockers, key=lambda block: (block.start, block.end))
    merged = [ordered[0]]
    for block in ordered[1:]:
        previous = merged[-1]
        if block.start <= previous.end:
            if block.end >= previous.end:
                end = block.end
                end_title = block.end_title
            else:
                end = previous.end
                end_title = previous.end_title
            merged[-1] = _Blocker(
                previous.start,
                end,
                previous.start_title,
                end_title,
            )
        else:
            merged.append(block)
    return merged


def compute_free_blocks(
    start: datetime,
    end: datetime,
    min_minutes: int,
    constraints: Any,
) -> list[FreeBlock]:
    """Compute free intervals from busy, waking, quiet, and buffer constraints.

    ``constraints`` may be a mapping or an object. Busy intervals are accepted
    under ``busy_intervals``, ``busy_blocks``, or ``busy``. Waking/quiet hours
    accept ``{"start": "09:00", "end": "01:00"}`` or a two-item tuple.
    """
    range_start, range_end = _validate_range(start, end)
    if not isinstance(min_minutes, int) or isinstance(min_minutes, bool) or min_minutes <= 0:
        raise ValueError("min_minutes must be a positive integer")
    buffer_minutes = _setting(constraints, "buffer_minutes", 15)
    if not isinstance(buffer_minutes, int) or isinstance(buffer_minutes, bool) or buffer_minutes < 0:
        raise ValueError("buffer_minutes must be a non-negative integer")
    timezone_name = _setting(constraints, "timezone", config.USER_TIMEZONE)
    try:
        zone = ZoneInfo(str(timezone_name))
    except Exception as exc:
        raise ValueError(f"unknown timezone: {timezone_name!r}") from exc

    waking_start, waking_end = _clock_range(
        _setting(constraints, "waking_hours"),
        default_start="00:00",
        default_end="00:00",
        prefix="waking_hours",
        constraints=constraints,
    )
    quiet_setting = _setting(constraints, "quiet_hours", None)
    quiet_start, quiet_end = _clock_range(
        quiet_setting,
        default_start=config.QUIET_HOURS_START,
        default_end=config.QUIET_HOURS_END,
        prefix="quiet_hours",
        constraints=constraints,
    )
    busy_values = _setting(constraints, "busy_intervals", None)
    if busy_values is None:
        busy_values = _setting(constraints, "busy_blocks", None)
    if busy_values is None:
        busy_values = _setting(constraints, "busy", [])
    if busy_values is None:
        busy_values = []

    buffer_delta = timedelta(minutes=buffer_minutes)
    blockers: list[_Blocker] = []
    for index, value in enumerate(busy_values):
        block = _as_blocker(value, zone, index)
        blockers.append(
            _Blocker(
                block.start - buffer_delta,
                block.end + buffer_delta,
                block.start_title,
                block.end_title,
            )
        )

    days = _dates_covering(range_start, range_end, zone)
    # Equal quiet endpoints mean no quiet period, whereas equal waking endpoints
    # intentionally mean a full local day.
    if quiet_start != quiet_end:
        for local_date in days:
            quiet_from, quiet_to = _local_interval(local_date, quiet_start, quiet_end, zone)
            blockers.append(_Blocker(quiet_from, quiet_to, "quiet hours", "quiet hours"))
    merged_blockers = _merge_blockers(blockers)

    result: list[FreeBlock] = []
    minimum = timedelta(minutes=min_minutes)
    for local_date in days:
        wake_from, wake_to = _local_interval(local_date, waking_start, waking_end, zone)
        window_start = max(range_start, wake_from)
        window_end = min(range_end, wake_to)
        if window_end <= window_start:
            continue
        cursor = window_start
        after = "waking hours begin" if window_start == wake_from and wake_from > range_start else None
        for blocker in merged_blockers:
            if blocker.end <= window_start:
                continue
            if blocker.start >= window_end:
                break
            blocked_start = max(window_start, blocker.start)
            blocked_end = min(window_end, blocker.end)
            if blocked_start > cursor and blocked_start - cursor >= minimum:
                result.append(FreeBlock(cursor, blocked_start, after, blocker.start_title))
            if blocked_end > cursor:
                cursor = blocked_end
                after = blocker.end_title
        if window_end > cursor and window_end - cursor >= minimum:
            before = "waking hours end" if window_end == wake_to and wake_to < range_end else None
            result.append(FreeBlock(cursor, window_end, after, before))

    # Adjacent waking windows can occur only for a full-day policy; coalesce them
    # so callers see one continuous interval rather than local-midnight fragments.
    coalesced: list[FreeBlock] = []
    for block in sorted(result, key=lambda item: (item.start, item.end)):
        if coalesced and block.start == coalesced[-1].end and coalesced[-1].before is None and block.after is None:
            coalesced[-1] = FreeBlock(
                coalesced[-1].start, block.end, coalesced[-1].after, block.before
            )
        else:
            coalesced.append(block)
    return coalesced


def is_free(start: datetime, end: datetime, constraints: Any) -> bool:
    """Deterministically test a range against explicit scheduling constraints."""
    proposed_start, proposed_end = _validate_range(start, end)
    duration_minutes = max(
        1,
        int((proposed_end - proposed_start + timedelta(minutes=1) - timedelta.resolution).total_seconds() // 60),
    )
    free_blocks = compute_free_blocks(
        proposed_start, proposed_end, duration_minutes, constraints
    )
    return any(
        block.start <= proposed_start and proposed_end <= block.end
        for block in free_blocks
    )


def next_free_block(
    after: datetime,
    min_minutes: int,
    constraints: Any,
    *,
    search_end: datetime,
) -> FreeBlock | None:
    """Return the earliest suitable interval within an explicit finite horizon."""
    after_utc = _to_utc(after, "after")
    search_end_utc = _to_utc(search_end, "search_end")
    if search_end_utc <= after_utc:
        raise ValueError("search_end must be later than after")
    if not isinstance(min_minutes, int) or isinstance(min_minutes, bool) or min_minutes <= 0:
        raise ValueError("min_minutes must be a positive integer")
    free_blocks = compute_free_blocks(
        after_utc, search_end_utc, min_minutes, constraints
    )
    return free_blocks[0] if free_blocks else None


def _schedule_block(record: Mapping[str, Any], source: Literal["gcal", "event", "task"]) -> ScheduleBlock:
    start = _parse_datetime(record.get("start_time", record.get("scheduled_start")), "block start")
    end = _parse_datetime(record.get("end_time", record.get("scheduled_end")), "block end")
    if end <= start:
        raise ValueError("schedule block end must be later than start")
    if source == "gcal":
        source_id = record.get("gcal_event_id", record.get("id", ""))
    else:
        source_id = record.get("id", record.get("gcal_event_id", ""))
    return ScheduleBlock(
        start=start,
        end=end,
        title=str(record.get("title") or "Untitled Event"),
        source=source,
        source_id=str(source_id),
        metadata=dict(record),
    )


async def query_schedule(
    store: Store, calendar: CalendarService, start: datetime, end: datetime
) -> list[ScheduleBlock]:
    """Return a merged, sorted view of all occupied schedule sources."""
    range_start, range_end = _validate_range(start, end)
    google_records = await calendar.list_events(range_start, range_end)
    if getattr(calendar, "_last_query_complete", True) is False:
        raise CalendarQueryIncompleteError(
            "Google Calendar returned an incomplete event set; availability is unknown"
        )
    event_records = await store.query_events(range_start, range_end)
    task_records = await store.query_tasks()
    task_blocks: list[ScheduleBlock] = []
    for record in task_records:
        if record.get("scheduled_start") is None or record.get("scheduled_end") is None:
            continue
        block = _schedule_block(record, "task")
        if block.start < range_end and range_start < block.end:
            task_blocks.append(block)
    task_gcal_ids = {
        str(block.metadata.get("gcal_event_id")).strip()
        for block in task_blocks
        if str(block.metadata.get("gcal_event_id") or "").strip()
    }
    blocks: list[ScheduleBlock] = [
        _schedule_block(record, "gcal")
        for record in google_records
        if str(record.get("gcal_event_id", record.get("id", ""))).strip()
        not in task_gcal_ids
    ]
    blocks.extend(_schedule_block(record, "event") for record in event_records)
    blocks.extend(task_blocks)
    return sorted(blocks, key=lambda block: (block.start, block.end, block.title))


def merge_blocks(blocks: list[ScheduleBlock]) -> list[ScheduleBlock]:
    """Merge overlapping occupied intervals while retaining all contributors."""
    if not blocks:
        return []
    normalized: list[ScheduleBlock] = []
    for block in blocks:
        start, end = _validate_range(block.start, block.end)
        normalized.append(replace(block, start=start, end=end))
    ordered = sorted(normalized, key=lambda block: (block.start, block.end))
    result: list[ScheduleBlock] = []
    group: list[ScheduleBlock] = [ordered[0]]
    group_end = ordered[0].end

    def finish(items: list[ScheduleBlock], merged_end: datetime) -> ScheduleBlock:
        first = items[0]
        titles = list(dict.fromkeys(item.title for item in items))
        metadata = dict(first.metadata)
        metadata["merged_blocks"] = [
            {
                "start": item.start,
                "end": item.end,
                "title": item.title,
                "source": item.source,
                "source_id": item.source_id,
                "metadata": item.metadata,
            }
            for item in items
        ]
        metadata["titles"] = titles
        return ScheduleBlock(
            start=first.start,
            end=merged_end,
            title=" / ".join(titles),
            source=first.source,
            source_id=first.source_id,
            metadata=metadata,
        )

    for block in ordered[1:]:
        if block.start <= group_end:
            group.append(block)
            group_end = max(group_end, block.end)
        else:
            result.append(finish(group, group_end))
            group = [block]
            group_end = block.end
    result.append(finish(group, group_end))
    return result


async def find_free_blocks(
    store: Store,
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    min_minutes: int,
) -> list[FreeBlock]:
    """Return available blocks in an aware UTC half-open search range."""
    occupied = await query_schedule(store, calendar, start, end)
    return compute_free_blocks(
        start,
        end,
        min_minutes,
        {
            "busy_intervals": occupied,
            "waking_hours": ("00:00", "00:00"),
            "quiet_hours": {
                "start": config.QUIET_HOURS_START,
                "end": config.QUIET_HOURS_END,
            },
            "buffer_minutes": 15,
        },
    )


def has_conflict(blocks: list[ScheduleBlock], start: datetime, end: datetime) -> bool:
    """Return whether a proposed aware UTC block overlaps an occupied block."""
    proposed_start, proposed_end = _validate_range(start, end)
    for block in blocks:
        block_start, block_end = _validate_range(block.start, block.end)
        if block_start < proposed_end and proposed_start < block_end:
            return True
    return False
