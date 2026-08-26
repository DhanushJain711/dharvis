"""Autonomous scheduling contracts; no placement algorithm lives here yet."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Literal

from .calendar_service import CalendarService
from .freebusy import FreeBlock
from .store import Store

Trigger = Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"]
DecisionAction = Literal["scheduled", "moved", "unscheduled", "shortened", "extended"]


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """A proposed placement plus its contemporaneous rationale."""

    task_id: int
    action: DecisionAction
    start: datetime
    end: datetime
    previous_start: datetime | None
    previous_end: datetime | None
    trigger: Trigger
    reasoning: str
    facts_used: list[int]


class SchedulerEngine:
    """Coordinates free-time selection, Calendar writes, and atomic decision logging."""

    def __init__(self, store: Store, calendar: CalendarService) -> None:
        self.store = store
        self.calendar = calendar

    async def choose_slot(
        self, task_id: int, candidates: list[FreeBlock], trigger: Trigger
    ) -> ScheduleDecision:
        """Choose a slot and produce a non-empty plain-language rationale."""
        raise NotImplementedError

    async def schedule_task(
        self,
        task_id: int,
        start: datetime,
        end: datetime,
        reasoning: str,
        trigger: Trigger,
        facts_used: list[int] | None = None,
    ) -> ScheduleDecision:
        """Write a Kalendra block, then atomically persist placement and rationale."""
        raise NotImplementedError

    async def plan_day(self, local_date: date) -> list[ScheduleDecision]:
        """Plan unscheduled work for one local calendar day."""
        return []

    async def resolve_conflicts(
        self, start: datetime, end: datetime
    ) -> list[ScheduleDecision]:
        """Move affected task blocks and record each conflict rationale."""
        return []

    async def explain_schedule(self, task_id: int) -> list[dict[str, object]]:
        """Return stored decision history without reconstructing rationale."""
        return await self.store.get_schedule_decisions(task_id)


async def create_scheduler_engine(
    store: Store, calendar: CalendarService
) -> SchedulerEngine:
    """Create a scheduling-engine facade."""
    return SchedulerEngine(store, calendar)
