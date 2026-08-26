"""Contracts for merged schedule and free-time calculation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from .calendar_service import CalendarService
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
    """One available aware UTC half-open interval."""

    start: datetime
    end: datetime


async def query_schedule(
    store: Store, calendar: CalendarService, start: datetime, end: datetime
) -> list[ScheduleBlock]:
    """Return a merged, sorted view of all occupied schedule sources."""
    return []


def merge_blocks(blocks: list[ScheduleBlock]) -> list[ScheduleBlock]:
    """Merge overlapping occupied intervals while retaining source metadata."""
    raise NotImplementedError


async def find_free_blocks(
    store: Store,
    calendar: CalendarService,
    start: datetime,
    end: datetime,
    min_minutes: int,
) -> list[FreeBlock]:
    """Return available blocks in an aware UTC half-open search range."""
    return []


def has_conflict(blocks: list[ScheduleBlock], start: datetime, end: datetime) -> bool:
    """Return whether a proposed aware UTC block overlaps an occupied block."""
    raise NotImplementedError
