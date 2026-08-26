"""APScheduler job contracts for briefs, debriefs, and proactive planning."""

from __future__ import annotations

from datetime import date
from typing import Any

from .scheduler_engine import SchedulerEngine
from .store import Store


async def send_daily_brief(store: Store, telegram: Any, local_date: date) -> None:
    """Send the morning plan once and record its UTC delivery time."""
    raise NotImplementedError


async def send_daily_debrief(store: Store, telegram: Any, local_date: date) -> None:
    """Send the evening completion review once and record its UTC delivery time."""
    raise NotImplementedError


async def run_daily_planning(engine: SchedulerEngine, local_date: date) -> None:
    """Autonomously place work for a local day."""
    await engine.plan_day(local_date)


async def reconcile_calendar(engine: SchedulerEngine) -> None:
    """Detect external conflicts and reschedule affected task blocks."""
    raise NotImplementedError


def configure_jobs(
    scheduler: Any,
    store: Store,
    engine: SchedulerEngine,
    telegram: Any,
) -> None:
    """Register all recurring jobs using configured local clock times."""
    raise NotImplementedError


def create_job_scheduler() -> Any:
    """Create the timezone-configured APScheduler instance."""
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to enable proactive jobs") from exc
    from .config import config

    return AsyncIOScheduler(timezone=config.USER_TIMEZONE)
