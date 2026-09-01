"""Reminder persistence, lifecycle, and migration coverage."""

import asyncio
from datetime import UTC, datetime, timedelta
from typing import AsyncIterator
from zoneinfo import ZoneInfo

import aiosqlite
import pytest

from src.migrate import run_migrations
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
async def test_add_query_and_update_reminders_are_utc_safe(store: Store) -> None:
    chicago = ZoneInfo("America/Chicago")
    first_local = datetime(2026, 10, 31, 9, 15, tzinfo=chicago)
    second_local = datetime(2026, 11, 1, 9, 15, tzinfo=chicago)

    created = await store.add_reminders(
        [
            {"message": "Call the dentist", "remind_at": first_local},
            {"message": "Email the recruiter", "remind_at": second_local},
        ]
    )

    assert [item["message"] for item in created] == [
        "Call the dentist",
        "Email the recruiter",
    ]
    assert created[0]["remind_at"] == datetime(2026, 10, 31, 14, 15, tzinfo=UTC)
    assert created[1]["remind_at"] == datetime(2026, 11, 1, 15, 15, tzinfo=UTC)
    assert all(item["status"] == "pending" for item in created)
    assert all(item["next_attempt_at"] == item["remind_at"] for item in created)

    queried = await store.query_reminders(
        status="pending",
        remind_after=datetime(2026, 10, 31, 14, tzinfo=UTC),
        remind_before=datetime(2026, 11, 1, 16, tzinfo=UTC),
    )
    assert [item["id"] for item in queried] == [item["id"] for item in created]

    moved = datetime(2026, 11, 2, 10, tzinfo=chicago)
    updated = await store.update_reminder(
        created[0]["id"], {"message": "Call Dr. Patel", "remind_at": moved}
    )
    assert updated["message"] == "Call Dr. Patel"
    assert updated["remind_at"] == datetime(2026, 11, 2, 16, tzinfo=UTC)
    assert updated["next_attempt_at"] == updated["remind_at"]

    with pytest.raises(ValueError, match="naive"):
        await store.add_reminders(
            [{"message": "No timezone", "remind_at": datetime(2026, 11, 3, 9)}]
        )
    with pytest.raises(ValueError, match="naive"):
        await store.query_reminders(remind_before=datetime(2026, 11, 3, 9))


@pytest.mark.asyncio
async def test_reminder_delivery_and_cancellation_state_transitions(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    due = datetime(2026, 8, 31, 15, tzinfo=UTC)
    monkeypatch.setattr("src.store.timeutil.now_utc", lambda: due)
    deliver, cancel = await store.add_reminders(
        [
            {"message": "Send the form", "remind_at": due},
            {"message": "Old reminder", "remind_at": due},
        ]
    )

    claimed = await store.claim_due_reminders(due, limit=1)
    assert [item["id"] for item in claimed] == [deliver["id"]]
    assert claimed[0]["delivery_attempts"] == 1
    assert claimed[0]["lease_token"]

    with pytest.raises(ValueError, match="currently being delivered"):
        await store.update_reminder(deliver["id"], {"message": "Changed too late"})
    with pytest.raises(ValueError, match="currently being delivered"):
        await store.cancel_reminder(deliver["id"], due + timedelta(seconds=1))
    with pytest.raises(ValueError, match="currently being delivered"):
        await store.cancel_reminder(deliver["id"], due + timedelta(days=30))

    delivered = await store.ack_reminder_delivery(
        deliver["id"], claimed[0]["lease_token"], due + timedelta(seconds=2)
    )
    assert delivered["status"] == "delivered"
    assert delivered["delivered_at"] == due + timedelta(seconds=2)
    assert delivered["next_attempt_at"] is None
    assert delivered["lease_token"] is None
    with pytest.raises(ValueError, match="cannot be edited"):
        await store.update_reminder(deliver["id"], {"message": "Rewrite history"})
    with pytest.raises(ValueError, match="already been delivered"):
        await store.ack_reminder_delivery(
            deliver["id"], claimed[0]["lease_token"], due + timedelta(seconds=3)
        )

    cancelled = await store.cancel_reminder(cancel["id"], due + timedelta(minutes=1))
    assert cancelled["status"] == "cancelled"
    assert cancelled["cancelled_at"] == due + timedelta(minutes=1)
    assert cancelled["next_attempt_at"] is None
    with pytest.raises(ValueError, match="cannot be cancelled"):
        await store.cancel_reminder(cancel["id"], due + timedelta(minutes=2))


@pytest.mark.asyncio
async def test_failed_delivery_retries_and_expired_lease_is_reclaimed(store: Store) -> None:
    due = datetime(2026, 8, 31, 16, tzinfo=UTC)
    reminder = (
        await store.add_reminders([{"message": "Call Mom", "remind_at": due}])
    )[0]

    first = (await store.claim_due_reminders(due, lease_for=timedelta(seconds=30)))[0]
    assert await store.claim_due_reminders(due + timedelta(seconds=29)) == []
    reclaimed = (
        await store.claim_due_reminders(
            due + timedelta(seconds=30), lease_for=timedelta(minutes=1)
        )
    )[0]
    assert reclaimed["id"] == reminder["id"]
    assert reclaimed["lease_token"] != first["lease_token"]
    assert reclaimed["delivery_attempts"] == 2

    failed_at = due + timedelta(seconds=35)
    retry_at = due + timedelta(minutes=5)
    released = await store.release_reminder_delivery(
        reminder["id"], reclaimed["lease_token"], failed_at, retry_at
    )
    assert released["last_attempt_at"] == failed_at
    assert released["next_attempt_at"] == retry_at
    assert released["lease_token"] is None
    assert await store.claim_due_reminders(retry_at - timedelta(microseconds=1)) == []
    retry = (await store.claim_due_reminders(retry_at))[0]
    assert retry["delivery_attempts"] == 3

    with pytest.raises(ValueError, match="not be earlier"):
        await store.release_reminder_delivery(
            reminder["id"], retry["lease_token"], retry_at, retry_at - timedelta(seconds=1)
        )


@pytest.mark.asyncio
async def test_concurrent_dispatchers_claim_each_reminder_once(tmp_path) -> None:
    store = Store(tmp_path / "claims.sqlite")
    await store.initialize()
    due = datetime(2026, 8, 31, 17, tzinfo=UTC)
    created = await store.add_reminders(
        [{"message": f"Reminder {index}", "remind_at": due} for index in range(8)]
    )

    left, right = await asyncio.gather(
        store.claim_due_reminders(due, limit=8),
        store.claim_due_reminders(due, limit=8),
    )

    claimed_ids = [item["id"] for item in left + right]
    assert sorted(claimed_ids) == sorted(item["id"] for item in created)
    assert len(claimed_ids) == len(set(claimed_ids))
    assert len({item["lease_token"] for item in left + right}) == len(created)


@pytest.mark.asyncio
async def test_existing_database_migration_installs_reminders_and_bumps_version(
    tmp_path,
) -> None:
    path = tmp_path / "legacy.sqlite"
    async with aiosqlite.connect(path) as db:
        await db.execute("CREATE TABLE existing_data (value TEXT NOT NULL)")
        await db.execute("INSERT INTO existing_data VALUES ('preserved')")
        await db.execute("PRAGMA user_version = 3")
        await db.commit()

    await run_migrations(path)

    async with aiosqlite.connect(path) as db:
        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
        columns = {
            row[1]
            for row in await (await db.execute("PRAGMA table_info(reminders)")).fetchall()
        }
        preserved = await (await db.execute("SELECT value FROM existing_data")).fetchone()
        index = await (
            await db.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'idx_reminders_due'"
            )
        ).fetchone()

    assert version == 5
    assert {
        "id",
        "message",
        "remind_at",
        "status",
        "created_at",
        "updated_at",
        "cancelled_at",
        "delivered_at",
        "delivery_attempts",
        "last_attempt_at",
        "next_attempt_at",
        "lease_token",
        "lease_expires_at",
    } <= columns
    assert preserved == ("preserved",)
    assert index == ("idx_reminders_due",)
