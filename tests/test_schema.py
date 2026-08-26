"""Canonical schema and legacy migration checks."""

import sqlite3
from pathlib import Path

import pytest

from src.migrate import run_migrations


@pytest.mark.asyncio
async def test_migrates_legacy_tasks_and_events_without_data_loss(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            deadline TEXT, priority TEXT, status TEXT, created_at TEXT, completed_at TEXT
        );
        CREATE TABLE events (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT,
            start_time TEXT, end_time TEXT, location TEXT, created_at TEXT, source TEXT
        );
        INSERT INTO tasks VALUES (
            7, 'Legacy task', NULL, '2026-08-27T10:00:00-05:00',
            'high', 'pending', '2026-08-26T08:00:00-05:00', NULL
        );
        INSERT INTO events VALUES (
            9, 'Legacy event', NULL, '2026-08-27T10:00:00-05:00',
            NULL, NULL, '2026-08-26T08:00:00-05:00', 'invalid-source'
        );
        """
    )
    db.close()

    await run_migrations(path)

    db = sqlite3.connect(path)
    task = db.execute("SELECT id, deadline, category, energy FROM tasks").fetchone()
    event = db.execute("SELECT id, start_time, end_time, source FROM events").fetchone()
    assert task == (7, "2026-08-27T15:00:00+00:00", "personal", "light")
    assert event == (
        9,
        "2026-08-27T15:00:00+00:00",
        "2026-08-27T16:00:00+00:00",
        "bot",
    )
    assert not db.execute("PRAGMA foreign_key_check").fetchall()


@pytest.mark.asyncio
async def test_schedule_reasoning_is_database_required(tmp_path: Path) -> None:
    path = tmp_path / "schema.db"
    await run_migrations(path)
    db = sqlite3.connect(path)
    db.execute("INSERT INTO tasks (title) VALUES ('Test')")
    for whitespace in (
        "",
        "   ",
        "\t\n\v\f\r",
        "\u00a0",  # no-break space
        "\u0085",  # next line
        "\u1680",
        "\u2000\u2007\u200a",
        "\u2028\u2029\u202f\u205f\u3000",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                """INSERT INTO schedule_decisions (
                    task_id, action, start, end, trigger, reasoning
                ) VALUES (1, 'scheduled', ?, ?, 'daily_plan', ?)""",
                (
                    "2026-08-27T15:00:00+00:00",
                    "2026-08-27T16:00:00+00:00",
                    whitespace,
                ),
            )
    db.execute(
        """INSERT INTO schedule_decisions (
            task_id, action, start, end, trigger, reasoning
        ) VALUES (1, 'scheduled', ?, ?, 'daily_plan', ?)""",
        (
            "2026-08-27T15:00:00+00:00",
            "2026-08-27T16:00:00+00:00",
            "\u00a0Deadline proximity makes this the safest slot.\u3000",
        ),
    )


@pytest.mark.asyncio
async def test_schedule_fact_ids_must_be_existing_integers(tmp_path: Path) -> None:
    path = tmp_path / "facts.db"
    await run_migrations(path)
    db = sqlite3.connect(path)
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("INSERT INTO tasks (title) VALUES ('Test')")
    db.execute(
        """INSERT INTO facts (content, category, confidence, source)
        VALUES ('Prefers mornings', 'scheduling', 1.0, 'explicit')"""
    )
    values = (
        1,
        "scheduled",
        "2026-08-27T15:00:00+00:00",
        "2026-08-27T16:00:00+00:00",
        "daily_plan",
        "This uses the user's confirmed morning preference.",
    )
    db.execute(
        """INSERT INTO schedule_decisions (
            task_id, action, start, end, trigger, reasoning, facts_used
        ) VALUES (?, ?, ?, ?, ?, ?, '[1]')""",
        values,
    )
    for invalid in ('[999]', '["1"]', '[1.5]'):
        with pytest.raises(sqlite3.IntegrityError, match="existing integer fact ids"):
            db.execute(
                """INSERT INTO schedule_decisions (
                    task_id, action, start, end, trigger, reasoning, facts_used
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (*values, invalid),
            )


@pytest.mark.asyncio
async def test_utc_suffix_alone_is_not_a_valid_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "timestamps.db"
    await run_migrations(path)
    db = sqlite3.connect(path)
    with pytest.raises(sqlite3.IntegrityError):
        db.execute("INSERT INTO tasks (title, created_at) VALUES ('Bad', 'not-a-dateZ')")
