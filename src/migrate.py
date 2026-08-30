"""Idempotent schema installation and legacy SQLite migration."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import aiosqlite

from .config import config

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_TASK_COLUMNS = {
    "id", "title", "description", "deadline", "estimated_minutes", "category",
    "energy", "priority", "status", "scheduled_start", "scheduled_end",
    "gcal_event_id", "goal_id", "created_at", "completed_at", "actual_minutes",
}
_EVENT_COLUMNS = {
    "id", "title", "description", "start_time", "end_time", "location",
    "category", "source", "gcal_event_id", "created_at",
}

_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "tasks": {
        "series_key": "TEXT",
        "estimate_source": (
            "TEXT CHECK (estimate_source IS NULL OR "
            "estimate_source IN ('user', 'history', 'default', 'goal'))"
        ),
        "actual_minutes_source": (
            "TEXT CHECK (actual_minutes_source IS NULL OR "
            "actual_minutes_source IN ('user', 'debrief', 'calendar', 'inferred'))"
        ),
    },
    "goals": {
        "session_minutes": "INTEGER NOT NULL DEFAULT 60 CHECK (session_minutes > 0)",
        "scheduling_enabled": (
            "INTEGER NOT NULL DEFAULT 1 CHECK (scheduling_enabled IN (0, 1))"
        ),
    },
    "goal_progress": {
        "task_id": "INTEGER REFERENCES tasks(id) ON DELETE SET NULL",
    },
    "event_change_proposals": {
        "claimed_at": "TEXT",
        "claim_token": "TEXT",
    },
}


def _enum(value: Any, allowed: set[str], default: str) -> str:
    """Return a valid legacy enum value or its safe canonical default."""
    return value if isinstance(value, str) and value in allowed else default


def _positive_int(value: Any, *, allow_zero: bool = False) -> int | None:
    """Normalize optional legacy duration values without violating checks."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    return value if value >= (0 if allow_zero else 1) else None


def _utc_text(value: Any, *, required: bool = False) -> str | None:
    """Normalize a legacy timestamp to an ISO-8601 UTC string."""
    if value in (None, ""):
        return datetime.now(UTC).isoformat() if required else None
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    elif isinstance(value, datetime):
        parsed = value
    else:
        raise ValueError(f"unsupported legacy timestamp {value!r}")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=ZoneInfo(config.USER_TIMEZONE))
    return parsed.astimezone(UTC).isoformat()


async def _table_columns(db: aiosqlite.Connection, table: str) -> set[str]:
    cursor = await db.execute(f'PRAGMA table_info("{table}")')
    return {row[1] for row in await cursor.fetchall()}


async def _table_exists(db: aiosqlite.Connection, table: str) -> bool:
    cursor = await db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    )
    return await cursor.fetchone() is not None


async def _copy_legacy_tasks(db: aiosqlite.Connection) -> None:
    db.row_factory = aiosqlite.Row
    rows = await (await db.execute("SELECT * FROM tasks__legacy")).fetchall()
    for raw in rows:
        row = dict(raw)
        status = _enum(
            row.get("status"), {"pending", "scheduled", "completed", "dropped"}, "pending"
        )
        await db.execute(
            """INSERT INTO tasks (
                id, title, description, deadline, estimated_minutes, category, energy,
                priority, status, scheduled_start, scheduled_end, gcal_event_id,
                goal_id, created_at, completed_at, actual_minutes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id"), row.get("title") or "Untitled task", row.get("description"),
                _utc_text(row.get("deadline")), _positive_int(row.get("estimated_minutes")),
                _enum(row.get("category"), {"school", "work", "personal", "fitness", "career", "errand"}, "personal"),
                _enum(row.get("energy"), {"deep_focus", "light", "errand"}, "light"),
                _enum(row.get("priority"), {"low", "medium", "high"}, "medium"), status,
                _utc_text(row.get("scheduled_start")), _utc_text(row.get("scheduled_end")),
                row.get("gcal_event_id"), row.get("goal_id"),
                _utc_text(row.get("created_at"), required=True),
                _utc_text(row.get("completed_at")),
                _positive_int(row.get("actual_minutes"), allow_zero=True),
            ),
        )


async def _copy_legacy_events(db: aiosqlite.Connection) -> None:
    db.row_factory = aiosqlite.Row
    rows = await (await db.execute("SELECT * FROM events__legacy")).fetchall()
    for raw in rows:
        row = dict(raw)
        start = _utc_text(row.get("start_time"), required=True)
        end = _utc_text(row.get("end_time"))
        if end is not None and end <= start:
            end = None
        if end is None:
            end = (datetime.fromisoformat(start) + timedelta(hours=1)).isoformat()
        source = _enum(row.get("source"), {"bot", "gcal"}, "bot")
        await db.execute(
            """INSERT INTO events (
                id, title, description, start_time, end_time, location, category,
                source, gcal_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("id"), row.get("title") or "Untitled event", row.get("description"),
                start, end, row.get("location"), row.get("category"), source,
                row.get("gcal_event_id"), _utc_text(row.get("created_at"), required=True),
            ),
        )


async def _copy_legacy_conversations(db: aiosqlite.Connection) -> None:
    """Move the retired paired-conversation table into canonical messages."""
    db.row_factory = aiosqlite.Row
    rows = await (
        await db.execute("SELECT * FROM conversation_context ORDER BY id")
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        created_at = _utc_text(row.get("timestamp"), required=True)
        await db.execute(
            """INSERT INTO messages (role, content, tool_calls, created_at, session_id)
            VALUES ('user', ?, '[]', ?, 'legacy')""",
            (row.get("user_message") or "", created_at),
        )
        await db.execute(
            """INSERT INTO messages (role, content, tool_calls, created_at, session_id)
            VALUES ('assistant', ?, '[]', ?, 'legacy')""",
            (row.get("bot_response") or "", created_at),
        )


async def run_migrations(db_path: str | Path | None = None) -> None:
    """Install the canonical schema and safely rebuild known legacy tables."""
    path = Path(db_path or config.DATABASE_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    async with aiosqlite.connect(path) as db:
        await db.execute("PRAGMA foreign_keys = ON")
        version = (await (await db.execute("PRAGMA user_version")).fetchone())[0]
        await db.execute("PRAGMA foreign_keys = OFF")
        tasks_legacy = await _table_exists(db, "tasks") and not _TASK_COLUMNS.issubset(
            await _table_columns(db, "tasks")
        )
        events_legacy = await _table_exists(db, "events") and (
            version < 2
            or not _EVENT_COLUMNS.issubset(await _table_columns(db, "events"))
        )
        conversations_legacy = await _table_exists(db, "conversation_context")
        try:
            index_names = (
                "idx_tasks_status_deadline", "idx_tasks_scheduled_start", "idx_tasks_goal_id",
                "idx_events_time_range", "idx_events_gcal_id",
                "idx_event_change_proposals_pending",
            )
            prefix = ["BEGIN IMMEDIATE;"]
            prefix.extend(f'DROP INDEX IF EXISTS "{name}";' for name in index_names)
            if tasks_legacy:
                prefix.append("ALTER TABLE tasks RENAME TO tasks__legacy;")
            if events_legacy:
                prefix.append("ALTER TABLE events RENAME TO events__legacy;")
            for table, definitions in _ADDITIVE_COLUMNS.items():
                if not await _table_exists(db, table):
                    continue
                # A legacy tasks table is rebuilt below; adding its new columns
                # before the rename would only do disposable work.
                if table == "tasks" and tasks_legacy:
                    continue
                existing = await _table_columns(db, table)
                for column, definition in definitions.items():
                    if column not in existing:
                        prefix.append(
                            f'ALTER TABLE "{table}" ADD COLUMN "{column}" {definition};'
                        )
            await db.executescript("\n".join(prefix) + "\n" + schema)
            if tasks_legacy:
                await _copy_legacy_tasks(db)
                await db.execute("DROP TABLE tasks__legacy")
            if events_legacy:
                await _copy_legacy_events(db)
                await db.execute("DROP TABLE events__legacy")
            if conversations_legacy:
                await _copy_legacy_conversations(db)
                await db.execute("DROP TABLE conversation_context")
            await db.execute("PRAGMA user_version = 3")
            violations = await (await db.execute("PRAGMA foreign_key_check")).fetchall()
            if violations:
                raise RuntimeError(f"foreign key violations after migration: {violations!r}")
            await db.commit()
        except Exception:
            await db.rollback()
            raise
        finally:
            await db.execute("PRAGMA foreign_keys = ON")


def main() -> None:
    """Run migrations for the configured database from the command line."""
    asyncio.run(run_migrations())


if __name__ == "__main__":
    main()
