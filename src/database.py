"""Database layer for Dharvis using SQLite with async support."""

import aiosqlite
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator

from .config import config
from .migrate import run_migrations
from .utils import format_datetime_iso, get_current_time


def _utc_value(value: str | datetime | None) -> str | None:
    """Normalize a compatibility-layer timestamp to aware UTC text."""
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
    return format_datetime_iso(parsed)


@asynccontextmanager
async def _connection(path: Path) -> AsyncIterator[aiosqlite.Connection]:
    """Yield a legacy-compatible connection with foreign keys enabled."""
    db = await aiosqlite.connect(path)
    try:
        await db.execute("PRAGMA foreign_keys = ON")
        yield db
    finally:
        await db.close()


class Database:
    """Async SQLite database manager for tasks and events."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialize database with path.

        Args:
            db_path: Path to SQLite database file. Uses config default if None.
        """
        self.db_path = db_path or config.DATABASE_PATH

    async def init_db(self) -> None:
        """Install only the canonical schema through the migration runner."""
        await run_migrations(self.db_path)

    # Task operations

    async def add_task(
        self,
        title: str,
        deadline: str | datetime | None = None,
        priority: str = "medium",
        description: str | None = None,
    ) -> int:
        """Add a new task.

        Args:
            title: Task title.
            deadline: ISO datetime string or datetime object.
            priority: Task priority (low, medium, high).
            description: Optional task description.

        Returns:
            ID of the created task.
        """
        deadline = _utc_value(deadline)

        created_at = format_datetime_iso(get_current_time())

        async with _connection(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO tasks (title, description, deadline, priority, status, created_at)
                VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (title, description, deadline, priority, created_at),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_task(self, task_id: int) -> dict[str, Any] | None:
        """Get a task by ID.

        Args:
            task_id: Task ID.

        Returns:
            Task dict or None if not found.
        """
        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM tasks WHERE id = ?", (task_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_pending_tasks(self) -> list[dict[str, Any]]:
        """Get all pending tasks.

        Returns:
            List of task dicts.
        """
        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending'
                ORDER BY deadline ASC NULLS LAST
                """
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def get_tasks_due_by(self, date: str | datetime) -> list[dict[str, Any]]:
        """Get tasks due by a specific date.

        Args:
            date: ISO datetime string or datetime object.

        Returns:
            List of task dicts.
        """
        date = _utc_value(date)

        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE status = 'pending' AND deadline <= ?
                ORDER BY deadline ASC
                """,
                (date,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def complete_task(
        self, task_id: int | None = None, title: str | None = None
    ) -> bool:
        """Mark a task as completed.

        Args:
            task_id: Task ID to complete.
            title: Task title to fuzzy match if no ID provided.

        Returns:
            True if task was found and completed, False otherwise.
        """
        completed_at = format_datetime_iso(get_current_time())

        async with _connection(self.db_path) as db:
            if task_id:
                cursor = await db.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed', completed_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (completed_at, task_id),
                )
            elif title:
                task = await self.fuzzy_match_task(title)
                if not task:
                    return False
                cursor = await db.execute(
                    """
                    UPDATE tasks
                    SET status = 'completed', completed_at = ?
                    WHERE id = ? AND status = 'pending'
                    """,
                    (completed_at, task["id"]),
                )
            else:
                return False

            await db.commit()
            return cursor.rowcount > 0

    async def delete_task(
        self, task_id: int | None = None, title: str | None = None
    ) -> bool:
        """Delete a task.

        Args:
            task_id: Task ID to delete.
            title: Task title to fuzzy match if no ID provided.

        Returns:
            True if task was found and deleted, False otherwise.
        """
        async with _connection(self.db_path) as db:
            if task_id:
                cursor = await db.execute(
                    "DELETE FROM tasks WHERE id = ?", (task_id,)
                )
            elif title:
                task = await self.fuzzy_match_task(title)
                if not task:
                    return False
                cursor = await db.execute(
                    "DELETE FROM tasks WHERE id = ?", (task["id"],)
                )
            else:
                return False

            await db.commit()
            return cursor.rowcount > 0

    async def update_task(self, task_id: int, **fields: Any) -> bool:
        """Update task fields.

        Args:
            task_id: Task ID to update.
            **fields: Fields to update (title, description, deadline, priority, status).

        Returns:
            True if task was found and updated, False otherwise.
        """
        allowed_fields = {"title", "description", "deadline", "priority", "status"}
        update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

        if not update_fields:
            return False

        if "deadline" in update_fields:
            update_fields["deadline"] = _utc_value(update_fields["deadline"])

        set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
        values = list(update_fields.values()) + [task_id]

        async with _connection(self.db_path) as db:
            cursor = await db.execute(
                f"UPDATE tasks SET {set_clause} WHERE id = ?", values
            )
            await db.commit()
            return cursor.rowcount > 0

    async def fuzzy_match_task(self, title: str) -> dict[str, Any] | None:
        """Find a task by fuzzy title matching.

        Args:
            title: Title to search for (case-insensitive substring match).

        Returns:
            Best matching task or None if not found.
        """
        title_lower = title.lower()

        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # First try exact match
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE LOWER(title) = ? AND status = 'pending'
                """,
                (title_lower,),
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)

            # Then try substring match
            cursor = await db.execute(
                """
                SELECT * FROM tasks
                WHERE LOWER(title) LIKE ? AND status = 'pending'
                ORDER BY
                    CASE WHEN LOWER(title) = ? THEN 0 ELSE 1 END,
                    length(title)
                """,
                (f"%{title_lower}%", title_lower),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # Event operations

    async def add_event(
        self,
        title: str,
        start_time: str | datetime,
        end_time: str | datetime | None = None,
        location: str | None = None,
        description: str | None = None,
        source: str = "bot",
    ) -> int:
        """Add a new event.

        Args:
            title: Event title.
            start_time: ISO datetime string or datetime object.
            end_time: Optional end time.
            location: Optional location.
            description: Optional description.
            source: Event source ('bot' or 'gcal').

        Returns:
            ID of the created event.
        """
        start_time = _utc_value(start_time)
        end_time = _utc_value(end_time)
        if start_time is None:
            raise ValueError("start_time is required")
        if end_time is None:
            end_time = (
                datetime.fromisoformat(start_time) + timedelta(hours=1)
            ).isoformat()

        created_at = format_datetime_iso(get_current_time())

        async with _connection(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO events (title, description, start_time, end_time, location, created_at, source)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (title, description, start_time, end_time, location, created_at, source),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_event(self, event_id: int) -> dict[str, Any] | None:
        """Get an event by ID.

        Args:
            event_id: Event ID.

        Returns:
            Event dict or None if not found.
        """
        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_events_between(
        self, start: str | datetime, end: str | datetime
    ) -> list[dict[str, Any]]:
        """Get events within a time range.

        Args:
            start: Start datetime.
            end: End datetime.

        Returns:
            List of event dicts.
        """
        start = _utc_value(start)
        end = _utc_value(end)

        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM events
                WHERE start_time >= ? AND start_time <= ?
                ORDER BY start_time ASC
                """,
                (start, end),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in rows]

    async def delete_event(
        self, event_id: int | None = None, title: str | None = None
    ) -> bool:
        """Delete an event.

        Args:
            event_id: Event ID to delete.
            title: Event title to fuzzy match if no ID provided.

        Returns:
            True if event was found and deleted, False otherwise.
        """
        async with _connection(self.db_path) as db:
            if event_id:
                cursor = await db.execute(
                    "DELETE FROM events WHERE id = ?", (event_id,)
                )
            elif title:
                event = await self.fuzzy_match_event(title)
                if not event:
                    return False
                cursor = await db.execute(
                    "DELETE FROM events WHERE id = ?", (event["id"],)
                )
            else:
                return False

            await db.commit()
            return cursor.rowcount > 0

    async def update_event(self, event_id: int, **fields: Any) -> bool:
        """Update event fields.

        Args:
            event_id: Event ID to update.
            **fields: Fields to update (title, description, start_time, end_time, location).

        Returns:
            True if event was found and updated, False otherwise.
        """
        allowed_fields = {"title", "description", "start_time", "end_time", "location"}
        update_fields = {k: v for k, v in fields.items() if k in allowed_fields}

        if not update_fields:
            return False

        for time_field in ["start_time", "end_time"]:
            if time_field in update_fields:
                update_fields[time_field] = _utc_value(update_fields[time_field])

        set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
        values = list(update_fields.values()) + [event_id]

        async with _connection(self.db_path) as db:
            cursor = await db.execute(
                f"UPDATE events SET {set_clause} WHERE id = ?", values
            )
            await db.commit()
            return cursor.rowcount > 0

    async def fuzzy_match_event(self, title: str) -> dict[str, Any] | None:
        """Find an event by fuzzy title matching.

        Args:
            title: Title to search for (case-insensitive substring match).

        Returns:
            Best matching event or None if not found.
        """
        title_lower = title.lower()

        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            # First try exact match
            cursor = await db.execute(
                "SELECT * FROM events WHERE LOWER(title) = ?", (title_lower,)
            )
            row = await cursor.fetchone()
            if row:
                return dict(row)

            # Then try substring match
            cursor = await db.execute(
                """
                SELECT * FROM events
                WHERE LOWER(title) LIKE ?
                ORDER BY
                    CASE WHEN LOWER(title) = ? THEN 0 ELSE 1 END,
                    start_time DESC
                """,
                (f"%{title_lower}%", title_lower),
            )
            row = await cursor.fetchone()
            return dict(row) if row else None

    # Conversation context operations

    async def add_conversation(
        self, user_message: str, bot_response: str
    ) -> int:
        """Add a conversation entry.

        Args:
            user_message: User's message.
            bot_response: Bot's response.

        Returns:
            ID of the conversation entry.
        """
        timestamp = format_datetime_iso(get_current_time())

        async with _connection(self.db_path) as db:
            await db.execute(
                """INSERT INTO messages (
                    role, content, tool_calls, created_at, session_id
                ) VALUES ('user', ?, '[]', ?, 'legacy')""",
                (user_message, timestamp),
            )
            cursor = await db.execute(
                """INSERT INTO messages (
                    role, content, tool_calls, created_at, session_id
                ) VALUES ('assistant', ?, '[]', ?, 'legacy')""",
                (bot_response, timestamp),
            )
            await db.commit()
            return cursor.lastrowid

    async def get_recent_conversations(
        self, limit: int = 5
    ) -> list[dict[str, Any]]:
        """Get recent conversation entries.

        Args:
            limit: Maximum number of entries to return.

        Returns:
            List of conversation dicts.
        """
        async with _connection(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """SELECT * FROM (
                    SELECT id, role, content, created_at
                    FROM messages
                    WHERE session_id = 'legacy' AND role IN ('user', 'assistant')
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id""",
                (limit * 2,),
            )
            rows = [dict(row) for row in await cursor.fetchall()]
            conversations: list[dict[str, Any]] = []
            for index in range(0, len(rows) - 1, 2):
                user, assistant = rows[index], rows[index + 1]
                if user["role"] == "user" and assistant["role"] == "assistant":
                    conversations.append(
                        {
                            "id": assistant["id"],
                            "user_message": user["content"],
                            "bot_response": assistant["content"],
                            "timestamp": assistant["created_at"],
                        }
                    )
            return conversations
