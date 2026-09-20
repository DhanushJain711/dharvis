"""Saved debrief outcomes survive integration outages; local clocks stay readable."""

import asyncio
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

from src import jobs, timeutil
from src.store import Store


@pytest.fixture(autouse=True)
def isolated_jobs(monkeypatch):
    monkeypatch.setattr(jobs, "_runtime", None)
    monkeypatch.setattr(jobs, "_notification_times", {})
    monkeypatch.setenv("QUIET_HOURS_START", "22:00")
    monkeypatch.setenv("QUIET_HOURS_END", "07:00")


async def _store(tmp_path):
    store = Store(tmp_path / "reliable-jobs.sqlite")
    await store.initialize()
    return store


class Facts:
    def __init__(self, *, failing=False):
        self.failing = failing
        self.calls = []

    async def extract_from_day(self, **kwargs):
        self.calls.append(kwargs)
        if self.failing:
            raise RuntimeError("model unavailable")
        return []


async def test_learning_failure_keeps_done_marker_and_retries_day_evidence_after_restart(
    tmp_path, monkeypatch,
):
    store = await _store(tmp_path)
    local_date = timeutil.now_local().date()
    task = (await store.add_tasks([{"title": "pset", "estimated_minutes": 40}]))[0]
    await store.upsert_daily_log(local_date, {"planned": [{"task_id": task["id"]}]})
    await store.append_message("user", "I worked on the pset after lunch", [], "day")
    event = {"callback_prefix": f"daily-debrief:{local_date}", "checklist_id": "retry",
        "items": [{"checked": True, "value": {"task_id": task["id"]}}]}
    failing = Facts(failing=True)

    await jobs.handle_debrief_submission(store, failing, object(), event)
    assert (await store.get_task(task["id"]))["status"] == "completed"
    log = await store.get_daily_log(local_date)
    assert "[debrief-checklist:retry]" in log["notes"]
    assert len(await store.get_pending_debrief_learning(local_date)) == 1
    assert not failing.calls
    await jobs._retry_debrief_learning(store, failing, local_date)
    await jobs.handle_debrief_submission(store, failing, object(), event)
    assert len(failing.calls) == 1
    assert len((await store.get_daily_log(local_date))["completed"]) == 1

    # A fresh process has no in-memory callback payload; durable day state is
    # sufficient even when restart happens more than the old one-week window later.
    recovered = Store(store.db_path)
    facts = Facts()
    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=recovered, facts_engine=facts))
    monkeypatch.setattr(jobs.timeutil, "now_local", lambda: datetime(2027, 1, 4, 12, tzinfo=ZoneInfo("America/Chicago")))
    await jobs._scheduled_debrief_learning()
    assert len(facts.calls) == 1
    payload = facts.calls[0]
    assert payload["daily_log"]["date"] == local_date
    assert payload["daily_log"]["actual"][0]["task_id"] == task["id"]
    assert [row["content"] for row in payload["conversation"]] == ["I worked on the pset after lunch"]
    assert await recovered.get_pending_debrief_learning(local_date) == []
    await jobs._scheduled_debrief_learning()
    assert len(facts.calls) == 1


async def test_followup_failure_does_not_undo_done_and_preserves_pending_question(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    local_date = timeutil.now_local().date()
    tasks = await store.add_tasks([{"title": "A"}, {"title": "B"}])
    await store.upsert_daily_log(local_date, {"planned": [{"task_id": t["id"]} for t in tasks]})
    class Telegram:
        async def send_message(self, text):
            raise RuntimeError("telegram down")
    monkeypatch.setattr(jobs, "_is_quiet", lambda *_: False)
    await jobs.handle_debrief_submission(store, Facts(), Telegram(), {
        "callback_prefix": f"daily-debrief:{local_date}", "checklist_id": "followup", "items": []})
    log = await store.get_daily_log(local_date)
    assert "[debrief-checklist:followup]" in log["notes"]
    assert "[debrief-followup:followup:miss:pending]" in log["notes"]


async def test_notification_times_persist_reconfigure_only_daily_jobs_and_keep_sent_markers(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    scheduler = AsyncIOScheduler(timezone=jobs._zone())
    jobs.configure_jobs(scheduler, store, object(), object(), Facts())
    ids_before = {job.id for job in scheduler.get_jobs()}
    evening_date = timeutil.now_local().date()
    await store.upsert_daily_log(evening_date, {"brief_sent_at": timeutil.now_utc()})
    saved = await jobs.update_notification_times(store, morning="09:15", evening="20:45")
    assert saved == {"morning": "09:15", "evening": "20:45"}
    assert {job.id for job in scheduler.get_jobs()} == ids_before
    # A stopped scheduler queues replace_existing jobs until it starts; the
    # last occurrence is the one it will persist/start with.
    morning = [job for job in scheduler.get_jobs() if job.id == jobs.MORNING_JOB_ID][-1]
    assert str(morning.trigger.fields[5]) == "9"
    assert str(morning.trigger.fields[6]) == "15"
    assert str(morning.trigger.timezone) == jobs.config.USER_TIMEZONE
    assert (await store.get_daily_log(evening_date))["brief_sent_at"] is not None
    await jobs.update_notification_times(store, morning="10:00")
    assert (await store.get_notification_times())["evening"] == "20:45"
    monkeypatch.setattr(jobs, "_notification_times", {})
    await jobs.load_notification_times(Store(store.db_path))
    assert jobs._notification_clock("morning") == (10, 0)
    reference = datetime(2026, 9, 21, 9, 50, tzinfo=jobs._zone())
    assert jobs._inside_brief_coalesce_window(reference)
    assert not jobs._inside_brief_coalesce_window(reference - timedelta(hours=2))


@pytest.mark.parametrize("value", ["24:00", "9:30", "nonsense", "22:00", "06:59"])
async def test_invalid_or_quiet_notification_clocks_do_not_persist(tmp_path, value):
    store = await _store(tmp_path)
    before = await store.get_notification_times()
    with pytest.raises(ValueError, match="local time|quiet hours"):
        await jobs.update_notification_times(store, morning="09:00", evening=value)
    assert await store.get_notification_times() == before


async def test_brief_uses_local_times_in_reasons_without_duplicate_assignment_or_filler(tmp_path):
    store = await _store(tmp_path)
    local_date = date(2026, 9, 21)
    start = datetime(2026, 9, 21, 15, tzinfo=jobs._zone()).astimezone(UTC)
    task = (await store.add_tasks([{"title": "math pset"}]))[0]
    await store.apply_schedule_decision(task["id"], "scheduled", start, start + timedelta(hours=2),
        None, None, "daily_plan",
        "Put math pset at 3 — the only two-hour gap before the deadline at 2026-09-22T01:00:00Z.",
        [], "block")
    text, decisions = await jobs._render_brief(store, local_date)
    assert "3pm–5pm · math pset — the only two-hour gap" in text
    assert "Mon Sep 21 at 8pm" in text
    assert "2026-" not in text
    assert text.count("math pset") == 1
    assert "Protect" not in text and "fixed events" not in text
    assert decisions
    await store.mark_decision_surfaced(decisions[0])
    text, decisions = await jobs._render_brief(store, local_date)
    assert "the only two-hour gap" in text
    assert decisions == []


def test_human_times_distinguish_midnight_and_repeated_dst_hour():
    zone = ZoneInfo("America/Chicago")
    assert jobs._format_span(datetime(2026, 9, 21, 23, tzinfo=zone), datetime(2026, 9, 22, 0, tzinfo=zone)) == "11pm–Tue 12am"
    assert jobs._format_span(datetime(2026, 11, 1, 1, tzinfo=zone, fold=0), datetime(2026, 11, 1, 1, tzinfo=zone, fold=1)) == "1am CDT–1am CST"


async def test_notification_edits_replace_live_jobs_and_preserve_local_clock_across_dst(tmp_path):
    store = await _store(tmp_path)
    scheduler = AsyncIOScheduler(timezone=jobs._zone())
    jobs.configure_jobs(scheduler, store, object(), object(), Facts())
    scheduler.start(paused=True)
    try:
        total = len(scheduler.get_jobs())
        for clock in ("08:00", "09:00", "08:30"):
            await jobs.update_notification_times(store, morning=clock)
        assert len(scheduler.get_jobs()) == total
        trigger = scheduler.get_job(jobs.MORNING_JOB_ID).trigger
        before = datetime(2026, 3, 7, 0, tzinfo=jobs._zone())
        first = trigger.get_next_fire_time(None, before)
        second = trigger.get_next_fire_time(first, first + timedelta(seconds=1))
        assert first.hour == second.hour == 8
        assert first.minute == second.minute == 30
        assert first.utcoffset() != second.utcoffset()
    finally:
        scheduler.shutdown(wait=False)


async def test_notification_clocks_and_nine_unique_jobs_survive_persistent_scheduler_restart(
    tmp_path, monkeypatch,
):
    store = await _store(tmp_path)
    jobs_path = tmp_path / "persistent-jobs.sqlite"
    expected_ids = {
        jobs.PLANNING_JOB_ID, jobs.MORNING_JOB_ID, jobs.DEBRIEF_JOB_ID,
        jobs.WEEKLY_JOB_ID, jobs.RECONCILE_JOB_ID, jobs.REMINDER_DISPATCH_JOB_ID,
        jobs.BACKUP_JOB_ID, jobs.NIGHTLY_FACTS_JOB_ID, jobs.FACTS_RETRY_JOB_ID,
    }

    def scheduler_from_disk():
        return AsyncIOScheduler(
            timezone=jobs._zone(),
            jobstores={"default": SQLAlchemyJobStore(url=f"sqlite:///{jobs_path}")},
        )

    scheduler = scheduler_from_disk()
    jobs.configure_jobs(scheduler, store, object(), object(), Facts())
    scheduler.start(paused=True)
    try:
        await jobs.update_notification_times(store, morning="09:15", evening="20:45")
        assert {job.id for job in scheduler.get_jobs()} == expected_ids
    finally:
        scheduler.shutdown(wait=False)
        await asyncio.sleep(0)

    monkeypatch.setattr(jobs, "_runtime", None)
    monkeypatch.setattr(jobs, "_notification_times", {})
    recovered = Store(store.db_path)
    await jobs.load_notification_times(recovered)
    restarted = scheduler_from_disk()
    jobs.configure_jobs(restarted, recovered, object(), object(), Facts())
    restarted.start(paused=True)
    try:
        registered = restarted.get_jobs()
        assert len(registered) == len(expected_ids)
        assert {job.id for job in registered} == expected_ids
        assert all(job.coalesce and job.max_instances == 1 for job in registered)
        for job_id, hour, minute in (
            (jobs.MORNING_JOB_ID, "9", "15"),
            (jobs.DEBRIEF_JOB_ID, "20", "45"),
            (jobs.PLANNING_JOB_ID, "9", "0"),
        ):
            trigger = restarted.get_job(job_id).trigger
            assert str(trigger.fields[5]) == hour
            assert str(trigger.fields[6]) == minute
            assert str(trigger.timezone) == jobs.config.USER_TIMEZONE
    finally:
        restarted.shutdown(wait=False)
        await asyncio.sleep(0)


async def test_change_summary_retains_new_placement_and_local_cause():
    start = datetime(2026, 9, 21, 20, tzinfo=UTC)
    text = await jobs._format_change({"action": "moved", "start": start,
        "end": start + timedelta(hours=1),
        "reasoning": "the new meeting took your earlier gap"}, {"title": "pset"})
    assert text == "moved to Mon 3pm–4pm — the new meeting took your earlier gap"


async def test_weekly_review_reports_concrete_outcomes_without_inferred_behavior(tmp_path, monkeypatch):
    store = await _store(tmp_path)
    sunday = date(2026, 9, 20)
    tasks = await store.add_tasks([{"title": "finish pset"}, {"title": "read chapter"}])
    planned = [{"task_id": t["id"]} for t in tasks]
    await store.upsert_daily_log(sunday - timedelta(days=1), {"planned": planned})
    await store.upsert_daily_log(sunday, {"planned": planned, "completed": [planned[0]]})
    class Telegram:
        messages = []
        async def send_message(self, text):
            self.messages.append(text)
    telegram = Telegram()
    monkeypatch.setattr(jobs, "_is_quiet", lambda *_: False)
    await jobs.send_weekly_review(store, telegram, sunday)
    assert "1 of 2 planned tasks" in telegram.messages[0]
    assert "Still open: read chapter" in telegram.messages[0]
    assert "behavior" not in telegram.messages[0]
    assert "signal" not in telegram.messages[0]
    await jobs.send_weekly_review(store, telegram, sunday)
    assert len(telegram.messages) == 1
