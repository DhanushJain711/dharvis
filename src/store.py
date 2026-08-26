"""Typed persistence boundary for the data-layer agent to implement."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import aiosqlite

from .config import config
from .migrate import run_migrations

Record = dict[str, Any]
TaskStatus = Literal["pending", "scheduled", "completed", "dropped"]


class Store:
    """Async repository facade; only schema initialization is implemented here."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path or config.DATABASE_PATH)

    async def initialize(self) -> None:
        """Install or migrate the canonical schema."""
        await run_migrations(self.db_path)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a database connection with foreign-key enforcement enabled."""
        db = await aiosqlite.connect(self.db_path)
        try:
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def add_tasks(self, tasks: list[Record]) -> list[Record]:
        """Persist a batch of task payloads and return their records."""
        raise NotImplementedError

    async def add_events(self, events: list[Record]) -> list[Record]:
        """Persist a batch of local event payloads and return their records."""
        raise NotImplementedError

    async def get_task(self, task_id: int) -> Record | None:
        """Fetch one task by exact id."""
        raise NotImplementedError

    async def get_event(self, event_id: int) -> Record | None:
        """Fetch one local event by exact id."""
        raise NotImplementedError

    async def update_task(self, task_id: int, changes: Record) -> Record:
        """Apply validated changes, including explicit ``clear_fields``, to a task."""
        raise NotImplementedError

    async def update_event(self, event_id: int, changes: Record) -> Record:
        """Apply validated changes, including explicit ``clear_fields``, to an event."""
        raise NotImplementedError

    async def complete_task(self, task_id: int, actual_minutes: int | None = None) -> Record:
        """Mark a task complete with an aware UTC completion time."""
        raise NotImplementedError

    async def drop_task(self, task_id: int) -> Record:
        """Mark a task dropped while retaining its decision history."""
        raise NotImplementedError

    async def delete_task(self, task_id: int) -> Record:
        """Tool-facing alias that drops a task without erasing decision history."""
        return await self.drop_task(task_id)

    async def delete_event(self, event_id: int) -> bool:
        """Delete one local event."""
        raise NotImplementedError

    async def query_tasks(
        self,
        status: TaskStatus | None = None,
        category: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
    ) -> list[Record]:
        """Query tasks by status, category, and aware UTC deadline bounds."""
        return []

    async def query_events(self, start: datetime, end: datetime) -> list[Record]:
        """Query local events overlapping an aware UTC half-open range."""
        return []

    async def apply_schedule_decision(
        self,
        task_id: int,
        action: Literal["scheduled", "moved", "unscheduled", "shortened", "extended"],
        start: datetime,
        end: datetime,
        previous_start: datetime | None,
        previous_end: datetime | None,
        trigger: Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"],
        reasoning: str,
        facts_used: list[int],
        gcal_event_id: str | None,
    ) -> Record:
        """Atomically mutate placement and insert its contemporaneous rationale.

        Implementations must perform both writes in one database transaction;
        neither the task placement nor schedule decision may commit alone.
        """
        raise NotImplementedError

    async def get_schedule_decisions(self, task_id: int) -> list[Record]:
        """Return a task's schedule decision history in chronological order."""
        return []

    async def mark_decision_surfaced(self, decision_id: int) -> None:
        """Mark a schedule decision's rationale as surfaced to the user."""
        raise NotImplementedError

    async def add_fact(self, fact: Record) -> Record:
        """Persist a durable user fact."""
        raise NotImplementedError

    async def update_fact(self, fact_id: int, changes: Record) -> Record:
        """Update or deactivate a durable user fact."""
        raise NotImplementedError

    async def query_facts(
        self,
        category: str | None = None,
        active: bool | None = True,
        min_confidence: float | None = None,
    ) -> list[Record]:
        """Query stored facts using optional filters."""
        return []

    async def add_goal(self, goal: Record) -> Record:
        """Persist a recurring quantitative goal."""
        raise NotImplementedError

    async def log_goal_progress(
        self,
        goal_id: int,
        amount: float,
        source: Literal["task", "manual", "inferred"],
        logged_at: datetime,
    ) -> Record:
        """Append an aware UTC goal-progress observation."""
        raise NotImplementedError

    async def query_goals(
        self, active: bool | None = True, category: str | None = None
    ) -> list[Record]:
        """Query goals with their current-period progress."""
        return []

    async def append_message(
        self,
        role: Literal["user", "assistant", "tool"],
        content: str,
        tool_calls: list[Record],
        session_id: str,
    ) -> Record:
        """Append a conversation message with an aware UTC timestamp."""
        raise NotImplementedError

    async def get_messages(self, session_id: str, limit: int = 100) -> list[Record]:
        """Return recent messages in chronological order."""
        return []

    async def get_daily_log(self, local_date: date) -> Record | None:
        """Fetch one local-calendar daily log."""
        return None

    async def upsert_daily_log(self, local_date: date, changes: Record) -> Record:
        """Create or update one local-calendar daily log."""
        raise NotImplementedError


async def create_store(db_path: str | Path | None = None) -> Store:
    """Create and initialize a store instance."""
    store = Store(db_path)
    await store.initialize()
    return store
