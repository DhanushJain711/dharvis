#!/usr/bin/env python3
"""Run the Dharvis agent loop in a terminal without Telegram."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.agent import Agent  # noqa: E402
from src.history import History  # noqa: E402
from src.store import Store  # noqa: E402


async def _readline(prompt: str) -> str:
    return await asyncio.to_thread(input, prompt)


async def main() -> None:
    """Initialize the normal persistence stack and serve terminal turns."""
    store = Store()
    await store.initialize()
    agent = Agent(History(store))
    conversation_id = os.getenv("CHAT_REPL_CONVERSATION_ID", "local-repl")

    print("Dharvis REPL — type /exit to quit")
    while True:
        try:
            message = (await _readline("you> ")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if message.lower() in {"/exit", "/quit", "exit", "quit"}:
            break
        if not message:
            continue
        try:
            reply = await agent.respond(message, conversation_id)
        except Exception as exc:
            print(f"error> {type(exc).__name__}: {exc}")
            continue
        print(f"bot> {reply}")


if __name__ == "__main__":
    asyncio.run(main())
