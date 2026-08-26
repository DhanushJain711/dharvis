"""Stateful OpenAI agent contract and runnable placeholder behavior."""

from __future__ import annotations

from typing import Any

from .history import History
from .timeutil import format_time_context
from .tools import TOOLS


class Agent:
    """Conversation orchestrator that future agent-core work will implement."""

    def __init__(self, history: History | None = None) -> None:
        self.history = history
        self.tools = TOOLS

    def build_system_prompt(self) -> str:
        """Return the minimal prompt shell with explicit local time context."""
        return (
            "You are Dharvis, a stateful personal scheduling assistant.\n\n"
            + format_time_context()
        )

    async def respond(self, message: str, session_id: str) -> str:
        """Return a safe placeholder until the agent-core implementation lands."""
        del message, session_id
        return "Dharvis is online. Agentic planning is being connected now."

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch a validated tool call to its owning service."""
        raise NotImplementedError

    async def run_tool_loop(self, message: str, session_id: str) -> str:
        """Run the OpenAI response/tool loop and persist each turn."""
        raise NotImplementedError


async def create_agent(history: History | None = None) -> Agent:
    """Create an agent instance without making an API request."""
    return Agent(history)
