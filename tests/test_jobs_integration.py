from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import jobs as jobs_module
from src.facts_engine import FactsEngine
from src.jobs import (
    _render_brief,
    handle_debrief_submission,
    reconcile_calendar,
    run_daily_planning,
)
from src.store import Store
from src import timeutil


class EngineSpy:
    def __init__(self) -> None:
        self.detected = False

    async def detect_conflicts(self, start, end):
        self.detected = True
        return []

    async def resolve_conflicts(self, start, end):
        raise AssertionError("reconciliation must inspect conflicts before rescheduling")


async def test_reconcile_uses_conflict_detection_not_blanket_reschedule():
    engine = EngineSpy()
    await reconcile_calendar(engine)
    assert engine.detected


class GoalEngineSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    async def refresh_goal_plan(self, *, reference):
        self.calls.append(("refresh", reference))
        return []

    async def replan_missed_goal_sessions(self, *, reference):
        self.calls.append(("replan", reference))
        return []

    async def plan_day(self, local_date):
        self.calls.append(("plan", local_date))
        return []

    async def detect_conflicts(self, start, end):
        self.calls.append(("conflicts", start))
        return []


@pytest.mark.asyncio
async def test_goal_lifecycle_hooks_use_concrete_scheduler_methods():
    engine = GoalEngineSpy()
    local_date = timeutil.now_local().date()

    await run_daily_planning(engine, local_date)
    await reconcile_calendar(engine)

    assert engine.calls[:2] == [("refresh", local_date), ("plan", local_date)]
    assert engine.calls[2][0] == "replan"
    assert engine.calls[3][0] == "conflicts"


class _FactsResponses:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(
            output_text='{"facts": [], "contradictions": []}', usage=None
        )


@pytest.mark.asyncio
async def test_debrief_extracts_real_facts_engine_with_bounded_evidence_and_later_answer(
    tmp_path,
):
    store = Store(tmp_path / "jobs.sqlite")
    await store.initialize()
    local_date = timeutil.now_local().date()
    now = datetime.now(UTC)
    task = (await store.add_tasks([{"title": "write report"}]))[0]
    await store.apply_schedule_decision(
        task_id=task["id"], action="scheduled", start=now,
        end=now + timedelta(minutes=30), previous_start=None, previous_end=None,
        trigger="daily_plan", reasoning="It fits the first open half hour.",
        facts_used=[], gcal_event_id="work-1",
    )
    await store.append_message("user", "I had more energy after lunch.", [], "day")
    responses = _FactsResponses()
    facts = FactsEngine(store, client=SimpleNamespace(responses=responses))
    event = {
        "callback_prefix": f"daily-debrief:{local_date.isoformat()}",
        "checklist_id": "day-1", "items": [],
    }

    await handle_debrief_submission(store, facts, object(), event, "day")

    first_payload = json.loads(responses.calls[0]["input"])
    assert first_payload["daily_log"]["date"] == local_date.isoformat()
    assert [item["content"] for item in first_payload["conversation"]] == [
        "I had more energy after lunch."
    ]
    assert len(first_payload["schedule_decisions"]) == 1

    await handle_debrief_submission(
        store, facts, object(), {**event, "response": "The afternoon worked well."}, "day"
    )

    assert len(responses.calls) == 2
    second_payload = json.loads(responses.calls[1]["input"])
    assert second_payload["daily_log"]["notes"] == "The afternoon worked well."
    assert "Debrief response: The afternoon worked well." in (
        await store.get_daily_log(local_date)
    )["notes"]


@pytest.mark.asyncio
async def test_morning_brief_includes_complete_external_calendar_view_without_mirrors(
    tmp_path,
):
    store = Store(tmp_path / "brief.sqlite")
    await store.initialize()
    local_date = timeutil.now_local().date()
    start, end = timeutil.day_bounds(local_date)
    await store.add_events([{
        "title": "Local meeting", "start_time": start + timedelta(hours=9),
        "end_time": start + timedelta(hours=10), "gcal_event_id": "duplicate",
    }])

    class Calendar:
        _last_query_complete = True

        async def list_events(self, _start, _end):
            assert (_start, _end) == (start, end)
            return [
                {
                    "id": "duplicate", "title": "Local meeting",
                    "start_time": (start + timedelta(hours=9)).isoformat(),
                    "end_time": (start + timedelta(hours=10)).isoformat(),
                },
                {
                    "id": "holiday", "title": "Holiday", "all_day": True,
                    "start_time": start.isoformat(), "end_time": end.isoformat(),
                },
                *[
                    {
                        "id": f"external-{index}", "title": f"External {index}",
                        "start_time": (start + timedelta(hours=index)).isoformat(),
                        "end_time": (start + timedelta(hours=index, minutes=30)).isoformat(),
                    }
                    for index in range(1, 6)
                ],
            ]

    text, _ = await _render_brief(store, local_date, calendar=Calendar())

    assert "Events:" in text
    assert "all day Holiday" in text
    assert text.count("Local meeting") == 1
    assert "+2 more" in text


class _FactsSpy:
    async def extract_from_day(self, *, daily_log, conversation, decisions):
        return []


@pytest.mark.asyncio
async def test_debrief_deletes_owned_block_and_accepts_only_stored_checklist_tasks(
    tmp_path, monkeypatch,
):
    store = Store(tmp_path / "completion.sqlite")
    await store.initialize()
    local_date = timeutil.now_local().date()
    now = datetime.now(UTC)
    goal = await store.add_goal({
        "title": "Practice", "target_amount": 2, "target_unit": "sessions",
        "period": "week", "category": "personal",
    })
    scheduled = (await store.add_tasks([{"title": "Practice", "goal_id": goal["id"]}]))[0]
    forged = (await store.add_tasks([{"title": "Forged"}]))[0]
    await store.apply_schedule_decision(
        scheduled["id"], "scheduled", now, now + timedelta(minutes=30), None, None,
        "goal_quota", "It is the planned practice session.", [], "owned-block",
    )
    await store.upsert_daily_log(local_date, {"planned": [{
        "task_id": scheduled["id"], "checklist_included": True,
    }]})

    class Calendar:
        deleted: list[str] = []

        async def delete_work_block(self, event_id):
            self.deleted.append(event_id)

    calendar = Calendar()
    monkeypatch.setattr(
        jobs_module, "_runtime", SimpleNamespace(engine=SimpleNamespace(calendar=calendar))
    )
    event = {
        "callback_prefix": f"daily-debrief:{local_date.isoformat()}",
        "checklist_id": "completion", "items": [
            {"checked": True, "value": {"task_id": scheduled["id"], "actual_minutes": 25}},
            {"checked": True, "value": {"task_id": forged["id"], "actual_minutes": 25}},
        ],
    }

    await handle_debrief_submission(store, _FactsSpy(), object(), event)

    completed = await store.get_task(scheduled["id"])
    untouched = await store.get_task(forged["id"])
    assert calendar.deleted == ["owned-block"]
    assert completed["status"] == "completed"
    assert completed["gcal_event_id"] is None
    assert completed["actual_minutes_source"] == "debrief"
    assert untouched["status"] == "pending"
    async with store.connection() as db:
        row = await (await db.execute(
            "SELECT task_id FROM goal_progress WHERE goal_id = ?", (goal["id"],)
        )).fetchone()
    assert row["task_id"] == scheduled["id"]

    await handle_debrief_submission(store, _FactsSpy(), object(), event)
    async with store.connection() as db:
        row = await (await db.execute(
            "SELECT COUNT(*) AS count FROM goal_progress WHERE goal_id = ?", (goal["id"],)
        )).fetchone()
    assert row["count"] == 1


@pytest.mark.asyncio
async def test_debrief_keeps_scheduled_task_retryable_when_block_delete_fails(
    tmp_path, monkeypatch,
):
    store = Store(tmp_path / "delete-failure.sqlite")
    await store.initialize()
    local_date = timeutil.now_local().date()
    now = datetime.now(UTC)
    task = (await store.add_tasks([{"title": "Scheduled"}]))[0]
    await store.apply_schedule_decision(
        task["id"], "scheduled", now, now + timedelta(minutes=30), None, None,
        "daily_plan", "It is the first safe opening.", [], "owned-block",
    )
    await store.upsert_daily_log(local_date, {"planned": [{
        "task_id": task["id"], "checklist_included": True,
    }]})

    class FailingCalendar:
        async def delete_work_block(self, event_id):
            raise RuntimeError("calendar outage")

    monkeypatch.setattr(
        jobs_module, "_runtime",
        SimpleNamespace(engine=SimpleNamespace(calendar=FailingCalendar())),
    )
    with pytest.raises(RuntimeError, match="calendar outage"):
        await handle_debrief_submission(store, _FactsSpy(), object(), {
            "callback_prefix": f"daily-debrief:{local_date.isoformat()}",
            "checklist_id": "delete-failure",
            "items": [{"checked": True, "value": {"task_id": task["id"]}}],
        })

    retained = await store.get_task(task["id"])
    assert retained["status"] == "scheduled"
    assert retained["gcal_event_id"] == "owned-block"
