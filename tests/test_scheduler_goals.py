"""Goal quota materialization regressions through the real scheduler path."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from src.scheduler_engine import SchedulerEngine, _goal_period_bounds
from src.store import Store
from src import timeutil


class EmptyCalendar:
    _last_query_complete = True

    async def list_events(self, start, end, *, force_refresh=False):
        return []

    async def delete_work_block(self, event_id):
        raise AssertionError("no existing goal block should need cleanup")


class RepairCalendar:
    """A Calendar double that retains owned remote work blocks until deleted."""

    _last_query_complete = True

    def __init__(self, *, failed_deletes: int = 0):
        self.events: list[dict] = []
        self.failed_deletes = failed_deletes
        self.deleted: list[str] = []

    async def list_events(self, start, end, *, force_refresh=False):
        return list(self.events)

    async def create_work_block(self, task_id, title, start, end, reasoning, **kwargs):
        event_id = f"orphan-{task_id}"
        self.events.append({
            "id": event_id, "gcal_event_id": event_id, "title": title,
            "start_time": start, "end_time": end, "kalendra_owned": True,
            "kalendra_kind": "task-block",
            "extended_properties": {"private": {"task_id": str(task_id)}},
        })
        return event_id

    async def delete_work_block(self, event_id):
        self.deleted.append(event_id)
        if self.failed_deletes:
            self.failed_deletes -= 1
            raise RuntimeError("calendar delete unavailable")
        self.events[:] = [event for event in self.events if event["id"] != event_id]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("target_amount", "target_unit", "session_minutes", "expected"),
    [
        (3, "sessions", 40, [(40, 1.0), (40, 1.0), (40, 1.0)]),
        (2.5, "hours", 60, [(60, 1.0), (60, 1.0), (30, 0.5)]),
    ],
)
async def test_goal_reconciliation_materializes_stable_quota_items_on_retry(
    tmp_path,
    target_amount,
    target_unit,
    session_minutes,
    expected,
):
    store = Store(tmp_path / "goals.sqlite")
    await store.initialize()
    goal = await store.add_goal({
        "title": "Practice", "target_amount": target_amount,
        "target_unit": target_unit, "period": "week", "category": "fitness",
        "session_minutes": session_minutes, "scheduling_enabled": True,
    })
    engine = SchedulerEngine(store, EmptyCalendar(), client=object())
    # This test owns materialization, not model ranking or calendar placement.
    engine._plan_assignments = AsyncMock(return_value=[])
    reference = timeutil.now_local().date()

    assert await engine.reconcile_goal_schedule(reference) == []
    assert await engine.reconcile_goal_schedule(reference) == []

    start, end = _goal_period_bounds(goal, reference)
    items = await store.get_goal_schedule_items(goal["id"], start, end)
    assert [(item["task"]["estimated_minutes"], item["planned_amount"]) for item in items] == expected
    assert [item["ordinal"] for item in items] == list(range(1, len(expected) + 1))
    assert all(item["task"]["estimate_source"] == "goal" for item in items)


def test_goal_week_period_bounds_follow_local_dst_transition(monkeypatch):
    chicago = timeutil.ZoneInfo("America/Chicago")
    monkeypatch.setattr(timeutil, "_local_zone", lambda: chicago)
    goal = {"period": "week"}

    start, end = _goal_period_bounds(goal, datetime(2026, 3, 10, 12, tzinfo=UTC))

    assert start == datetime(2026, 3, 9, 5, tzinfo=UTC)
    assert end == datetime(2026, 3, 16, 5, tzinfo=UTC)


async def _repair_signals(store: Store) -> list[dict]:
    return [
        json.loads(message["content"])
        for message in await store.get_messages("scheduler-fact-signals")
    ]


@pytest.mark.asyncio
async def test_schedule_store_failure_and_failed_rollback_are_repaired_by_reconciliation(
    tmp_path, monkeypatch,
):
    store = Store(tmp_path / "create-repair.sqlite")
    await store.initialize()
    task = (await store.add_tasks([{"title": "Report", "estimated_minutes": 30}]))[0]
    calendar = RepairCalendar(failed_deletes=1)
    engine = SchedulerEngine(store, calendar, client=object())
    engine._actual_free = AsyncMock(return_value=True)
    original_apply = store.apply_schedule_decision

    async def reject_persistence(*args, **kwargs):
        raise RuntimeError("sqlite write failed")

    monkeypatch.setattr(store, "apply_schedule_decision", reject_persistence)
    start = datetime.now(UTC).replace(second=0, microsecond=0) + timedelta(days=1)
    end = start + timedelta(minutes=30)
    with pytest.raises(RuntimeError, match="sqlite write failed"):
        await engine.schedule_task(
            task["id"], start, end, "the 30-minute block fits the report", "daily_plan"
        )

    assert (await store.get_task(task["id"]))["status"] == "pending"
    assert [signal["kind"] for signal in await _repair_signals(store)] == [
        "calendar_sqlite_repair_required"
    ]
    assert calendar.events and calendar.events[0]["id"] == f"orphan-{task['id']}"

    monkeypatch.setattr(store, "apply_schedule_decision", original_apply)
    assert await engine.detect_conflicts(start, end) == []
    assert calendar.events == []
    assert calendar.deleted == [f"orphan-{task['id']}", f"orphan-{task['id']}"]


@pytest.mark.asyncio
async def test_unschedule_compensation_failure_leaves_repairable_orphan(tmp_path, monkeypatch):
    store = Store(tmp_path / "unschedule-repair.sqlite")
    await store.initialize()
    task = (await store.add_tasks([{"title": "Report", "estimated_minutes": 30}]))[0]
    start = datetime.now(UTC).replace(second=0, microsecond=0)
    start += timedelta(days=1)
    end = start + timedelta(minutes=30)
    event_id = f"orphan-{task['id']}"
    await store.apply_schedule_decision(
        task["id"], "scheduled", start, end, None, None, "daily_plan",
        "The 30-minute block fits the report.", [], event_id,
    )
    calendar = RepairCalendar(failed_deletes=1)
    await calendar.create_work_block(task["id"], "Report", start, end, "reason")
    engine = SchedulerEngine(store, calendar, client=object())
    original_apply = store.apply_schedule_decision
    calls = 0

    async def fail_only_compensation(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("sqlite compensation failed")
        return await original_apply(*args, **kwargs)

    monkeypatch.setattr(store, "apply_schedule_decision", fail_only_compensation)
    with pytest.raises(RuntimeError, match="sqlite compensation failed"):
        await engine._unschedule_locked(
            await store.get_task(task["id"]), "the block conflicts", "conflict"
        )

    assert (await store.get_task(task["id"]))["status"] == "pending"
    assert [signal["kind"] for signal in await _repair_signals(store)] == [
        "calendar_sqlite_repair_required"
    ]
    assert calendar.events

    monkeypatch.setattr(store, "apply_schedule_decision", original_apply)
    assert await engine.detect_conflicts(start, end) == []
    assert calendar.events == []
