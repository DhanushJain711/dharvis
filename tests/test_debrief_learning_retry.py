"""Real persistence and fact gating across delayed, retried debrief processing."""

import asyncio
import json
from contextlib import suppress
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src import jobs, timeutil
from src.facts_engine import FactsEngine
from src.store import Store


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_runtime", None)


async def make_store(tmp_path):
    store = Store(tmp_path / "learning.sqlite")
    await store.initialize()
    return store


def checklist(local_date, task_ids=(), **extra):
    return {
        "callback_prefix": f"daily-debrief:{local_date}",
        "checklist_id": "day-1",
        "items": [{"checked": True, "value": {"task_id": task_id}} for task_id in task_ids],
        **extra,
    }


class FactResponses:
    def __init__(self):
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        daily_log = json.loads(kwargs["input"])["daily_log"]
        return SimpleNamespace(output_text=json.dumps({
            "facts": [{
                "content": "Afternoon blocks support focused writing",
                "category": "work", "confidence": 0.9,
                "evidence": daily_log.get("notes") or "Writing completed after lunch today",
                "contradicts_fact_ids": [],
            }],
            "contradictions": [],
        }), usage=None)


async def test_committed_extraction_ack_failures_restart_and_new_chat_do_not_add_evidence(
    tmp_path, monkeypatch,
):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    await store.append_message("user", "I wrote after lunch", [], "day")
    await jobs.handle_debrief_submission(store, object(), object(), checklist(local_date))
    original = (await store.get_pending_debrief_learning())[0]
    responses = FactResponses()
    ack = Store.ack_debrief_learning
    attempts = 0

    async def intermittent_ack(self, attempt_id):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("database acknowledgement interrupted")
        await ack(self, attempt_id)

    monkeypatch.setattr(Store, "ack_debrief_learning", intermittent_ack)
    for index in range(3):
        # Both engines and Store are fresh, as after a process restart.
        recovered = Store(store.db_path)
        facts = FactsEngine(recovered, client=SimpleNamespace(responses=responses))
        monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=recovered, facts_engine=facts))
        await jobs._scheduled_debrief_learning()
        learned = (await recovered.query_facts(active=None))[0]
        assert learned["evidence_count"] == 1
        assert not learned["active"]
        await recovered.append_message("user", f"Later unrelated message {index}", [], "day")
        if index < 2:
            assert (await recovered.get_pending_debrief_learning())[0] == original

    assert len(responses.calls) == 1
    payload = json.loads(responses.calls[0]["input"])
    assert [row["content"] for row in payload["conversation"]] == ["I wrote after lunch"]
    assert await store.get_pending_debrief_learning() == []


async def test_same_day_followups_retain_evidence_without_manufacturing_repetition(tmp_path):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    responses = FactResponses()
    facts = FactsEngine(store, client=SimpleNamespace(responses=responses))
    event = checklist(local_date)
    await jobs.handle_debrief_submission(store, facts, object(), event)
    await jobs._retry_debrief_learning(store, facts, local_date)
    for reflection in ("The afternoon worked well", "Lunch helped me focus"):
        await jobs.handle_debrief_submission(store, facts, object(), {**event, "response": reflection})
        await jobs._retry_debrief_learning(store, facts, local_date)
    learned = (await store.query_facts(active=None))[0]
    assert learned["evidence_count"] == 1
    assert not learned["active"]
    evidence = await facts.explain_fact(learned["id"])
    assert "Lunch helped me focus" in str(evidence)
    assert len(responses.calls) == 3
    # Independent days still pass the ordinary three-observation gate.
    for offset in (1, 2):
        await facts.extract_from_day({"date": local_date + timedelta(days=offset)}, [], [])
    learned = (await store.query_facts(active=None))[0]
    assert learned["evidence_count"] == 3
    assert learned["active"]


async def test_hanging_learning_worker_does_not_block_saved_done_or_followup(tmp_path, monkeypatch):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    started = asyncio.Event()

    class HangingFacts:
        async def extract_from_day(self, **kwargs):
            started.set()
            await asyncio.Event().wait()

    facts = HangingFacts()
    event = checklist(local_date)
    await asyncio.wait_for(jobs.handle_debrief_submission(store, facts, object(), event), 1)
    assert not started.is_set()
    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=store, facts_engine=facts))
    worker = asyncio.create_task(jobs._scheduled_debrief_learning())
    try:
        await asyncio.wait_for(started.wait(), 1)
        await asyncio.wait_for(jobs.handle_debrief_submission(
            store, facts, object(), {**event, "response": "I preferred the afternoon"},
        ), 1)
        assert len(await store.get_pending_debrief_learning()) == 2
    finally:
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker


@pytest.mark.parametrize("explicit_minutes,goal_hours", [(None, 1.0), (90, 1.5)])
async def test_partial_progress_remaining_block_is_not_final_actual_duration(
    tmp_path, explicit_minutes, goal_hours,
):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    goal = await store.add_goal({
        "title": "Writing", "target_amount": 3, "target_unit": "hours",
        "period": "week", "category": "work",
    })
    task = (await store.add_tasks([{"title": "Essay", "goal_id": goal["id"]}]))[0]
    await store.log_task_progress(task["id"], total_minutes=60, remaining_minutes=30)
    now = timeutil.now_utc()
    await store.apply_schedule_decision(
        task["id"], "scheduled", now, now + timedelta(minutes=30), None, None,
        "daily_plan", "The remaining half hour fits before the deadline.", [], None,
    )
    await store.upsert_daily_log(local_date, {"planned": [{"task_id": task["id"]}]})
    event = checklist(local_date, [task["id"]])
    if explicit_minutes is not None:
        event["items"][0]["value"]["actual_minutes"] = explicit_minutes
    await jobs.handle_debrief_submission(store, object(), object(), event)
    completed = await store.get_task(task["id"])
    assert completed["status"] == "completed"
    assert completed["actual_minutes"] == explicit_minutes
    assert completed["progress_minutes"] == 60
    progress = await store.get_goal_progress(goal["id"], timeutil.day_bounds(local_date)[0])
    assert progress["amount_done"] == goal_hours


async def test_stale_dropped_and_reopened_selections_do_not_block_other_completions(tmp_path):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    dropped, reopened, valid = await store.add_tasks([
        {"title": "Dropped"}, {"title": "Reopened"}, {"title": "Still wanted"},
    ])
    issued_at = timeutil.now_utc()
    ids = [task["id"] for task in (dropped, reopened, valid)]
    await store.upsert_daily_log(local_date, {
        "planned": [{"task_id": task_id} for task_id in ids], "debrief_sent_at": issued_at,
    })
    await store.drop_task(dropped["id"])
    await store.complete_task(reopened["id"])
    await store.update_task(reopened["id"], {"status": "pending"})
    await jobs.handle_debrief_submission(store, object(), object(), checklist(
        local_date, ids, created_at=issued_at.isoformat(),
    ))
    assert (await store.get_task(dropped["id"]))["status"] == "dropped"
    assert (await store.get_task(reopened["id"]))["status"] == "pending"
    assert (await store.get_task(valid["id"]))["status"] == "completed"
    log = await store.get_daily_log(local_date)
    assert [row["task_id"] for row in log["completed"]] == [valid["id"]]


async def test_reopening_invalidates_queued_completion_observation(tmp_path):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    task = (await store.add_tasks([{"title": "Essay"}]))[0]
    await store.upsert_daily_log(local_date, {"planned": [{"task_id": task["id"]}]})
    responses = FactResponses()
    facts = FactsEngine(store, client=SimpleNamespace(responses=responses))
    await jobs.handle_debrief_submission(store, facts, object(), checklist(local_date, [task["id"]]))
    await store.update_task(task["id"], {"status": "pending"})
    await jobs._retry_debrief_learning(store, facts, local_date)
    assert not responses.calls
    assert await store.query_facts(active=None) == []
    assert await store.get_pending_debrief_learning() == []


async def test_reopen_during_save_keeps_learning_completion_totals_consistent(tmp_path, monkeypatch):
    store = await make_store(tmp_path)
    local_date = timeutil.now_local().date()
    task = (await store.add_tasks([{"title": "Essay"}]))[0]
    await store.upsert_daily_log(local_date, {"planned": [{"task_id": task["id"]}]})
    save = store.save_debrief_learning

    async def reopen_before_save(day, changes, snapshot):
        await store.update_task(task["id"], {"status": "pending"})
        return await save(day, changes, snapshot)

    monkeypatch.setattr(store, "save_debrief_learning", reopen_before_save)
    responses = FactResponses()
    facts = FactsEngine(store, client=SimpleNamespace(responses=responses))
    await jobs.handle_debrief_submission(store, facts, object(), checklist(local_date, [task["id"]]))
    await jobs._retry_debrief_learning(store, facts, local_date)
    log = json.loads(responses.calls[0]["input"])["daily_log"]
    assert log["actual"] == []
    assert log["completion_rate"] == 0
    assert log["checklist_completion_rate"] == 0
