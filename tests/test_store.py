"""Focused behavioral tests for the higher-level Store helpers."""

from datetime import UTC, datetime, timedelta
from typing import AsyncIterator
from zoneinfo import ZoneInfo

import pytest

import src.store as store_module
from src.store import Store


CHICAGO = ZoneInfo("America/Chicago")


@pytest.fixture
async def store() -> AsyncIterator[Store]:
    store = Store(":memory:")
    await store.initialize()
    try:
        yield store
    finally:
        if store._keeper is not None:
            await store._keeper.close()


@pytest.mark.asyncio
async def test_find_task_by_description_ranks_math_and_biases_pending_recent(
    store: Store,
) -> None:
    created = await store.add_tasks(
        [
            {"title": "Finish the math worksheet", "category": "school"},
            {"title": "Finish the math worksheet", "category": "school"},
            {"title": "Read the history chapter", "category": "school"},
            {"title": "Buy groceries", "category": "errand"},
            {"title": "Call the dentist", "category": "personal"},
        ]
    )
    pending_math, completed_math = created[:2]
    await store.complete_task(completed_math["id"])

    now = datetime(2026, 8, 26, 18, tzinfo=UTC)
    async with store.connection() as db:
        await db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            ((now - timedelta(days=180)).isoformat(), completed_math["id"]),
        )
        await db.execute(
            "UPDATE tasks SET created_at = ? WHERE id = ?",
            (now.isoformat(), pending_math["id"]),
        )
        await db.commit()

    results = await store.find_task_by_description("mark the math thing done")

    assert 1 <= len(results) <= 3
    assert results[0]["id"] == pending_math["id"]
    assert results[0]["status"] == "pending"
    assert all(isinstance(result["score"], (int, float)) for result in results)
    assert all(0.0 <= result["score"] <= 1.0 for result in results)
    assert [result["score"] for result in results] == sorted(
        (result["score"] for result in results), reverse=True
    )
    completed_result = next(
        result for result in results if result["id"] == completed_math["id"]
    )
    assert results[0]["score"] > completed_result["score"]


@pytest.mark.asyncio
async def test_upsert_fact_deduplicates_across_category_and_formats_active_facts(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_confirmation = datetime(2026, 8, 20, 15, tzinfo=UTC)
    original = await store.add_fact(
        {
            "content": "User likes the gym late",
            "category": "fitness",
            "confidence": 0.9,
            "source": "explicit",
            "last_confirmed_at": first_confirmation,
        }
    )
    reconfirmed_at = datetime(2026, 8, 26, 22, 30, tzinfo=UTC)
    monkeypatch.setattr(store_module.timeutil, "now_utc", lambda: reconfirmed_at)

    duplicate = await store.upsert_fact(
        "User prefers going to the gym in the evening", "scheduling"
    )

    assert duplicate["id"] == original["id"]
    assert duplicate["category"] == "fitness"
    assert duplicate["content"] == "User likes the gym late"
    assert duplicate["evidence_count"] == 2
    assert duplicate["last_confirmed_at"] == reconfirmed_at
    assert len(await store.query_facts(active=True)) == 1

    await store.add_fact(
        {
            "content": "Avoids peanuts",
            "category": "diet",
            "confidence": 0.75,
            "source": "explicit",
            "evidence_count": 3,
        }
    )
    await store.add_fact(
        {
            "content": "Old inactive preference",
            "category": "archive",
            "confidence": 1.0,
            "source": "explicit",
            "active": False,
        }
    )

    assert await store.get_active_facts() == (
        "diet:\n"
        "- Avoids peanuts (confidence 0.75; evidence 3)\n"
        "\n"
        "fitness:\n"
        "- User likes the gym late (confidence 0.90; evidence 2)"
    )


@pytest.mark.asyncio
async def test_get_goal_progress_uses_local_week_boundaries_and_days_left(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module.timeutil, "_local_zone", lambda: CHICAGO)
    monkeypatch.setattr(
        store_module.timeutil,
        "now_local",
        lambda: datetime(2026, 3, 10, 12, tzinfo=CHICAGO),
    )
    goal = await store.add_goal(
        {
            "title": "Exercise four times",
            "target_amount": 4,
            "target_unit": "sessions",
            "period": "week",
            "category": "fitness",
        }
    )
    period_start = datetime(2026, 3, 9, 0, tzinfo=CHICAGO)
    start_utc = datetime(2026, 3, 9, 5, tzinfo=UTC)
    end_utc = datetime(2026, 3, 16, 5, tzinfo=UTC)
    for amount, logged_at in (
        (9, start_utc - timedelta(microseconds=1)),
        (1, start_utc),
        (1.5, end_utc - timedelta(microseconds=1)),
        (9, end_utc),
    ):
        await store.log_goal_progress(goal["id"], amount, "manual", logged_at)

    progress = await store.get_goal_progress(goal["id"], period_start)

    assert progress == {
        "goal_id": goal["id"],
        "period_start": start_utc,
        "period_end": end_utc,
        "amount_done": 2.5,
        "amount_remaining": 1.5,
        "days_left": 6,
    }
    with pytest.raises(ValueError, match="naive"):
        await store.get_goal_progress(goal["id"], datetime(2026, 3, 9))


@pytest.mark.asyncio
async def test_get_goal_progress_uses_local_month_boundaries(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(store_module.timeutil, "_local_zone", lambda: CHICAGO)
    monkeypatch.setattr(
        store_module.timeutil,
        "now_local",
        lambda: datetime(2026, 11, 15, 9, tzinfo=CHICAGO),
    )
    goal = await store.add_goal(
        {
            "title": "Study this month",
            "target_amount": 20,
            "target_unit": "hours",
            "period": "month",
            "category": "school",
        }
    )
    period_start = datetime(2026, 11, 1, 0, tzinfo=CHICAGO)
    start_utc = datetime(2026, 11, 1, 5, tzinfo=UTC)
    end_utc = datetime(2026, 12, 1, 6, tzinfo=UTC)
    await store.log_goal_progress(goal["id"], 7.25, "manual", start_utc)
    await store.log_goal_progress(goal["id"], 50, "manual", end_utc)

    progress = await store.get_goal_progress(goal["id"], period_start)

    assert progress["period_start"] == start_utc
    assert progress["period_end"] == end_utc
    assert progress["amount_done"] == 7.25
    assert progress["amount_remaining"] == 12.75
    assert progress["days_left"] == 16


@pytest.mark.asyncio
async def test_decision_history_order_surfacing_and_validation(store: Store) -> None:
    task = (await store.add_tasks([{"title": "Write project outline"}]))[0]
    start = datetime(2026, 8, 27, 15, tzinfo=UTC)

    first = await store.record_decision(
        task["id"],
        "scheduled",
        start,
        start + timedelta(hours=1),
        None,
        "daily_plan",
        "The morning slot leaves enough focused work time.",
        [],
    )
    second = await store.record_decision(
        task["id"],
        "moved",
        start + timedelta(hours=2),
        start + timedelta(hours=3),
        (start, start + timedelta(hours=1)),
        "conflict",
        "A calendar conflict requires moving this work later.",
        [],
    )
    async with store.connection() as db:
        await db.execute(
            "UPDATE schedule_decisions SET decided_at = ? WHERE id = ?",
            ("2026-08-26T10:00:00.000100Z", first["id"]),
        )
        await db.execute(
            "UPDATE schedule_decisions SET decided_at = ? WHERE id = ?",
            ("2026-08-26T10:00:00.000200+00:00", second["id"]),
        )
        await db.commit()

    chronological = await store.get_schedule_decisions(task["id"])
    newest_first = await store.get_decisions_for_task(task["id"])
    assert [item["id"] for item in chronological] == [first["id"], second["id"]]
    assert [item["id"] for item in newest_first] == [second["id"], first["id"]]
    assert all(item["start"].tzinfo is UTC for item in chronological)

    since = datetime(2026, 8, 26, 10, 0, 0, 150, tzinfo=UTC)
    assert [item["id"] for item in await store.get_unsurfaced_decisions(since)] == [
        second["id"]
    ]
    await store.mark_decision_surfaced(second["id"])
    assert await store.get_unsurfaced_decisions(since) == []
    with pytest.raises(KeyError):
        await store.mark_decision_surfaced(999_999)
    with pytest.raises(ValueError, match="naive"):
        await store.get_unsurfaced_decisions(datetime(2026, 8, 26, 10))

    for placeholder in ("placeholder", "some reason", "TODO: explain later"):
        with pytest.raises(ValueError, match="placeholder|meaningful"):
            await store.record_decision(
                task["id"],
                "scheduled",
                start,
                start + timedelta(hours=1),
                None,
                "user_request",
                placeholder,
                [],
            )
    with pytest.raises(ValueError, match="naive"):
        await store.record_decision(
            task["id"],
            "scheduled",
            datetime(2026, 8, 27, 15),
            start + timedelta(hours=1),
            None,
            "user_request",
            "The user explicitly selected this available slot.",
            [],
        )
