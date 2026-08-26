"""Conversation-history boundary for the stateful agent."""

from __future__ import annotations

from typing import Any, Literal

from .store import Store

Message = dict[str, Any]


class History:
    """Session-scoped message history backed by :class:`Store`."""

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
        return await self.store.append_message(role, content, tool_calls or [], session_id)

    async def load(self, session_id: str, limit: int = 100) -> list[Message]:
        """Load recent messages in chronological order."""
        return await self.store.get_messages(session_id, limit)

    async def clear(self, session_id: str) -> None:
        """Remove a session's history; implemented by the data-layer agent."""
        raise NotImplementedError

    def to_openai_input(self, messages: list[Message]) -> list[dict[str, Any]]:
        """Convert persisted messages into OpenAI Responses API input items."""
        raise NotImplementedError


async def create_history(store: Store) -> History:
    """Create a history facade for a store."""
    return History(store)
