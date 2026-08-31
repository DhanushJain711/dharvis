"""Reminder tool bindings stay isolated from calendar and scheduling services."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.integration import build_tool_handlers
from src.tools import TOOLS_BY_NAME


class _ReminderStore:
    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.updated: tuple[int, dict[str, Any]] | None = None
        self.cancelled: int | None = None
        self.queried: tuple[Any, ...] | None = None

    async def add_reminders(self, reminders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        self.created = reminders
        return [
            {"id": index, "status": "pending", **item}
            for index, item in enumerate(reminders, start=1)
        ]

    async def update_reminder(self, reminder_id: int, changes: dict[str, Any]) -> dict[str, Any]:
        self.updated = (reminder_id, changes)
        return {"id": reminder_id, "status": "pending", **changes}

    async def cancel_reminder(self, reminder_id: int) -> dict[str, Any]:
        self.cancelled = reminder_id
        return {"id": reminder_id, "status": "cancelled"}

    async def query_reminders(
        self,
        status: str | None,
        remind_before: datetime | None,
        remind_after: datetime | None,
    ) -> list[dict[str, Any]]:
        self.queried = (status, remind_before, remind_after)
        return []


class _MustNotBeCalled:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"reminder handler touched unrelated service method {name}")


@pytest.mark.asyncio
async def test_batch_add_reminders_uses_store_only_and_normalizes_utc() -> None:
    store = _ReminderStore()
    untouched = _MustNotBeCalled()
    handlers = await build_tool_handlers(store, untouched, untouched, untouched)  # type: ignore[arg-type]

    result = await handlers["add_reminder"](reminders=[
        {"message": "call the dentist", "remind_at": "2026-10-31T09:00:00-05:00"},
        {"message": "email Sam", "remind_at": "2026-11-06T15:00:00-06:00"},
    ])

    assert len(store.created) == 2
    assert store.created[0]["remind_at"] == datetime(2026, 10, 31, 14, tzinfo=UTC)
    assert result[0]["remind_at"] == "2026-10-31T14:00:00Z"
    assert result[1]["message"] == "email Sam"


@pytest.mark.asyncio
async def test_update_cancel_and_query_reminders_use_store_only() -> None:
    store = _ReminderStore()
    untouched = _MustNotBeCalled()
    handlers = await build_tool_handlers(store, untouched, untouched, untouched)  # type: ignore[arg-type]

    updated = await handlers["update_reminder"](
        reminder_id=8,
        message=None,
        remind_at="2026-09-02T15:00:00-05:00",
    )
    assert store.updated == (
        8,
        {"remind_at": datetime(2026, 9, 2, 20, tzinfo=UTC)},
    )
    assert updated["remind_at"] == "2026-09-02T20:00:00Z"

    assert await handlers["cancel_reminder"](reminder_id=8) == {
        "id": 8, "status": "cancelled",
    }
    assert store.cancelled == 8

    await handlers["query_reminders"](
        status="pending",
        remind_before="2026-09-03T00:00:00Z",
        remind_after=None,
    )
    assert store.queried == (
        "pending", datetime(2026, 9, 3, tzinfo=UTC), None,
    )


@pytest.mark.asyncio
async def test_update_reminder_rejects_an_empty_change() -> None:
    untouched = _MustNotBeCalled()
    handlers = await build_tool_handlers(
        _ReminderStore(), untouched, untouched, untouched  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="new reminder message or due time"):
        await handlers["update_reminder"](
            reminder_id=1, message=None, remind_at=None,
        )


def test_reminder_schema_and_handler_names_are_part_of_canonical_sets() -> None:
    assert {"add_reminder", "update_reminder", "cancel_reminder", "query_reminders"} <= set(TOOLS_BY_NAME)
