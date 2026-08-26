"""Conversation and physical-session history for the Responses API."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import uuid4

import aiosqlite

from .store import Store

Message = dict[str, Any]
SESSION_IDLE_TIMEOUT = timedelta(hours=4)


def _parse_utc(value: str | datetime) -> datetime:
    """Parse a persisted UTC timestamp and reject naive values."""
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("message created_at must be timezone-aware")
    return parsed.astimezone(UTC)


def _json_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _session_like_prefix(conversation_id: str) -> str:
    """Escape a conversation id for a SQLite ``LIKE ... ESCAPE`` query."""
    escaped = conversation_id.replace("\\", "\\\\").replace("%", "\\%")
    escaped = escaped.replace("_", "\\_")
    return f"{escaped}::%"


class History:
    """Session-scoped message history backed by :class:`Store`.

    Callers supply a stable conversation id (for example, a Telegram chat id).
    ``resolve_session`` maps it to a physical session that rolls over after four
    hours of silence. The direct SQL lookup is intentionally isolated here until
    the data-layer contract grows an equivalent Store method.
    """

    def __init__(self, store: Store) -> None:
        self.store = store

    async def append(
        self,
        session_id: str,
        role: Literal["user", "assistant", "tool"],
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> Message:
        """Append one conversation item."""
        metadata = tool_calls or []
        created_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        async with self.store.connection() as db:
            cursor = await db.execute(
                """
                INSERT INTO messages (role, content, tool_calls, created_at, session_id)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    role,
                    content,
                    json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
                    created_at,
                    session_id,
                ),
            )
            await db.commit()
            message_id = cursor.lastrowid
        return {
            "id": message_id,
            "role": role,
            "content": content,
            "tool_calls": metadata,
            "created_at": created_at,
            "session_id": session_id,
        }

    async def load(self, session_id: str, limit: int = 100) -> list[Message]:
        """Load the newest messages in chronological order."""
        if limit < 1:
            return []
        async with self.store.connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT id, role, content, tool_calls, created_at, session_id
                FROM (
                    SELECT id, role, content, tool_calls, created_at, session_id
                    FROM messages
                    WHERE session_id = ?
                    ORDER BY created_at DESC, id DESC
                    LIMIT ?
                )
                ORDER BY created_at ASC, id ASC
                """,
                (session_id, limit),
            )
            rows = await cursor.fetchall()
        return [
            {
                "id": row["id"],
                "role": row["role"],
                "content": row["content"],
                "tool_calls": _json_list(row["tool_calls"]),
                "created_at": row["created_at"],
                "session_id": row["session_id"],
            }
            for row in rows
        ]

    async def latest_session(
        self, conversation_id: str
    ) -> tuple[str | None, datetime | None]:
        """Return the most recently active physical session for a conversation."""
        async with self.store.connection() as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                """
                SELECT session_id, created_at
                FROM messages
                WHERE session_id = ? OR session_id LIKE ? ESCAPE '\\'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
                """,
                (conversation_id, _session_like_prefix(conversation_id)),
            )
            row = await cursor.fetchone()
        if row is None:
            return None, None
        return str(row["session_id"]), _parse_utc(row["created_at"])

    async def resolve_session(
        self,
        conversation_id: str,
        *,
        at: datetime | None = None,
        idle_timeout: timedelta = SESSION_IDLE_TIMEOUT,
    ) -> tuple[str, str | None]:
        """Resolve the active session and return a prior session when it expired.

        The timeout is inclusive: exactly four hours of silence starts a new
        physical session. A brand-new conversation also receives a physical id.
        """
        current_time = at or datetime.now(UTC)
        if current_time.tzinfo is None or current_time.utcoffset() is None:
            raise ValueError("session resolution time must be timezone-aware")
        previous_id, last_active = await self.latest_session(conversation_id)
        if previous_id is None or last_active is None:
            return self.new_session_id(conversation_id), None
        if current_time.astimezone(UTC) - last_active >= idle_timeout:
            return self.new_session_id(conversation_id), previous_id
        return previous_id, None

    @staticmethod
    def new_session_id(conversation_id: str) -> str:
        """Create a collision-resistant physical id under a conversation id."""
        return f"{conversation_id}::{uuid4().hex}"

    async def clear(self, session_id: str) -> None:
        """Remove a physical session's persisted messages."""
        async with self.store.connection() as db:
            await db.execute("DELETE FROM messages WHERE session_id = ?", (session_id,))
            await db.commit()

    def to_openai_input(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert persisted records into valid Responses API input items.

        Assistant output metadata stores the original Responses output items.
        Only function calls that have a matching function-call output are
        replayed. Orphan outputs (usually caused by the 20-message history
        window cutting through a tool exchange) are dropped.
        """
        metadata_by_message = [
            _json_list(message.get("tool_calls")) for message in messages
        ]
        completed_call_ids = {
            str(item.get("call_id"))
            for metadata in metadata_by_message
            for item in metadata
            if item.get("type") == "function_call_output" and item.get("call_id")
        }

        result: list[dict[str, Any]] = []
        emitted_calls: set[str] = set()
        for message, metadata in zip(messages, metadata_by_message):
            role = message.get("role")
            content = str(message.get("content") or "")

            if role == "assistant" and metadata:
                emitted_message = False
                for item in metadata:
                    item_type = item.get("type")
                    if item_type == "function_call":
                        call_id = str(item.get("call_id") or "")
                        if call_id and call_id in completed_call_ids:
                            result.append(item)
                            emitted_calls.add(call_id)
                    elif item_type == "function_call_output":
                        # Tool outputs are persisted on tool-role records below.
                        continue
                    elif item_type in {"message", "reasoning"}:
                        result.append(item)
                        emitted_message = emitted_message or item_type == "message"
                if content and not emitted_message:
                    result.append({"role": "assistant", "content": content})
                continue

            if role == "tool" and metadata:
                for item in metadata:
                    if item.get("type") != "function_call_output":
                        continue
                    call_id = str(item.get("call_id") or "")
                    if call_id and call_id in emitted_calls:
                        result.append(item)
                continue

            if role in {"user", "assistant"}:
                result.append({"role": role, "content": content})

        return result


async def create_history(store: Store) -> History:
    """Create a history facade for a store."""
    return History(store)
