#!/usr/bin/env python3
"""Interactively seed Dharvis with the user's real scheduling preferences."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.facts_engine import FactsEngine  # noqa: E402
from src.store import Store  # noqa: E402


QUESTIONS = (
    ("sleep", "What time do you usually go to sleep?", "The user's usual sleep time is {answer}."),
    ("sleep", "What time do you usually wake up?", "The user's usual wake time is {answer}."),
    ("focus", "When do you do focused work best?", "The user does focused work best {answer}."),
    (
        "fitness",
        "What are your gym habits (usual days, times, and session length)?",
        "The user's gym habits are: {answer}.",
    ),
    (
        "commitments",
        "What is your recurring class schedule?",
        "The user's recurring class schedule is: {answer}.",
    ),
    (
        "commitments",
        "What is your recurring work schedule?",
        "The user's recurring work schedule is: {answer}.",
    ),
    (
        "estimates",
        "How long do your common task types actually take?",
        "The user's task-type time estimates are: {answer}.",
    ),
    (
        "priorities",
        "Rank your real priority ordering across school, work, health, "
        "relationships, and other categories.",
        "The user's real priority ordering is: {answer}.",
    ),
)


def collect_facts(
    input_fn: Callable[[str], str] | None = None,
) -> list[dict[str, object]]:
    """Ask every cold-start question and convert answers to standalone facts."""
    read = input_fn or input
    facts: list[dict[str, object]] = []
    for category, question, template in QUESTIONS:
        while True:
            answer = read(f"{question}\n> ").strip()
            if answer:
                break
            print("Please enter an answer so the bot has something concrete to remember.")
        facts.append(
            {
                "content": template.format(answer=answer).replace("..", "."),
                "category": category,
                "source": "seed",
                "confidence": 0.9,
            }
        )
    return facts


async def main() -> None:
    """Initialize storage, collect answers, and idempotently seed the fact store."""
    print("Seed Dharvis with your actual habits. Exact reruns will not duplicate facts.\n")
    facts = collect_facts()
    store = Store()
    await store.initialize()
    seeded = await FactsEngine(store).seed_facts(facts)
    print(f"\nSaved {len(seeded)} facts with source=seed and confidence=0.9.")


if __name__ == "__main__":
    asyncio.run(main())
