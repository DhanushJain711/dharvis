"""Database layer for Dharvis using SQLite with async support."""

import aiosqlite
from datetime import datetime
from pathlib import Path
from typing import Any

from config import config
from utils import format_datetime_iso, get_current_time


class Database:
    """Async SQLite database manager for tasks and events."""

    def __init__(self, db_path: Path | None = None):
        """Initialize database with path.

        Args:
            db_path: Path to SQLite database file. Uses config default if None.
        """
        self.db_path = db_path or config.DATABASE_PATH

    async def init_db(self) -> None:
        """Create database tables if they don't exist."""
        async with aiosqlite.connect(self.db_path) as db:
            # Tasks table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    deadline TEXT,
                    priority TEXT DEFAULT 'medium',
                    status TEXT DEFAULT 'pending',
                    created_at TEXT,
                    completed_at TEXT
                )
            """)

            # Events table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT,
                    start_time TEXT,
                    end_time TEXT,
                    location TEXT,
                    created_at TEXT,
                    source TEXT DEFAULT 'bot'
                )
            """)

            # Conversation context table
            await db.execute("""
                CREATE TABLE IF NOT EXISTS conversation_context (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_message TEXT,
                    bot_response TEXT,
                    timestamp TEXT
                )
            """)

            await db.commit()

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
        if isinstance(deadline, datetime):
            deadline = format_datetime_iso(deadline)

        created_at = format_datetime_iso(get_current_time())

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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
        if isinstance(date, datetime):
            date = format_datetime_iso(date)

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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

    async def update_task(self, task_id: int, **fields) -> bool:
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

        if "deadline" in update_fields and isinstance(
            update_fields["deadline"], datetime
        ):
            update_fields["deadline"] = format_datetime_iso(update_fields["deadline"])

        set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
        values = list(update_fields.values()) + [task_id]

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
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
        if isinstance(start_time, datetime):
            start_time = format_datetime_iso(start_time)
        if isinstance(end_time, datetime):
            end_time = format_datetime_iso(end_time)

        created_at = format_datetime_iso(get_current_time())

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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
        if isinstance(start, datetime):
            start = format_datetime_iso(start)
        if isinstance(end, datetime):
            end = format_datetime_iso(end)

        async with aiosqlite.connect(self.db_path) as db:
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
        async with aiosqlite.connect(self.db_path) as db:
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

    async def update_event(self, event_id: int, **fields) -> bool:
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
            if time_field in update_fields and isinstance(
                update_fields[time_field], datetime
            ):
                update_fields[time_field] = format_datetime_iso(
                    update_fields[time_field]
                )

        set_clause = ", ".join(f"{k} = ?" for k in update_fields.keys())
        values = list(update_fields.values()) + [event_id]

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
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

        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                """
                INSERT INTO conversation_context (user_message, bot_response, timestamp)
                VALUES (?, ?, ?)
                """,
                (user_message, bot_response, timestamp),
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
        async with aiosqlite.connect(self.db_path) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT * FROM conversation_context
                ORDER BY timestamp DESC
                LIMIT ?
                """,
                (limit,),
            )
            rows = await cursor.fetchall()
            return [dict(row) for row in reversed(rows)]
