"""Behavioral regressions for replay, unfinished work and delayed Google cleanup."""

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.calendar_service import CalendarError, CalendarWriteUncertainError
from src.facts_engine import FactsEngine
from src.integration import build_tool_handlers, complete_task_with_calendar, drain_calendar_cleanup
from src.store import Store
from src import timeutil
from tests.test_integration_runtime import FakeCalendar, SpyScheduler


@pytest.fixture
async def store(tmp_path):
    result = Store(tmp_path / "state.db")
    await result.initialize()
    return result


@pytest.mark.asyncio
async def test_notification_times_and_telegram_receipts_survive_restart(store):
    defaults = await store.get_notification_times()
    await store.set_notification_times(morning="08:35")
    reopened = Store(store.db_path)
    assert await reopened.get_notification_times() == {**defaults, "morning": "08:35"}
    with pytest.raises(ValueError, match="evening"):
        await store.set_notification_times(morning="09:00", evening="24:01")
    assert (await store.get_notification_times())["morning"] == "08:35"
    assert not await reopened.has_processed_update(42)
    await store.mark_update_processed(42)
    await store.mark_update_processed(42)
    assert await reopened.has_processed_update(42)
    assert not await reopened.has_processed_update(43)


@pytest.mark.asyncio
async def test_partial_work_preserves_task_and_does_not_credit_goal(store):
    goal = await store.add_goal({"title": "Apply", "target_amount": 3,
        "target_unit": "hours", "period": "week", "category": "career"})
    task = (await store.add_tasks([{"title": "Apply for jobs", "estimated_minutes": 90,
                                 "goal_id": goal["id"]}]))[0]
    first = await store.log_task_progress(task["id"], 60, 45, "two applications drafted")
    replay = await store.log_task_progress(task["id"], 60, 45, "two applications drafted")
    assert replay == first
    assert replay["status"] == "pending"
    assert replay["progress_minutes"] == 60
    assert replay["estimated_minutes"] == 45
    assert len(await store.query_tasks()) == 1
    async with store.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM goal_progress")).fetchone())[0] == 0
    changed = await store.log_task_progress(task["id"], 80, None, "sent the first one")
    assert changed["estimated_minutes"] == 45


@pytest.mark.asyncio
async def test_completion_is_atomic_once_and_cleanup_survives_failure(store):
    goal = await store.add_goal({"title": "Gym", "target_amount": 3,
        "target_unit": "sessions", "period": "week", "category": "fitness"})
    task = (await store.add_tasks([{"title": "Gym session", "estimated_minutes": 60,
                                  "goal_id": goal["id"]}]))[0]
    start = datetime(2026, 9, 20, 15, tzinfo=UTC)
    await store.apply_schedule_decision(task["id"], "scheduled", start,
        start + timedelta(hours=1), None, None, "daily_plan",
        "this hour clears the gym's closing time", [], "owned-work-1")

    class OfflineCalendar(FakeCalendar):
        async def delete_work_block(self, event_id):
            raise CalendarError("temporarily unavailable")

    saved = await complete_task_with_calendar(store, OfflineCalendar(), task["id"], 50)
    assert saved["status"] == "completed"
    assert saved["calendar_sync_pending"]
    assert saved["gcal_event_id"] is None
    replay = await store.complete_task(task["id"], 90, "debrief")
    assert replay["actual_minutes"] == 50
    assert replay["completed_at"] == saved["completed_at"]
    assert len(await Store(store.db_path).list_pending_calendar_cleanup()) == 1
    async with store.connection() as db:
        rows = await (await db.execute("SELECT amount FROM goal_progress WHERE task_id=?", (task["id"],))).fetchall()
    assert [row[0] for row in rows] == [1]
    calendar = FakeCalendar()
    assert await drain_calendar_cleanup(store, calendar) == 1
    assert calendar.deleted == ["owned-work-1"]
    assert await store.list_pending_calendar_cleanup() == []
    assert await drain_calendar_cleanup(store, calendar) == 0


@pytest.mark.asyncio
async def test_concurrent_completion_credits_hours_once(store):
    goal = await store.add_goal({"title": "Applications", "target_amount": 3,
        "target_unit": "hours", "period": "week", "category": "career"})
    task = (await store.add_tasks([{"title": "Applications", "goal_id": goal["id"]}]))[0]
    results = await asyncio.gather(store.complete_task(task["id"], 90), store.complete_task(task["id"], 90))
    assert results[0] == results[1]
    async with store.connection() as db:
        rows = await (await db.execute("SELECT amount FROM goal_progress")).fetchall()
    assert [row[0] for row in rows] == [1.5]


@pytest.mark.asyncio
async def test_partial_duration_not_mistaken_for_final_observed_duration(store):
    task = (await store.add_tasks([{"title": "Math pset", "estimated_minutes": 120}]))[0]
    await store.log_task_progress(task["id"], 30, None, "started it")
    completed = await store.complete_task(task["id"])
    assert completed["actual_minutes"] is None
    inferred = await store.infer_task_duration("Math pset", "personal", "light")
    assert inferred["estimated_minutes"] is None


@pytest.mark.asyncio
async def test_exact_active_task_replay_does_not_merge_other_occurrences(store):
    when = datetime(2026, 9, 20, 15, tzinfo=UTC)
    payload = {"title": "Math pset 1", "deadline": when, "category": "school"}
    first, replay = await asyncio.gather(store.add_tasks([payload]), store.add_tasks([payload]))
    assert first[0]["id"] == replay[0]["id"]
    assert (await store.add_tasks([{**payload, "title": "  MATH   pset 1 "}]))[0]["id"] == first[0]["id"]
    await store.add_tasks([{**payload, "title": "Math pset 2"},
                           {**payload, "deadline": when + timedelta(days=7)},
                           {**payload, "category": "personal"}])
    assert len(await store.query_tasks()) == 4


@pytest.mark.asyncio
async def test_concurrent_identical_event_calls_create_one_remote_event(store):
    calendar = FakeCalendar()
    handlers = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    event = {"title": "Dinner", "start": "2026-09-21T23:00:00Z", "end": "2026-09-22T00:00:00Z"}
    first, replay = await asyncio.gather(handlers["add_event"]([event]), handlers["add_event"]([event]))
    assert len(calendar.events) == 1
    assert first["events"][0]["id"] == replay["events"][0]["id"]
    assert replay["already_exists"]


@pytest.mark.asyncio
async def test_partial_progress_tool_updates_existing_record(store):
    handlers = await build_tool_handlers(store, FakeCalendar(), SpyScheduler(), FactsEngine(store))
    task = (await store.add_tasks([{"title": "Pset", "estimated_minutes": 120}]))[0]
    result = await handlers["log_task_progress"](task["id"], 60, None, "not done")
    assert result["id"] == task["id"]
    assert result["progress_minutes"] == 60
    assert len(await store.query_tasks()) == 1


@pytest.mark.asyncio
async def test_unknown_google_write_is_not_reported_as_compensated(store):
    class UncertainCalendar(FakeCalendar):
        async def create_event(self, *args, **kwargs):
            raise CalendarWriteUncertainError("It may have gone through; check before retrying")

    handlers = await build_tool_handlers(store, UncertainCalendar(), SpyScheduler(), FactsEngine(store))
    with pytest.raises(RuntimeError, match="may have gone through") as raised:
        await handlers["add_event"]([{"title": "Dinner", "start": "2026-09-21T23:00:00Z", "end": "2026-09-22T00:00:00Z"}])
    assert not raised.value.compensated


@pytest.mark.asyncio
async def test_uncertain_confirmation_retains_claim_without_new_proposal(store):
    calendar = FakeCalendar()
    handlers = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    await handlers["add_event"]([{"title": "Meeting", "start": "2026-09-21T23:00:00Z", "end": "2026-09-22T00:00:00Z"}])
    proposed = await handlers["add_event"]([{"title": "Dinner", "start": "2026-09-21T23:30:00Z", "end": "2026-09-22T00:30:00Z"}])
    real_create = calendar.create_event

    async def uncertain(*args, **kwargs):
        await real_create(*args, **kwargs)
        raise CalendarWriteUncertainError("It may have gone through; check before retrying")

    calendar.create_event = uncertain
    with pytest.raises(RuntimeError, match="may have gone through"):
        await handlers["confirm_event_change"](proposed["proposal_id"])
    replay = await handlers["confirm_event_change"](proposed["proposal_id"])
    assert replay == {"applied": False, "reason": "proposal_unavailable"}
    assert len(calendar.events) == 2


@pytest.mark.asyncio
async def test_missing_calendar_does_not_block_completion_or_forget_cleanup(store):
    task = (await store.add_tasks([{"title": "Report"}]))[0]
    start = datetime(2026, 9, 20, 15, tzinfo=UTC)
    await store.apply_schedule_decision(task["id"], "scheduled", start, start + timedelta(hours=1),
        None, None, "daily_plan", "the hour before class fits the report", [], "work-absent")
    result = await complete_task_with_calendar(store, None, task["id"])
    assert result["status"] == "completed"
    assert result["calendar_sync_pending"]
    assert (await store.list_pending_calendar_cleanup())[0]["gcal_event_id"] == "work-absent"


@pytest.mark.asyncio
@pytest.mark.parametrize("change_goal", [False, True])
async def test_reopen_reverses_completion_then_recredits_correct_duration(
    store, monkeypatch, change_goal,
):
    now = datetime(2026, 9, 20, 15, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "now_utc", lambda: now)
    goal = await store.add_goal({"title": "Study", "target_amount": 3,
        "target_unit": "hours", "period": "week", "category": "school"})
    replacement = await store.add_goal({"title": "Practice", "target_amount": 4,
        "target_unit": "hours", "period": "week", "category": "career"})
    task = (await store.add_tasks([{"title": "Practice problems", "estimated_minutes": 60,
        "goal_id": goal["id"], "category": "school"}]))[0]
    await store.log_task_progress(task["id"], 20, None, "first problem done")
    await store.log_goal_progress(goal["id"], 2, "manual", now)
    await store.apply_schedule_decision(task["id"], "scheduled", now,
        now + timedelta(hours=1), None, None, "daily_plan",
        "this hour fits before the next lecture", [], "old-owned-block")
    handlers = await build_tool_handlers(store, None, SpyScheduler(), FactsEngine(store))
    completed = await handlers["complete_task"](task["id"], 30)
    assert completed["calendar_sync_pending"]
    day = timeutil.to_local(now).date()
    await store.upsert_daily_log(day, {"completed": [
        {"task_id": task["id"], "actual_minutes": 30},
        {"task_id": 999, "actual_minutes": 15},
    ]})
    now += timedelta(minutes=1)
    changes = {"goal_id": replacement["id"], "category": "career"} if change_goal else {}
    restored = await handlers["update_task"](task["id"], [], status="pending", **changes)
    assert restored["status"] == "pending"
    assert restored["completed_at"] is restored["actual_minutes"] is restored["actual_minutes_source"] is None
    assert restored["progress_minutes"] == 20
    assert restored["progress_notes"] == "first problem done"
    assert (await store.get_task(task["id"]))["reopened_at"] == now
    assert (await store.get_daily_log(day))["completed"] == [{"task_id": 999, "actual_minutes": 15}]
    assert [row["gcal_event_id"] for row in await store.list_pending_calendar_cleanup()] == ["old-owned-block"]
    async with store.connection() as db:
        credits = await (await db.execute("SELECT amount, source FROM goal_progress")).fetchall()
    assert [tuple(row) for row in credits] == [(2, "manual")]

    await handlers["update_task"](task["id"], [], status="pending")
    assert (await store.get_task(task["id"]))["reopened_at"] == now
    now += timedelta(minutes=2)
    completed = await handlers["complete_task"](task["id"], 90)
    replay = await handlers["complete_task"](task["id"], 120)
    assert completed["actual_minutes"] == replay["actual_minutes"] == 90
    assert completed["completed_at"] == replay["completed_at"]
    async with store.connection() as db:
        credits = await (await db.execute(
            "SELECT goal_id, amount, source FROM goal_progress ORDER BY id"
        )).fetchall()
    credited_goal = replacement["id"] if change_goal else goal["id"]
    assert [tuple(row) for row in credits] == [(goal["id"], 2, "manual"), (credited_goal, 1.5, "task")]
    calendar = FakeCalendar()
    assert await drain_calendar_cleanup(store, calendar) == 1
    assert calendar.deleted == ["old-owned-block"]


@pytest.mark.asyncio
async def test_reopen_preserves_manual_task_credit_and_rolls_back_invalid_change(store):
    now = datetime(2026, 9, 20, 15, tzinfo=UTC)
    old_goal = await store.add_goal({"title": "Old goal", "target_amount": 3,
        "target_unit": "hours", "period": "week", "category": "school"})
    goal = await store.add_goal({"title": "New goal", "target_amount": 3,
        "target_unit": "hours", "period": "week", "category": "school"})
    task = (await store.add_tasks([{"title": "Pset", "goal_id": old_goal["id"]}]))[0]
    manual = await store.log_goal_progress(old_goal["id"], 2, "manual", now, task["id"])
    await store.update_task(task["id"], {"goal_id": goal["id"]})
    before = await store.complete_task(task["id"], 30)
    day = timeutil.to_local(now).date()
    await store.upsert_daily_log(day, {"completed": [{"task_id": task["id"]}]})
    with pytest.raises(sqlite3.IntegrityError):
        await store.update_task(task["id"], {"status": "pending", "category": "invalid"})
    assert await store.get_task(task["id"]) == before
    assert (await store.get_daily_log(day))["completed"] == [{"task_id": task["id"]}]
    async with store.connection() as db:
        assert (await (await db.execute("SELECT COUNT(*) FROM goal_progress")).fetchone())[0] == 2
    await store.update_task(task["id"], {"status": "pending"})
    async with store.connection() as db:
        rows = await (await db.execute("SELECT id FROM goal_progress")).fetchall()
    assert [row[0] for row in rows] == [manual["id"]]


@pytest.mark.asyncio
async def test_old_completion_observation_cannot_finish_reopened_task(store, monkeypatch):
    now = datetime(2026, 9, 20, 15, tzinfo=UTC)
    monkeypatch.setattr(timeutil, "now_utc", lambda: now)
    task = (await store.add_tasks([{"title": "Pset"}]))[0]
    await store.complete_task(task["id"], 30)
    issued_at = now
    now += timedelta(minutes=1)
    restored = await store.update_task(task["id"], {"status": "pending"})
    ignored = await complete_task_with_calendar(
        store, FakeCalendar(), task["id"], 30, actual_minutes_source="debrief", observed_at=issued_at
    )
    assert ignored == restored
    assert "calendar_sync_pending" not in ignored
    now += timedelta(minutes=1)
    completed = await complete_task_with_calendar(
        store, FakeCalendar(), task["id"], 90, actual_minutes_source="debrief", observed_at=now
    )
    assert completed["status"] == "completed"
    assert completed["actual_minutes"] == 90


@pytest.mark.asyncio
async def test_learning_snapshot_is_atomic_immutable_and_durable(store):
    now = datetime(2026, 9, 20, 15, tzinfo=UTC)
    day = timeutil.to_local(now).date()
    old_log = await store.upsert_daily_log(day, {"notes": "first reflection"})
    with pytest.raises(TypeError):
        await store.save_debrief_learning(day, {"notes": "must roll back"}, {"bad": object()})
    assert await store.get_daily_log(day) == old_log
    assert await store.get_pending_debrief_learning() == []
    attempt = await store.save_debrief_learning(day, {"notes": "second reflection"}, {
        "daily_log": {"notes": "untrusted snapshot value"},
        "conversation": [{"created_at": now, "content": "finished the pset"}],
        "metadata": {"completion_rate": 1}, "decisions": [],
    })
    await store.upsert_daily_log(day, {"notes": "later unrelated reflection"})
    pending = await Store(store.db_path).get_pending_debrief_learning(day)
    assert pending == [attempt]
    assert pending[0]["snapshot"]["daily_log"]["notes"] == "second reflection"
    assert pending[0]["snapshot"]["conversation"][0]["created_at"] == "2026-09-20T15:00:00.000000Z"
    assert pending[0]["snapshot"]["metadata"] == {"completion_rate": 1}
    await store.ack_debrief_learning(attempt["id"])
    await store.ack_debrief_learning(attempt["id"])
    assert await store.get_pending_debrief_learning() == []
    async with store.connection() as db:
        row = await (await db.execute(
            "SELECT snapshot, processed_at FROM debrief_learning_attempts WHERE id = ?", (attempt["id"],)
        )).fetchone()
    assert row["processed_at"] is not None
    assert "second reflection" in row["snapshot"]


@pytest.mark.asyncio
async def test_learning_save_cannot_restore_a_concurrently_reopened_completion(store):
    task = (await store.add_tasks([{"title": "Pset"}]))[0]
    await store.complete_task(task["id"], 30)
    stale_completed = [{"task_id": task["id"], "actual_minutes": 30}]
    await store.update_task(task["id"], {"status": "pending"})
    day = timeutil.now_local().date()
    saved = await store.save_debrief_learning(day, {"completed": stale_completed}, {
        "conversation": [], "decisions": [],
    })
    assert saved["snapshot"]["daily_log"]["completed"] == []
    assert (await store.get_daily_log(day))["completed"] == []
