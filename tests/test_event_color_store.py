"""Focused persistence tests for Google Calendar per-event color IDs."""

from datetime import UTC, datetime
from typing import AsyncIterator

import aiosqlite
import pytest

from src.store import Store


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    repository = Store(":memory:")
    await repository.initialize()
    try:
        yield repository
    finally:
        assert repository._keeper is not None
        await repository._keeper.close()


@pytest.mark.asyncio
async def test_event_color_round_trips_through_create_update_clear_and_query(
    store: Store,
) -> None:
    start = datetime(2026, 9, 2, 14, tzinfo=UTC)
    end = datetime(2026, 9, 2, 15, tzinfo=UTC)
    created = (
        await store.add_events(
            [{
                "title": "CS 311 discussion",
                "start": start,
                "end": end,
                "category": "school",
                "color_id": "6",
            }]
        )
    )[0]

    assert created["color_id"] == "6"
    assert (await store.get_event(created["id"]))["color_id"] == "6"
    assert (await store.query_events(start, end))[0]["color_id"] == "6"

    updated = await store.update_event(created["id"], {"color_id": "9"})
    assert updated["color_id"] == "9"

    cleared = await store.update_event(created["id"], {"clear_fields": ["color_id"]})
    assert cleared["color_id"] is None


@pytest.mark.asyncio
async def test_store_rejects_invalid_event_color_ids(store: Store) -> None:
    start = datetime(2026, 9, 2, 14, tzinfo=UTC)
    end = datetime(2026, 9, 2, 15, tzinfo=UTC)

    for invalid in (0, 1, "0", "12", "blue", ""):
        with pytest.raises(ValueError, match="color_id"):
            await store.add_events(
                [{
                    "title": "Invalid color",
                    "start": start,
                    "end": end,
                    "color_id": invalid,
                }]
            )

    event = (
        await store.add_events(
            [{"title": "No color", "start": start, "end": end}]
        )
    )[0]
    for invalid in (0, "12", "orange"):
        with pytest.raises(ValueError, match="color_id"):
            await store.update_event(event["id"], {"color_id": invalid})


@pytest.mark.asyncio
async def test_database_constraint_rejects_invalid_event_color(store: Store) -> None:
    start = datetime(2026, 9, 2, 14, tzinfo=UTC)
    end = datetime(2026, 9, 2, 15, tzinfo=UTC)
    event = (
        await store.add_events(
            [{"title": "Constrained", "start": start, "end": end}]
        )
    )[0]

    async with store.connection() as db:
        with pytest.raises(aiosqlite.IntegrityError):
            await db.execute(
                "UPDATE events SET color_id = '12' WHERE id = ?", (event["id"],)
            )
