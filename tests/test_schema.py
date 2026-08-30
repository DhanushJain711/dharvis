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
async def test_migrates_additive_goal_and_task_fields_on_existing_canonical_db(
    tmp_path: Path,
) -> None:
    path = tmp_path / "pre-feature.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE goals (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, target_amount REAL NOT NULL,
            target_unit TEXT NOT NULL, period TEXT NOT NULL, category TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        );
        CREATE TABLE tasks (
            id INTEGER PRIMARY KEY, title TEXT NOT NULL, description TEXT, deadline TEXT,
            estimated_minutes INTEGER, category TEXT NOT NULL DEFAULT 'personal',
            energy TEXT NOT NULL DEFAULT 'light', priority TEXT NOT NULL DEFAULT 'medium',
            status TEXT NOT NULL DEFAULT 'pending', scheduled_start TEXT,
            scheduled_end TEXT, gcal_event_id TEXT, goal_id INTEGER, created_at TEXT NOT NULL,
            completed_at TEXT, actual_minutes INTEGER
        );
        CREATE TABLE goal_progress (
            id INTEGER PRIMARY KEY, goal_id INTEGER NOT NULL, logged_at TEXT NOT NULL,
            amount REAL NOT NULL, source TEXT NOT NULL
        );
        CREATE TABLE event_change_proposals (
            id TEXT PRIMARY KEY, operation TEXT NOT NULL, payload TEXT NOT NULL,
            conflicts TEXT NOT NULL, created_at TEXT NOT NULL, expires_at TEXT NOT NULL,
            consumed_at TEXT
        );
        INSERT INTO goals VALUES (1, 'Legacy goal', 3, 'sessions', 'week', 'fitness', 1,
            '2026-08-01T00:00:00Z');
        INSERT INTO tasks VALUES (2, 'Legacy task', NULL, NULL, NULL, 'personal', 'light',
            'medium', 'pending', NULL, NULL, NULL, 1, '2026-08-01T00:00:00Z', NULL, NULL);
        INSERT INTO goal_progress VALUES (3, 1, '2026-08-01T00:00:00Z', 1, 'manual');
        INSERT INTO event_change_proposals VALUES (
            'legacy-proposal', 'create', '{}', '[]', '2026-08-01T00:00:00Z',
            '2026-08-01T01:00:00Z', NULL
        );
        """
    )
    db.close()

    await run_migrations(path)

    db = sqlite3.connect(path)
    task_columns = {row[1] for row in db.execute("PRAGMA table_info(tasks)")}
    goal_columns = {row[1] for row in db.execute("PRAGMA table_info(goals)")}
    progress_columns = {row[1] for row in db.execute("PRAGMA table_info(goal_progress)")}
    proposal_columns = {
        row[1] for row in db.execute("PRAGMA table_info(event_change_proposals)")
    }
    assert {"series_key", "estimate_source", "actual_minutes_source"} <= task_columns
    assert {"session_minutes", "scheduling_enabled"} <= goal_columns
    assert "task_id" in progress_columns
    assert {"claimed_at", "claim_token"} <= proposal_columns
    assert db.execute(
        "SELECT session_minutes, scheduling_enabled FROM goals WHERE id = 1"
    ).fetchone() == (60, 1)
    assert db.execute("SELECT title FROM tasks WHERE id = 2").fetchone() == ("Legacy task",)
    assert db.execute("SELECT amount FROM goal_progress WHERE id = 3").fetchone() == (1.0,)
    assert db.execute(
        "SELECT claimed_at, claim_token FROM event_change_proposals WHERE id = 'legacy-proposal'"
    ).fetchone() == (None, None)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"goal_schedule_items", "event_change_proposals"} <= tables
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
