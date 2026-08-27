"""Runtime composition and the canonical implementations of agent tools."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from typing import Any

from . import timeutil
from .calendar_service import CalendarService
from .facts_engine import FactsEngine
from .freebusy import find_free_blocks, query_schedule
from .scheduler_engine import SchedulerEngine
from .store import Store

ToolHandler = Callable[..., Awaitable[Any]]
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "about", "after", "before", "because", "from", "that", "the", "their",
    "this", "with", "your", "you", "and", "for", "into", "slot", "time",
}


def _aware(value: str | datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def jsonable(value: Any) -> Any:
    """Convert runtime records and dataclasses into tool-result JSON values."""
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be returned from a tool")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _words(value: str) -> set[str]:
    return {
        word for word in _WORD_RE.findall(value.lower())
        if len(word) >= 3 and word not in _STOP
    }


async def build_tool_handlers(
    store: Store,
    calendar: CalendarService,
    scheduler: SchedulerEngine,
    facts_engine: FactsEngine,
) -> dict[str, ToolHandler]:
    """Bind every schema in ``src.tools`` to real application services."""

    async def add_task(tasks: list[dict[str, Any]]) -> Any:
        payloads: list[dict[str, Any]] = []
        for task in tasks:
            item = dict(task)
            item["deadline"] = _aware(item.get("deadline"), "deadline")
            payloads.append(item)
        return jsonable(await store.add_tasks(payloads))

    async def add_event(events: list[dict[str, Any]]) -> Any:
        validated: list[dict[str, Any]] = []
        for event in events:
            item = dict(event)
            item["start"] = _aware(item.get("start"), "start")
            item["end"] = _aware(item.get("end"), "end")
            if item["start"] is None or item["end"] is None or item["end"] <= item["start"]:
                raise ValueError("event end must be later than start")
            validated.append(item)
        records: list[dict[str, Any]] = []
        schedule_changes: list[Any] = []
        for item in validated:
            created = await calendar.create_event(
                item,
                "the user requested this fixed-time event",
            )
            local = dict(item)
            local["source"] = "bot"
            local["gcal_event_id"] = created["gcal_event_id"]
            try:
                records.extend(await store.add_events([local]))
            except Exception:
                await calendar.delete_event(str(created["gcal_event_id"]))
                raise
            schedule_changes.extend(
                await scheduler.detect_conflicts(item["start"], item["end"])
            )
        return jsonable({"events": records, "schedule_changes": schedule_changes})

    async def update_task(task_id: int, clear_fields: list[str], **changes: Any) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        payload = {key: value for key, value in changes.items() if value is not None}
        if "deadline" in payload:
            payload["deadline"] = _aware(payload["deadline"], "deadline")
        payload["clear_fields"] = clear_fields
        updated = await store.update_task(task_id, payload)
        schedule_changes: list[Any] = []
        deadline_changed = "deadline" in payload or "deadline" in clear_fields
        if (
            deadline_changed
            and current.get("scheduled_start")
            and current.get("scheduled_end")
        ):
            new_deadline = updated.get("deadline")
            deadline_text = (
                timeutil.to_local(new_deadline).strftime("%a %-I:%M%p")
                if isinstance(new_deadline, datetime)
                else "no deadline"
            )
            schedule_changes = await scheduler.reschedule(
                f"the deadline changed to {deadline_text}",
                (current["scheduled_start"], current["scheduled_end"]),
                trigger="deadline_shift",
            )
        result = dict(updated)
        result["schedule_changes"] = schedule_changes
        return jsonable(result)

    async def update_event(event_id: int, clear_fields: list[str], **changes: Any) -> Any:
        current = await store.get_event(event_id)
        if current is None:
            raise KeyError(f"Event {event_id} does not exist")
        payload = {key: value for key, value in changes.items() if value is not None}
        for key in ("start", "end"):
            if key in payload:
                payload[key] = _aware(payload[key], key)
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            calendar_payload = dict(payload)
            for field in clear_fields:
                calendar_payload[field] = None
            if set(calendar_payload) & {
                "title", "description", "start", "end", "start_time",
                "end_time", "location",
            }:
                await calendar.update_event(gcal_id, calendar_payload)
        payload["clear_fields"] = clear_fields
        try:
            updated = await store.update_event(event_id, payload)
        except Exception:
            if gcal_id:
                await calendar.update_event(gcal_id, {
                    "title": current.get("title"),
                    "description": current.get("description"),
                    "start_time": current.get("start_time"),
                    "end_time": current.get("end_time"),
                    "location": current.get("location"),
                })
            raise
        start = updated.get("start_time")
        end = updated.get("end_time")
        schedule_changes = (
            await scheduler.detect_conflicts(start, end)
            if isinstance(start, datetime) and isinstance(end, datetime)
            else []
        )
        result = dict(updated)
        result["schedule_changes"] = schedule_changes
        return jsonable(result)

    async def complete_task(task_id: int, actual_minutes: int | None) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_work_block(gcal_id)
        return jsonable(await store.complete_task(task_id, actual_minutes))

    async def delete_task(task_id: int) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_work_block(gcal_id)
        return jsonable(await store.delete_task(task_id))

    async def delete_event(event_id: int) -> Any:
        current = await store.get_event(event_id)
        if current is None:
            raise KeyError(f"Event {event_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_event(gcal_id)
        if not await store.delete_event(event_id):
            raise RuntimeError(f"Event {event_id} could not be deleted locally")
        return {"deleted": True, "event_id": event_id}

    async def query_schedule_tool(start: str, end: str) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        return jsonable(await query_schedule(store, calendar, start_dt, end_dt))

    async def query_tasks(
        status: str | None,
        category: str | None,
        due_before: str | None,
        due_after: str | None,
    ) -> Any:
        return jsonable(await store.query_tasks(
            status=status,
            category=category,
            due_before=_aware(due_before, "due_before"),
            due_after=_aware(due_after, "due_after"),
        ))

    async def find_free_blocks_tool(start: str, end: str, min_minutes: int) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        return jsonable(await find_free_blocks(
            store, calendar, start_dt, end_dt, min_minutes
        ))

    async def _facts_for_reason(reasoning: str) -> list[int]:
        reason_words = _words(reasoning)
        matched: list[int] = []
        for fact in await store.query_facts(active=True):
            fact_words = _words(str(fact.get("content") or ""))
            if fact_words and len(reason_words & fact_words) >= min(2, len(fact_words)):
                matched.append(int(fact["id"]))
        return matched

    async def schedule_task(
        task_id: int,
        start: str,
        end: str,
        reasoning: str,
        trigger: str,
    ) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        decision = await scheduler.schedule_task(
            task_id,
            start_dt,
            end_dt,
            reasoning,
            trigger,  # type: ignore[arg-type]
            await _facts_for_reason(reasoning),
        )
        return jsonable(decision)

    async def explain_schedule(task_id: int) -> Any:
        return jsonable(await scheduler.explain_schedule(task_id))

    async def add_fact(content: str, category: str, confidence: float, source: str) -> Any:
        return jsonable(await store.add_fact({
            "content": content, "category": category, "confidence": confidence,
            "source": source,
        }))

    async def update_fact(fact_id: int, **changes: Any) -> Any:
        return jsonable(await store.update_fact(
            fact_id, {key: value for key, value in changes.items() if value is not None}
        ))

    async def query_facts(
        category: str | None, active: bool | None, min_confidence: float | None
    ) -> Any:
        return jsonable(await store.query_facts(category, active, min_confidence))

    async def add_goal(**goal: Any) -> Any:
        return jsonable(await store.add_goal(goal))

    async def log_goal_progress(
        goal_id: int, amount: float, source: str, logged_at: str
    ) -> Any:
        moment = _aware(logged_at, "logged_at")
        assert moment is not None
        return jsonable(await store.log_goal_progress(
            goal_id, amount, source, moment  # type: ignore[arg-type]
        ))

    async def query_goals(active: bool | None, category: str | None) -> Any:
        return jsonable(await store.query_goals(active, category))

    async def resolve_date(phrase: str) -> Any:
        local = timeutil.resolve_relative(phrase)
        return {
            "phrase": phrase,
            "local": local.isoformat(),
            "utc": timeutil.to_utc(local).isoformat().replace("+00:00", "Z"),
            "timezone": str(local.tzinfo),
        }

    handlers: dict[str, ToolHandler] = {
        "add_task": add_task,
        "add_event": add_event,
        "update_task": update_task,
        "update_event": update_event,
        "complete_task": complete_task,
        "delete_task": delete_task,
        "delete_event": delete_event,
        "query_schedule": query_schedule_tool,
        "query_tasks": query_tasks,
        "find_free_blocks": find_free_blocks_tool,
        "schedule_task": schedule_task,
        "explain_schedule": explain_schedule,
        "add_fact": add_fact,
        "update_fact": update_fact,
        "query_facts": query_facts,
        "add_goal": add_goal,
        "log_goal_progress": log_goal_progress,
        "query_goals": query_goals,
        "resolve_date": resolve_date,
    }
    return handlers
