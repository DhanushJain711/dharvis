"""Contracts for extracting and retrieving durable user facts."""

from __future__ import annotations

from typing import Any

from .store import Store

Fact = dict[str, Any]


class FactsEngine:
    """Extracts stable preferences while avoiding transient-message leakage."""

    def __init__(self, store: Store) -> None:
        self.store = store

    async def extract_facts(self, user_message: str, assistant_message: str) -> list[Fact]:
        """Extract durable facts from one completed conversational turn."""
        return []

    async def consolidate(self, candidates: list[Fact]) -> list[Fact]:
        """Merge evidence into existing facts without duplicating semantics."""
        raise NotImplementedError

    async def relevant_facts(
        self, context: str, category: str | None = None, limit: int = 20
    ) -> list[Fact]:
        """Retrieve active, sufficiently confident facts relevant to a decision."""
        return []

    async def seed_facts(self, facts: list[Fact]) -> list[Fact]:
        """Install explicitly provided initial facts idempotently."""
        raise NotImplementedError

    async def confirm_fact(self, fact_id: int, confidence: float = 1.0) -> Fact:
        """Reconfirm a fact and increment its evidence count."""
        raise NotImplementedError

    async def deactivate_fact(self, fact_id: int) -> Fact:
        """Deactivate a contradicted or expired fact without deleting it."""
        raise NotImplementedError


async def create_facts_engine(store: Store) -> FactsEngine:
    """Create a facts-engine facade."""
    return FactsEngine(store)
