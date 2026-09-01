"""Cross-module regressions for zero-duration external calendar entries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from src.calendar_service import CalendarService
from src.facts_engine import FactsEngine
from src.integration import build_tool_handlers
from src.store import Store


class _Request:
    def __init__(self, value: dict) -> None:
        self.value = value

    def execute(self) -> dict:
        return self.value


class _ParsingCalendar(CalendarService):
    """Use the real Google parser while recording application-owned writes."""

    def __init__(self, token_path: Path, upstream_events: list[Any]) -> None:
        super().__init__(token_path=token_path)
        credentials = MagicMock(expired=False, valid=True)
        self._credentials = credentials
        google = MagicMock()
        calendar_list = MagicMock()
        calendar_list.list.return_value = _Request(
            {"items": [{"id": "external", "summary": "External"}]}
        )
        google.calendarList.return_value = calendar_list
        events = MagicMock()
        events.list.return_value = _Request({"items": upstream_events})
        google.events.return_value = events
        self.event_resource = events
        self._service = google
        self.created: list[dict] = []

    async def create_event(
        self,
        event: dict,
        reasoning: str | None = None,
        *,
        category: str | None = None,
        kind: str = "fixed-event",
    ) -> dict:
        created = dict(event)
        created["gcal_event_id"] = f"created-{len(self.created) + 1}"
        self.created.append(created)
        return created


class _Scheduler:
    async def detect_conflicts(self, start: datetime, end: datetime) -> list:
        return []


def _google_event(event_id: str, start: datetime, end: datetime) -> dict:
    return {
        "id": event_id,
        "summary": event_id,
        "start": {"dateTime": start.isoformat()},
        "end": {"dateTime": end.isoformat()},
    }


def _event_payload(start: datetime, end: datetime) -> dict:
    return {
        "title": "Dinner",
        "description": None,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "location": None,
        "category": "personal",
    }


@pytest.mark.asyncio
async def test_fixed_event_preflight_ignores_external_zero_duration_entry(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 9, 2, 19, tzinfo=UTC)
    calendar = _ParsingCalendar(
        tmp_path / "token.json",
        [_google_event("empty", start + timedelta(minutes=30), start + timedelta(minutes=30))],
    )
    store = Store(tmp_path / "zero.sqlite")
    await store.initialize()
    handlers = await build_tool_handlers(store, calendar, _Scheduler(), FactsEngine(store))

    result = await handlers["add_event"](
        events=[_event_payload(start, start + timedelta(hours=1))]
    )

    assert result["events"][0]["title"] == "Dinner"
    assert len(calendar.created) == 1
    assert calendar._last_query_complete is True


@pytest.mark.asyncio
async def test_fixed_event_preflight_still_warns_for_positive_duration_overlap(
    tmp_path: Path,
) -> None:
    start = datetime(2026, 9, 2, 19, tzinfo=UTC)
    calendar = _ParsingCalendar(
        tmp_path / "token.json",
        [
            _google_event(
                "meeting",
                start + timedelta(minutes=30),
                start + timedelta(minutes=45),
            )
        ],
    )
    store = Store(tmp_path / "overlap.sqlite")
    await store.initialize()
    handlers = await build_tool_handlers(store, calendar, _Scheduler(), FactsEngine(store))

    result = await handlers["add_event"](
        events=[_event_payload(start, start + timedelta(hours=1))]
    )

    assert result["confirmation_required"] is True
    assert result["conflicts"][0]["conflict"]["title"] == "meeting"
    assert calendar.created == []
    assert calendar._last_query_complete is True


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_event", [None, "not-an-event", 42])
async def test_non_mapping_google_event_is_incomplete_and_uncached(
    tmp_path: Path, invalid_event: Any
) -> None:
    calendar = _ParsingCalendar(tmp_path / "token.json", [invalid_event])

    assert await calendar.list_events(
        datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
    ) == []
    assert calendar._last_query_complete is False
    assert await calendar.list_events(
        datetime(2026, 9, 2, tzinfo=UTC), datetime(2026, 9, 3, tzinfo=UTC)
    ) == []
    assert calendar.event_resource.list.call_count == 2
