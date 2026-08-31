"""Focused reminder delivery and morning-brief job coverage."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from src import jobs, timeutil
from src.store import Store


class _ReminderStore:
    def __init__(self, batches):
        self.batches = list(batches)
        self.claims = []
        self.acks = []
        self.releases = []

    async def claim_due_reminders(self, now, *, lease_for, limit):
        self.claims.append((now, lease_for, limit))
        return self.batches.pop(0) if self.batches else []

    async def ack_reminder_delivery(self, reminder_id, claim_token, delivered_at=None):
        self.acks.append((reminder_id, claim_token, delivered_at))

    async def release_reminder_delivery(
        self, reminder_id, claim_token, failed_at, retry_at
    ):
        self.releases.append(
            (reminder_id, claim_token, failed_at, retry_at)
        )


class _Telegram:
    def __init__(self, failing=()):
        self.failing = set(failing)
        self.messages = []

    async def send_message(self, text):
        if any(value in text for value in self.failing):
            raise RuntimeError("transport unavailable")
        self.messages.append(text)


def _reminder(identifier, message, due, attempts=1):
    return {
        "id": identifier,
        "message": message,
        "remind_at": due,
        "lease_token": f"claim-{identifier}",
        "delivery_attempts": attempts,
    }


@pytest.mark.asyncio
async def test_due_delivery_acks_after_send_and_late_text_names_original_time(
    monkeypatch,
):
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    store = _ReminderStore([[
        _reminder(1, "call the dentist", now - timedelta(hours=2)),
    ]])
    telegram = _Telegram()
    monkeypatch.setattr(jobs, "_is_quiet", lambda *_args: (_ for _ in ()).throw(
        AssertionError("explicit reminders must bypass quiet-hour checks")
    ))

    delivered = await jobs.deliver_due_reminders(store, telegram, now=now)

    assert delivered == 1
    assert telegram.messages[0].startswith("reminder: call the dentist")
    assert "set for" in telegram.messages[0]
    assert [item[:2] for item in store.acks] == [(1, "claim-1")]
    assert store.releases == []
    assert store.claims == [(now, timedelta(minutes=2), 20)]


@pytest.mark.asyncio
async def test_empty_due_claim_sends_nothing_for_future_reminders():
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    store = _ReminderStore([[]])
    telegram = _Telegram()

    assert await jobs.deliver_due_reminders(store, telegram, now=now) == 0
    assert telegram.messages == []
    assert store.acks == []


@pytest.mark.asyncio
async def test_delivery_failure_releases_for_retry_and_does_not_block_batch(
    monkeypatch,
):
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    failed_at = now + timedelta(seconds=3)
    monkeypatch.setattr(jobs.timeutil, "now_utc", lambda: failed_at)
    store = _ReminderStore([[
        _reminder(1, "fail this one", now, attempts=3),
        _reminder(2, "send this one", now),
    ]])
    telegram = _Telegram(failing={"fail this one"})

    delivered = await jobs.deliver_due_reminders(store, telegram, now=now)

    assert delivered == 1
    assert telegram.messages == ["reminder: send this one"]
    assert [item[:2] for item in store.acks] == [(2, "claim-2")]
    assert store.releases[0][:3] == (1, "claim-1", failed_at)
    assert store.releases[0][3] == failed_at + timedelta(seconds=120)


@pytest.mark.asyncio
async def test_second_dispatch_cannot_resend_an_already_claimed_reminder():
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    store = _ReminderStore([[
        _reminder(1, "only once", now),
    ], []])
    telegram = _Telegram()

    await jobs.deliver_due_reminders(store, telegram, now=now)
    await jobs.deliver_due_reminders(store, telegram, now=now)

    assert telegram.messages == ["reminder: only once"]
    assert len(store.acks) == 1


@pytest.mark.asyncio
async def test_real_store_delivery_is_durable_and_never_creates_calendar_rows():
    """Exercise the local persistence-to-delivery seam without external services."""
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)
    store = Store(":memory:")
    await store.initialize()
    telegram = _Telegram()
    try:
        created = await store.add_reminders([
            {"message": "call the dentist", "remind_at": now},
            {"message": "email Sam", "remind_at": now + timedelta(days=60)},
        ])

        assert await jobs.deliver_due_reminders(store, telegram, now=now) == 1
        assert telegram.messages == ["reminder: call the dentist"]
        assert (await store.get_reminder(created[0]["id"]))["status"] == "delivered"
        assert (await store.get_reminder(created[1]["id"]))["status"] == "pending"
        async with store.connection() as db:
            task_count = (await (await db.execute("SELECT count(*) FROM tasks")).fetchone())[0]
            event_count = (await (await db.execute("SELECT count(*) FROM events")).fetchone())[0]
        assert task_count == event_count == 0
    finally:
        assert store._keeper is not None
        await store._keeper.close()


@pytest.mark.asyncio
async def test_ack_failure_keeps_claim_for_expiry_recovery_without_immediate_release():
    now = datetime(2026, 8, 31, 18, 0, tzinfo=UTC)

    class AckFailingStore(_ReminderStore):
        async def ack_reminder_delivery(self, reminder_id, claim_token, delivered_at=None):
            raise RuntimeError("database temporarily unavailable")

    store = AckFailingStore([[_reminder(1, "only once for now", now)]])
    telegram = _Telegram()

    assert await jobs.deliver_due_reminders(store, telegram, now=now) == 0
    assert telegram.messages == ["reminder: only once for now"]
    assert store.releases == []


@pytest.mark.asyncio
async def test_brief_includes_overdue_today_and_next_two_days_without_claiming(
    monkeypatch,
):
    local_date = date(2026, 8, 31)
    start, _ = timeutil.day_bounds(local_date)

    class Store:
        queried = None

        async def query_reminders(self, **kwargs):
            self.queried = kwargs
            return [
                _reminder(1, "old call", start - timedelta(hours=2)),
                _reminder(2, "email Sam", start + timedelta(hours=15)),
                _reminder(3, "renew pass", start + timedelta(days=1, hours=15)),
                _reminder(4, "send form", start + timedelta(days=2, hours=15)),
            ]

        async def get_unsurfaced_decisions(self, _since):
            return []

        async def claim_due_reminders(self, *_args, **_kwargs):
            raise AssertionError("brief rendering must not claim reminders")

    store = Store()
    monkeypatch.setattr(
        jobs,
        "_brief_data",
        lambda *_args, **_kwargs: _async_value(([], [], [], [])),
    )

    text, decisions = await jobs._render_brief(store, local_date)

    assert "Reminders:" in text
    assert "overdue old call" in text
    assert "email Sam" in text
    assert "tomorrow" in text
    assert "send form" in text
    assert decisions == []
    _, expected_end = timeutil.day_bounds(local_date + timedelta(days=2))
    assert store.queried == {"status": "pending", "remind_before": expected_end}


async def _async_value(value):
    return value


@pytest.mark.asyncio
async def test_startup_catchup_dispatches_reminders_before_other_work(monkeypatch):
    order = []
    local_now = datetime(2026, 8, 31, 6, 0, tzinfo=jobs._zone())

    class Store:
        async def get_daily_log(self, _local_date):
            return None

    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(
        store=Store(), engine=object(), telegram=object()
    ))
    monkeypatch.setattr(jobs, "_scheduled_reminders", lambda: _record(order, "reminders"))
    monkeypatch.setattr(jobs, "_goal_hook", lambda *_args: _record(order, "goals"))
    monkeypatch.setattr(jobs, "_scheduled_debrief_for", lambda *_args: _record(order, "debrief"))
    monkeypatch.setattr(jobs, "_scheduled_weekly_for", lambda *_args: _record(order, "weekly"))
    monkeypatch.setattr(jobs.timeutil, "now_local", lambda: local_now)

    await jobs.run_startup_catchup()

    assert order[0] == "reminders"
    assert "goals" in order


async def _record(target, value):
    target.append(value)


def test_configure_jobs_registers_one_stable_coalescing_dispatch_job():
    captured = []

    class Scheduler:
        def add_job(self, callback, trigger, **kwargs):
            captured.append((callback, trigger, kwargs))

    class Facts:
        async def extract_from_day(self, **_kwargs):
            return []

    previous_runtime = jobs._runtime
    try:
        jobs.configure_jobs(
            Scheduler(), object(), object(), SimpleNamespace(), Facts()
        )
    finally:
        jobs._runtime = previous_runtime

    reminders = [
        item for item in captured
        if item[2]["id"] == jobs.REMINDER_DISPATCH_JOB_ID
    ]
    assert len(reminders) == 1
    callback, trigger, settings = reminders[0]
    assert callback is jobs._scheduled_reminders
    assert int(trigger.interval.total_seconds()) == 30
    assert settings["coalesce"] is True
    assert settings["max_instances"] == 1
