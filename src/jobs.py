"""Durable proactive jobs for daily planning, reflection, and review.

The module deliberately keeps integrations duck typed. The scheduler,
Telegram transport, and learning engine are being built independently, while
the persistence contract in :class:`Store` is already stable.

Delivery markers are written immediately after successful Telegram calls.
Telegram and SQLite cannot commit atomically, so a hard crash in that narrow
gap can still duplicate a brief/review; checklist state is recoverable through
Agent D, and process locks eliminate ordinary cron/catch-up races.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import math
import os
import re
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import MethodType
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

from . import scheduler_engine as scheduler_module
from . import timeutil
from .config import config
from .facts_engine import FactsEngine
from .scheduler_engine import SchedulerEngine
from .store import Record, Store

LOGGER = logging.getLogger(__name__)

MORNING_JOB_ID = "proactive-morning-brief"
DEBRIEF_JOB_ID = "proactive-evening-debrief"
WEEKLY_JOB_ID = "proactive-weekly-review"
PLANNING_JOB_ID = "proactive-daily-planning"
RECONCILE_JOB_ID = "proactive-calendar-reconcile"
DECISION_ACK_JOB_ID = "proactive-decision-ack"
CHANGE_ACK_JOB_ID = "proactive-change-ack"
FOLLOWUP_JOB_ID = "proactive-debrief-followup"
_CHECKLIST_PREFIX = "daily-debrief"
_RECENT_CONVERSATION = timedelta(minutes=5)
_MAX_BRIEF_CHARS = 1_350
_CHECKLIST_LIMIT = 20
_UNSURFACED_SINCE = datetime(1970, 1, 1, tzinfo=UTC)
_occurrence_locks: dict[str, asyncio.Lock] = {}
_startup_tasks: set[asyncio.Task[None]] = set()
_persistent_jobstore_enabled = False


@dataclass(slots=True)
class _Runtime:
    scheduler: Any
    store: Store
    engine: SchedulerEngine
    telegram: Any
    facts_engine: Any


class CalendarBriefIncompleteError(RuntimeError):
    """Raised when a complete all-calendar morning view cannot be verified."""


_runtime: _Runtime | None = None


def _occurrence_lock(kind: str, local_date: date) -> asyncio.Lock:
    """Serialize cron, catch-up, and deferred paths within this process."""
    key = f"{kind}:{local_date.isoformat()}"
    return _occurrence_locks.setdefault(key, asyncio.Lock())


def _zone() -> ZoneInfo:
    return ZoneInfo(config.USER_TIMEZONE)


def _clock_setting(env_name: str, fallback: str) -> tuple[int, int]:
    """Read a job clock without inheriting legacy Config defaults."""
    value = os.getenv(env_name, fallback).strip()
    match = re.fullmatch(r"(\d{1,2}):(\d{2})", value)
    if match is None:
        raise ValueError(f"{env_name} must use 24-hour HH:MM format")
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        raise ValueError(f"{env_name} must be a valid local clock time")
    return hour, minute


def _day_bounds(local_date: date) -> tuple[datetime, datetime]:
    return timeutil.day_bounds(local_date)


def _format_clock(value: datetime) -> str:
    rendered = value.astimezone(_zone()).strftime("%I:%M").lstrip("0")
    return rendered or "12:00"


def _format_span(start: datetime, end: datetime) -> str:
    return f"{_format_clock(start)}–{_format_clock(end)}"


def _one_clause(value: Any, limit: int = 105) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip(" .;—-")
    if not text:
        return "to keep the day realistic"
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip(" .")
    if len(first) > limit:
        first = first[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return first


def _short_text(value: Any, limit: int = 48) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    shortened = text[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return (shortened or text[: limit - 1]) + "…"


def _compact_items(items: list[Record], render: Callable[[Record], str], limit: int) -> str:
    visible = [render(item) for item in items[:limit]]
    overflow = len(items) - len(visible)
    if overflow:
        visible.append(f"+{overflow} more")
    return ", ".join(visible)


async def _maybe_await(value: Any) -> Any:
    return await value if inspect.isawaitable(value) else value


async def _goal_hook(engine: Any, method_name: str, reference: date | datetime) -> Any:
    """Invoke one concrete optional SchedulerEngine goal lifecycle hook."""
    method = getattr(engine, method_name, None)
    if callable(method):
        return await method(reference=reference)
    return []


async def _call_compatible(method: Callable[..., Any], **values: Any) -> Any:
    """Call an evolving integration using only keyword names it accepts."""
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return await _maybe_await(method(**values))
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        kwargs = values
    else:
        kwargs = {name: value for name, value in values.items() if name in signature.parameters}
    return await _maybe_await(method(**kwargs))


async def _send_text(telegram: Any, text: str) -> Any:
    """Send a proactive text through either Agent D or its Telegram app."""
    for name in ("send_message", "send_text", "send"):
        method = getattr(telegram, name, None)
        if callable(method):
            try:
                return await _maybe_await(method(text))
            except TypeError:
                return await _call_compatible(method, text=text, message=text)
    outbound = getattr(telegram, "_outbound_target", None)
    if callable(outbound):
        app, chat_id = outbound()
        return await app.bot.send_message(chat_id=chat_id, text=text)
    app = getattr(telegram, "app", None) or getattr(telegram, "application", None)
    chat_id = getattr(telegram, "chat_id", None) or config.ALLOWED_USER_ID
    if app is not None and getattr(app, "bot", None) is not None and chat_id is not None:
        return await app.bot.send_message(chat_id=chat_id, text=text)
    raise TypeError("Telegram transport does not expose a proactive send method")


async def _checklist_is_active(telegram: Any, callback_prefix: str) -> bool:
    """Recognize Agent D's durable checklist after a send/marker crash."""
    method = getattr(telegram, "has_active_checklist", None)
    if callable(method):
        return bool(
            await _call_compatible(
                method,
                callback_prefix=callback_prefix,
                prefix=callback_prefix,
            )
        )
    records = getattr(telegram, "_checklists", None)
    return bool(
        isinstance(records, dict)
        and any(
            isinstance(state, dict)
            and state.get("callback_prefix") == callback_prefix
            for state in records.values()
        )
    )


def _is_quiet(now: datetime | None = None) -> bool:
    local_now = (now or timeutil.now_local()).astimezone(_zone())
    start_h, start_m = _clock_setting("QUIET_HOURS_START", config.QUIET_HOURS_START)
    end_h, end_m = _clock_setting("QUIET_HOURS_END", config.QUIET_HOURS_END)
    start, end = time(start_h, start_m), time(end_h, end_m)
    current = local_now.time().replace(tzinfo=None)
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def _quiet_end(now: datetime | None = None) -> datetime:
    local_now = (now or timeutil.now_local()).astimezone(_zone())
    end_h, end_m = _clock_setting("QUIET_HOURS_END", config.QUIET_HOURS_END)
    target = datetime.combine(local_now.date(), time(end_h, end_m), _zone())
    if target <= local_now:
        target += timedelta(days=1)
    return target


async def _conversation_is_active(store: Store) -> bool:
    """Use in-process locks and the durable message timestamp when available."""
    if _runtime is not None:
        agent = getattr(_runtime.telegram, "agent", None)
        locks = getattr(agent, "_conversation_locks", {})
        if isinstance(locks, dict) and any(
            getattr(lock, "locked", lambda: False)() for lock in locks.values()
        ):
            return True
    latest: datetime | None = None
    method = getattr(store, "last_message_at", None)
    if callable(method):
        latest = await _maybe_await(method())
    elif hasattr(store, "connection"):
        try:
            async with store.connection() as db:
                cursor = await db.execute(
                    "SELECT created_at FROM messages ORDER BY created_at DESC, id DESC LIMIT 1"
                )
                row = await cursor.fetchone()
            if row:
                raw = row["created_at"] if hasattr(row, "keys") else row[0]
                latest = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except Exception:
            LOGGER.debug("Could not inspect recent conversation activity", exc_info=True)
    return bool(
        latest
        and timeutil.now_utc() - latest.astimezone(UTC) < _RECENT_CONVERSATION
    )


def _goal_is_behind(goal: Record, local_date: date) -> bool:
    progress = goal.get("progress") or {}
    done = float(progress.get("amount_done", 0))
    target = float(goal.get("target_amount", 0))
    if target <= 0:
        return False
    if goal.get("period") == "month":
        total, elapsed = monthrange(local_date.year, local_date.month)[1], local_date.day - 1
    else:
        total, elapsed = 7, local_date.weekday()
    return done + 1e-9 < target * max(0, elapsed) / total


async def _format_decision(decision: Record, task: Record) -> str:
    formatter = getattr(scheduler_module, "format_change_summary", None)
    if callable(formatter):
        for call in (
            lambda: formatter(decision),
            lambda: formatter(decision, task),
            lambda: formatter(decision=decision, task=task),
        ):
            try:
                rendered = await _maybe_await(call())
                if rendered:
                    return _one_clause(rendered)
            except TypeError:
                continue
            except Exception:
                LOGGER.warning(
                    "Could not format schedule decision %s",
                    decision.get("id"),
                    exc_info=True,
                )
                break
    LOGGER.debug(
        "format_change_summary is unavailable; using stored reasoning for decision %s",
        decision.get("id"),
    )
    return _one_clause(decision.get("reasoning"))


async def _brief_data(
    store: Store, local_date: date, calendar: Any | None = None
) -> tuple[list[Record], list[Record], list[Record], list[Record]]:
    start, end = _day_bounds(local_date)
    local_events = await store.query_events(start, end)
    all_tasks = await store.query_tasks()
    active = [task for task in all_tasks if task.get("status") not in {"completed", "dropped"}]
    due = [
        task for task in active
        if task.get("deadline") and start <= task["deadline"] < end
    ]
    blocks = [
        task for task in active
        if task.get("scheduled_start") and task.get("scheduled_end")
        and task["scheduled_start"] < end and task["scheduled_end"] > start
    ]
    blocks.sort(key=lambda task: (task["scheduled_start"], task.get("id", 0)))
    goals = [
        goal for goal in await store.query_goals(active=True)
        if _goal_is_behind(goal, local_date)
    ]
    # Locally tracked fixed commitments are the assistant's durable plan. Put
    # them ahead of supplementary Google-only entries before compacting so a
    # crowded external calendar cannot hide an event the user explicitly
    # created through Dharvis.
    events = [{**item, "_brief_local": True} for item in local_events]
    if calendar is not None:
        google_events = await calendar.list_events(start, end)
        if getattr(calendar, "_last_query_complete", True) is False:
            raise CalendarBriefIncompleteError(
                "Google Calendar could not provide a complete view; "
                "the morning brief will retry instead of hiding events"
            )
        mirrored_ids = {
            str(item.get("gcal_event_id"))
            for item in [*local_events, *all_tasks]
            if item.get("gcal_event_id")
        }
        for item in google_events:
            gcal_id = str(item.get("gcal_event_id") or item.get("id") or "")
            if gcal_id and gcal_id in mirrored_ids:
                continue
            normalized = dict(item)
            for field in ("start_time", "end_time"):
                value = normalized.get(field)
                if isinstance(value, str):
                    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                    if parsed.tzinfo is None or parsed.utcoffset() is None:
                        raise CalendarBriefIncompleteError(
                            f"Google Calendar returned a naive {field}"
                        )
                    normalized[field] = parsed.astimezone(UTC)
            # CalendarService identifies all-day entries through their local
            # midnight boundaries.  Keep the display concern here so the
            # calendar boundary remains the shared schedule shape.
            if not normalized.get("all_day"):
                event_start, event_end = (
                    normalized.get("start_time"), normalized.get("end_time")
                )
                if isinstance(event_start, datetime) and isinstance(event_end, datetime):
                    local_start = event_start.astimezone(_zone())
                    local_end = event_end.astimezone(_zone())
                    normalized["all_day"] = (
                        local_start.time() == time.min
                        and local_end.time() == time.min
                        and local_end > local_start
                    )
            events.append(normalized)
    events.sort(
        key=lambda item: (
            not item.get("_brief_local", False),
            item["start_time"],
            item["end_time"],
        )
    )
    return events, due, blocks, goals


async def _render_brief(
    store: Store,
    local_date: date,
    represented_elsewhere: set[int] | None = None,
    calendar: Any | None = None,
) -> tuple[str, list[int]]:
    events, due, blocks, goals = await _brief_data(store, local_date, calendar)
    excluded = represented_elsewhere or set()
    decisions = [
        decision
        for decision in await store.get_unsurfaced_decisions(_UNSURFACED_SINCE)
        if int(decision["id"]) not in excluded
    ]
    if not (events or due or blocks or goals or decisions):
        return "Nothing is on the plan today.", []

    by_task: dict[int, Record] = {}
    for decision in decisions:
        by_task[int(decision["task_id"])] = decision

    lines = [f"Today · {local_date.strftime('%a %b')} {local_date.day}"]
    if events:
        rendered = _compact_items(
            events,
            lambda item: (
                f"{'all day' if item.get('all_day') else _format_clock(item['start_time'])} "
                f"{_short_text(item['title'])}"
            ),
            5,
        )
        lines.append(f"Events: {rendered}")
    if due:
        lines.append(
            "Due: " + _compact_items(due, lambda item: _short_text(item["title"]), 5)
        )

    included: list[int] = []
    represented: set[int] = set()
    if blocks:
        lines.append("Work:")
        rendered_blocks = 0
        for task in blocks:
            if rendered_blocks >= 5:
                break
            block_decision = by_task.get(int(task["id"]))
            why = (
                await _format_decision(block_decision, task)
                if block_decision else "to protect focused progress"
            )
            candidate = (
                f"• {_format_span(task['scheduled_start'], task['scheduled_end'])} "
                f"{'Goal: ' if task.get('goal_id') else ''}"
                f"{_short_text(task['title'])} — {why}"
            )
            if len("\n".join([*lines, candidate])) > _MAX_BRIEF_CHARS - 650:
                break
            lines.append(candidate)
            rendered_blocks += 1
            if block_decision is not None:
                decision_id = int(block_decision["id"])
                included.append(decision_id)
                represented.add(decision_id)
        if len(blocks) > rendered_blocks:
            lines.append(f"• +{len(blocks) - rendered_blocks} more work blocks")
    if goals:
        goal_bits = []
        for goal in goals[:3]:
            done = float(goal["progress"]["amount_done"])
            target = float(goal["target_amount"])
            goal_bits.append(
                f"{_short_text(goal['title'], 36)} "
                f"({done:g}/{target:g} {goal['target_unit']})"
            )
        if len(goals) > len(goal_bits):
            goal_bits.append(f"+{len(goals) - len(goal_bits)} more")
        lines.append("Behind pace: " + ", ".join(goal_bits))

    unmatched = [
        decision for decision in decisions if int(decision["id"]) not in represented
    ]
    rendered_changes: list[str] = []
    for decision in unmatched:
        if len(rendered_changes) >= 2:
            break
        changed_task = await store.get_task(int(decision["task_id"]))
        if changed_task is None:
            continue
        summary = await _format_decision(decision, changed_task)
        rendered_changes.append(
            f"{_short_text(changed_task['title'], 34)} — {summary}"
        )
        included.append(int(decision["id"]))
    if rendered_changes:
        remaining = len(unmatched) - len(rendered_changes)
        if remaining:
            rendered_changes.append(f"+{remaining} more changes")
        lines.append("Changes: " + "; ".join(rendered_changes))

    framing: list[str] = []
    if due and events:
        framing.append("The crunch is fitting the due work around fixed events.")
    elif due:
        framing.append("The due work is today’s pressure point.")
    elif goals:
        framing.append("The main risk is letting the behind-pace goal slip another day.")
    elif events:
        framing.append("The fixed events set the shape of the day.")
    if blocks:
        framing.append(f"Protect the {_format_clock(blocks[0]['scheduled_start'])} block first.")
    lines.extend(framing[:2])
    return "\n".join(lines).rstrip(), included


_BRIEF_DECISIONS_RE = re.compile(r"\[brief-decisions:([0-9,]+)\]")
_CHANGE_DECISIONS_RE = re.compile(r"\[change-decisions:([0-9,]+)\]")


def _brief_decision_ids(notes: Any) -> list[int]:
    match = _BRIEF_DECISIONS_RE.search(str(notes or ""))
    return [int(value) for value in match.group(1).split(",")] if match else []


def _with_brief_decisions(notes: Any, decision_ids: list[int]) -> str:
    cleaned = _BRIEF_DECISIONS_RE.sub("", str(notes or "")).strip()
    marker = (
        f"[brief-decisions:{','.join(str(value) for value in decision_ids)}]"
        if decision_ids else ""
    )
    return "\n".join(part for part in (cleaned, marker) if part)


async def _retry_decision_surfacing(
    store: Store, local_date: date, notes: Any
) -> list[int]:
    pending = _brief_decision_ids(notes)
    if not pending:
        return []
    failed: list[int] = []
    for decision_id in pending:
        try:
            await store.mark_decision_surfaced(decision_id)
        except Exception:
            failed.append(decision_id)
            LOGGER.exception(
                "Decision %s remains queued for surfacing acknowledgement",
                decision_id,
            )
    latest = await store.get_daily_log(local_date) or {}
    await store.upsert_daily_log(
        local_date,
        {"notes": _with_brief_decisions(latest.get("notes"), failed) or None},
    )
    return failed


def _change_decision_ids(notes: Any) -> list[int]:
    match = _CHANGE_DECISIONS_RE.search(str(notes or ""))
    return [int(value) for value in match.group(1).split(",")] if match else []


def _with_change_decisions(notes: Any, decision_ids: list[int]) -> str:
    cleaned = _CHANGE_DECISIONS_RE.sub("", str(notes or "")).strip()
    marker = (
        f"[change-decisions:{','.join(str(value) for value in decision_ids)}]"
        if decision_ids else ""
    )
    return "\n".join(part for part in (cleaned, marker) if part)


async def _retry_change_surfacing(
    store: Store, local_date: date, notes: Any
) -> list[int]:
    pending = _change_decision_ids(notes)
    if not pending:
        return []
    failed: list[int] = []
    for decision_id in pending:
        try:
            await store.mark_decision_surfaced(decision_id)
        except Exception:
            failed.append(decision_id)
            LOGGER.exception(
                "Change-alert decision %s remains queued for acknowledgement",
                decision_id,
            )
    latest = await store.get_daily_log(local_date) or {}
    await store.upsert_daily_log(
        local_date,
        {"notes": _with_change_decisions(latest.get("notes"), failed) or None},
    )
    return failed


async def send_daily_brief(
    store: Store,
    telegram: Any,
    local_date: date,
    calendar: Any | None = None,
) -> None:
    """Serialize and send one morning dashboard occurrence."""
    async with _occurrence_lock("brief", local_date):
        await _send_daily_brief_once(store, telegram, local_date, calendar)


async def _send_daily_brief_once(
    store: Store,
    telegram: Any,
    local_date: date,
    calendar: Any | None = None,
) -> None:
    """Send the morning dashboard once and surface included decisions."""
    existing = await store.get_daily_log(local_date)
    if existing and _change_decision_ids(existing.get("notes")):
        await _retry_change_surfacing(store, local_date, existing.get("notes"))
        existing = await store.get_daily_log(local_date)
    if existing and existing.get("brief_sent_at"):
        await _retry_decision_surfacing(store, local_date, existing.get("notes"))
        return
    if _is_quiet():
        LOGGER.info("Morning brief held until quiet hours end")
        return
    represented_elsewhere = set(
        _change_decision_ids(existing.get("notes")) if existing else []
    )
    text, included_decisions = await _render_brief(
        store, local_date, represented_elsewhere, calendar
    )
    if _is_quiet():
        LOGGER.info("Morning brief entered quiet hours while rendering; holding it")
        return
    await _send_text(telegram, text)
    # Telegram and SQLite cannot share a transaction. A process crash after the
    # send but before this marker is the irreducible duplicate-delivery window.
    # Persist decision acknowledgements with the occurrence so failures remain
    # retryable without sending the brief a second time.
    latest = await store.get_daily_log(local_date) or {}
    await store.upsert_daily_log(
        local_date,
        {
            "brief_sent_at": timeutil.now_utc(),
            "notes": _with_brief_decisions(
                latest.get("notes"), included_decisions
            ) or None,
        },
    )
    if included_decisions:
        persisted = await store.get_daily_log(local_date) or {}
        await _retry_decision_surfacing(
            store, local_date, persisted.get("notes")
        )


def _scheduled_minutes(task: Record) -> int | None:
    """Infer observed duration only from an actual scheduled time span."""
    start, end = task.get("scheduled_start"), task.get("scheduled_end")
    if isinstance(start, datetime) and isinstance(end, datetime) and end > start:
        return max(1, round((end - start).total_seconds() / 60))
    return None


async def _planned_items(store: Store, local_date: date) -> list[Record]:
    start, end = _day_bounds(local_date)
    tasks = await store.query_tasks()
    selected: list[Record] = []
    seen: set[int] = set()
    for task in tasks:
        if task.get("status") == "dropped":
            continue
        scheduled = (
            task.get("scheduled_start") and task.get("scheduled_end")
            and task["scheduled_start"] < end and task["scheduled_end"] > start
        )
        due = task.get("deadline") and start <= task["deadline"] < end
        if not (scheduled or due) or int(task["id"]) in seen:
            continue
        seen.add(int(task["id"]))
        selected.append(task)
    selected.sort(
        key=lambda item: (
            item.get("scheduled_start") or item.get("deadline") or end,
            item["id"],
        )
    )
    return selected


def _checklist_item(task: Record) -> Record:
    label = str(task["title"])
    if task.get("scheduled_start") and task.get("scheduled_end"):
        label = f"{_format_span(task['scheduled_start'], task['scheduled_end'])} {label}"
    return {
        "id": int(task["id"]),
        "title": label,
        "value": {
            "task_id": int(task["id"]),
            "goal_id": task.get("goal_id"),
            "scheduled_minutes": _scheduled_minutes(task),
            "estimated_minutes": task.get("estimated_minutes"),
        },
    }


async def send_daily_debrief(store: Store, telegram: Any, local_date: date) -> None:
    """Serialize and send one evening checklist occurrence."""
    async with _occurrence_lock("debrief", local_date):
        await _send_daily_debrief_once(store, telegram, local_date)


async def _send_daily_debrief_once(
    store: Store, telegram: Any, local_date: date
) -> None:
    """Send the day's low-friction completion checklist exactly once."""
    existing = await store.get_daily_log(local_date)
    if existing and existing.get("debrief_sent_at"):
        return
    if _is_quiet():
        LOGGER.info("Evening debrief held during quiet hours")
        return
    tasks = await _planned_items(store, local_date)
    checklist_tasks = tasks[:_CHECKLIST_LIMIT]
    planned = []
    for index, task in enumerate(tasks):
        planned.append(
            _checklist_item(task)["value"]
            | {
                "title": task["title"],
                "checklist_included": index < _CHECKLIST_LIMIT,
            }
        )
    await store.upsert_daily_log(local_date, {"planned": planned})
    if _is_quiet():
        LOGGER.info("Evening debrief entered quiet hours while preparing; holding it")
        return
    if not checklist_tasks:
        await _send_text(telegram, "Nothing was planned today — no checklist needed.")
    else:
        prefix = f"{_CHECKLIST_PREFIX}:{local_date.isoformat()}"
        if await _checklist_is_active(telegram, prefix):
            LOGGER.warning(
                "Recovered active debrief checklist for %s after a missing daily marker",
                local_date,
            )
            await store.upsert_daily_log(
                local_date, {"debrief_sent_at": timeutil.now_utc()}
            )
            return
        overflow = len(tasks) - len(checklist_tasks)
        if overflow:
            await _send_text(
                telegram,
                f"The buttons cover {_CHECKLIST_LIMIT} items; +{overflow} more planned "
                "items are recorded in today’s log, not shown as buttons.",
            )
        if _is_quiet():
            LOGGER.info("Debrief checklist reached quiet hours after overflow notice")
            return
        await telegram.send_checklist(
            [_checklist_item(task) for task in checklist_tasks],
            callback_prefix=prefix,
        )
    await store.upsert_daily_log(local_date, {"debrief_sent_at": timeutil.now_utc()})


def _event_date(event: Record) -> date:
    prefix = str(event.get("callback_prefix", ""))
    match = re.fullmatch(
        rf"{re.escape(_CHECKLIST_PREFIX)}:(\d{{4}}-\d{{2}}-\d{{2}})", prefix
    )
    if match is None:
        raise ValueError("Not a daily-debrief checklist event")
    return date.fromisoformat(match.group(1))


def _processed_marker(checklist_id: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(checklist_id))[:80]
    return f"[debrief-checklist:{safe}]" if safe else ""


def _append_note(notes: Any, addition: str) -> str:
    current = str(notes or "").strip()
    return "\n".join(part for part in (current, addition.strip()) if part)


_FOLLOWUP_RE = re.compile(
    r"\[debrief-followup:([A-Za-z0-9_-]+):(miss|unexpected):"
    r"(pending|claimed|sent)\]"
)


def _followup_marker(checklist_id: Any, kind: str, state: str) -> str:
    identity = re.sub(r"[^A-Za-z0-9_-]", "", str(checklist_id))[:48] or "day"
    return f"[debrief-followup:{identity}:{kind}:{state}]"


def _pending_followup(notes: Any) -> tuple[str, str] | None:
    for match in _FOLLOWUP_RE.finditer(str(notes or "")):
        if match.group(3) == "pending":
            return match.group(1), match.group(2)
    return None


def _followup_question(kind: str) -> str:
    return (
        "What took the unexpected time today?"
        if kind == "unexpected" else "What pulled you off plan today?"
    )


def _schedule_followup(local_date: date) -> None:
    if _runtime is None:
        return
    when = _quiet_end() + timedelta(minutes=1) if _is_quiet() else _retry_time()
    _runtime.scheduler.add_job(
        _scheduled_debrief_followup,
        trigger="date",
        run_date=when,
        args=[local_date],
        id=f"{FOLLOWUP_JOB_ID}-{local_date.isoformat()}",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
    )


async def _deliver_pending_followup(
    store: Store, telegram: Any, local_date: date
) -> None:
    """Deliver a notable-day question at most once and never in quiet hours."""
    async with _occurrence_lock("debrief-followup", local_date):
        log = await store.get_daily_log(local_date) or {}
        pending = _pending_followup(log.get("notes"))
        if pending is None:
            return
        identity, kind = pending
        pending_marker = f"[debrief-followup:{identity}:{kind}:pending]"
        claimed_marker = f"[debrief-followup:{identity}:{kind}:claimed]"
        if _is_quiet():
            _schedule_followup(local_date)
            return
        notes = str(log.get("notes") or "").replace(
            pending_marker, claimed_marker
        )
        await store.upsert_daily_log(local_date, {"notes": notes})
        # Re-check immediately before the outbound call. If quiet hours began
        # during the claim write, safely return the unsent claim to pending.
        if _is_quiet():
            latest = await store.get_daily_log(local_date) or {}
            reverted = str(latest.get("notes") or "").replace(
                claimed_marker, pending_marker
            )
            await store.upsert_daily_log(local_date, {"notes": reverted})
            _schedule_followup(local_date)
            return
        await _send_text(telegram, _followup_question(kind))
        latest = await store.get_daily_log(local_date) or {}
        sent = str(latest.get("notes") or "").replace(
            claimed_marker,
            f"[debrief-followup:{identity}:{kind}:sent]",
        )
        await store.upsert_daily_log(local_date, {"notes": sent})


def _progress_markers(
    checklist_id: Any, local_date: date, task_id: int
) -> tuple[str, str, str]:
    identity = _progress_identity(checklist_id, local_date)
    stem = f"goal-progress:{identity}:{task_id}"
    return (
        f"[{stem}:pending]",
        f"[{stem}:retryable]",
        f"[{stem}:applied]",
    )


def _progress_identity(checklist_id: Any, local_date: date) -> str:
    checklist = re.sub(r"[^A-Za-z0-9_-]", "", str(checklist_id))[:48]
    return checklist or local_date.isoformat()


def _epoch_microseconds(value: datetime) -> int:
    utc = value.astimezone(UTC)
    delta = utc - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _attempt_marker(
    checklist_id: Any,
    local_date: date,
    task_id: int,
    goal_id: int,
    baseline_id: int,
    logged_at: datetime,
    amount: float,
) -> str:
    identity = _progress_identity(checklist_id, local_date)
    return (
        f"[goal-attempt:{identity}:{task_id}:{goal_id}:{baseline_id}:"
        f"{_epoch_microseconds(logged_at)}:{amount:.17g}]"
    )


def _attempt_pattern(checklist_id: Any, local_date: date, task_id: int) -> re.Pattern[str]:
    identity = re.escape(_progress_identity(checklist_id, local_date))
    return re.compile(
        rf"\[goal-attempt:{identity}:{task_id}:(\d+):(\d+):(\d+):([^\]]+)\]"
    )


def _attempt_details(
    notes: str, checklist_id: Any, local_date: date, task_id: int
) -> tuple[int, int, datetime, float] | None:
    match = _attempt_pattern(checklist_id, local_date, task_id).search(notes)
    if match is None:
        return None
    goal_id, baseline_id, epoch_us = map(int, match.group(1, 2, 3))
    logged_at = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(
        microseconds=epoch_us
    )
    try:
        amount = float(match.group(4))
    except ValueError:
        return None
    return goal_id, baseline_id, logged_at, amount


def _without_attempt(
    notes: str, checklist_id: Any, local_date: date, task_id: int
) -> str:
    return _attempt_pattern(checklist_id, local_date, task_id).sub("", notes).strip()


async def _goal_progress_baseline(store: Store, goal_id: int) -> int | None:
    try:
        async with store.connection() as db:
            cursor = await db.execute(
                "SELECT COALESCE(MAX(id), 0) AS baseline FROM goal_progress "
                "WHERE goal_id = ?",
                (goal_id,),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return int(row["baseline"] if hasattr(row, "keys") else row[0])
    except Exception:
        LOGGER.exception("Could not capture goal-progress baseline for goal %s", goal_id)
        return None


async def _goal_progress_attempt_visible(
    store: Store,
    goal_id: int,
    amount: float,
    logged_at: datetime,
    baseline_id: int,
) -> bool | None:
    utc_text = logged_at.astimezone(UTC).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    try:
        async with store.connection() as db:
            cursor = await db.execute(
                "SELECT amount FROM goal_progress WHERE goal_id = ? AND id > ? "
                "AND source = 'task' AND logged_at = ?",
                (goal_id, baseline_id, utc_text),
            )
            rows = await cursor.fetchall()
    except Exception:
        LOGGER.exception(
            "Could not reconcile goal-progress attempt for goal %s", goal_id
        )
        return None
    return any(
        math.isclose(float(row["amount"]), amount, rel_tol=1e-12, abs_tol=1e-12)
        for row in rows
    )


def _event_unplanned_minutes(event: Record) -> int:
    direct = event.get("unplanned_minutes", event.get("unexpected_minutes", 0))
    if isinstance(direct, (int, float)) and not isinstance(direct, bool):
        direct_minutes = max(0, round(direct))
        if direct_minutes:
            return direct_minutes
    total = 0
    blocks = event.get("unplanned_blocks")
    if not isinstance(blocks, list):
        return 0
    for block in blocks:
        if not isinstance(block, dict):
            continue
        minutes = block.get("actual_minutes", block.get("minutes"))
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool):
            total += max(0, round(minutes))
            continue
        start, end = block.get("start"), block.get("end")
        try:
            if isinstance(start, str):
                start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            if isinstance(end, str):
                end = datetime.fromisoformat(end.replace("Z", "+00:00"))
            if isinstance(start, datetime) and isinstance(end, datetime) and end > start:
                total += round((end - start).total_seconds() / 60)
        except (TypeError, ValueError):
            continue
    return total


async def _day_learning_evidence(
    store: Store, local_date: date
) -> tuple[list[Record], list[Record]]:
    """Return the complete conversation and scheduling trail for a local day."""
    start, end = _day_bounds(local_date)
    conversation, decisions = await asyncio.gather(
        store.get_messages_between(start, end),
        store.get_schedule_decisions_between(start, end),
    )
    return conversation, decisions


async def _extract_day(
    facts_engine: Any,
    daily_log: Record,
    conversation: list[Record],
    decisions: list[Record],
) -> None:
    method = getattr(facts_engine, "extract_from_day", None)
    if callable(method):
        await method(
            daily_log=daily_log,
            conversation=conversation,
            decisions=decisions,
        )
        return
    raise RuntimeError(
        "FactsEngine.extract_from_day is required for debrief completion; "
        "the daily log was retained and the checklist will remain retryable"
    )


async def _delete_completed_work_block(task: Record) -> None:
    """Delete an owned work block before its local id is cleared on completion."""
    gcal_event_id = str(task.get("gcal_event_id") or "").strip()
    if not gcal_event_id:
        return
    calendar = getattr(getattr(_runtime, "engine", None), "calendar", None)
    delete_work_block = getattr(calendar, "delete_work_block", None)
    if not callable(delete_work_block):
        raise RuntimeError(
            "Cannot safely complete a scheduled task while its Kalendra work block "
            "cannot be deleted; the debrief remains retryable"
        )
    # CalendarService refuses non-owned or non-movable Google events.  This
    # must precede Store.complete_task(), which deliberately clears this id.
    await delete_work_block(gcal_event_id)


async def handle_debrief_submission(
    store: Store,
    facts_engine: Any,
    telegram: Any,
    event: Record,
    session_id: str | None = None,
) -> None:
    """Apply a completed checklist idempotently and feed the learning system."""
    local_date = _event_date(event)
    log = await store.get_daily_log(local_date) or {}
    marker = _processed_marker(event.get("checklist_id"))
    user_reflection = next(
        (
            str(event[key]).strip()
            for key in ("notes", "reflection", "answer", "response")
            if isinstance(event.get(key), str) and str(event[key]).strip()
        ),
        "",
    )
    if marker and marker in str(log.get("notes") or ""):
        # A follow-up answer can arrive after the checklist callback was
        # completed.  Do not replay outcomes or goal progress, but retain the
        # answer and run a fresh, evidence-scoped extraction.
        if not user_reflection:
            return
        reflection_line = f"Debrief response: {user_reflection}"
        notes = str(log.get("notes") or "").strip()
        if reflection_line not in notes:
            notes = _append_note(notes, reflection_line)
            log = await store.upsert_daily_log(local_date, {"notes": notes})
        learning_log = dict(log)
        learning_log.update(
            {
                "date": local_date,
                "checklist_id": event.get("checklist_id"),
                "planned": log.get("planned") or [],
                "actual": log.get("completed") or [],
                "notes": user_reflection,
                "session_id": session_id,
            }
        )
        conversation, decisions = await _day_learning_evidence(store, local_date)
        await _extract_day(facts_engine, learning_log, conversation, decisions)
        return

    prior_completed = log.get("completed") or []
    known_ids = {
        int(item["task_id"])
        for item in prior_completed
        if isinstance(item, dict) and str(item.get("task_id", "")).isdigit()
    }
    completed = list(prior_completed)
    newly_completed: list[Record] = []
    planned = log.get("planned") or []
    checklist_task_ids = {
        int(item["task_id"])
        for item in planned
        if isinstance(item, dict)
        and item.get("checklist_included", True)
        and str(item.get("task_id", "")).isdigit()
    }
    raw_items = event.get("items")
    items: list[Any] = raw_items if isinstance(raw_items, list) else []
    goals_cache: list[Record] | None = None
    notes = str(log.get("notes") or "").strip()
    uncertain_progress: list[int] = []
    for item in items:
        if not isinstance(item, dict) or not item.get("checked"):
            continue
        value: dict[str, Any] = (
            item["value"] if isinstance(item.get("value"), dict) else {}
        )
        raw_id: Any = value.get("task_id", item.get("id"))
        try:
            task_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if task_id not in checklist_task_ids:
            LOGGER.warning(
                "Ignoring debrief task %s absent from the stored checklist", task_id
            )
            continue
        if task_id in known_ids:
            continue
        task = await store.get_task(task_id)
        if task is None:
            continue
        raw_minutes = value.get("actual_minutes")
        minutes = (
            int(raw_minutes)
            if isinstance(raw_minutes, (int, float)) and raw_minutes >= 0
            else _scheduled_minutes(task)
        )
        if task.get("status") != "completed":
            await _delete_completed_work_block(task)
            task = await store.complete_task(
                task_id, minutes, actual_minutes_source="debrief"
            )
        actual = {
            "task_id": task_id,
            "title": task.get("title"),
            "actual_minutes": (
                task.get("actual_minutes")
                if task.get("actual_minutes") is not None else minutes
            ),
            "goal_id": task.get("goal_id"),
        }
        completed.append(actual)
        newly_completed.append(actual)
        known_ids.add(task_id)

        goal_id = task.get("goal_id")
        if goal_id:
            if goals_cache is None:
                goals_cache = await store.query_goals(active=None)
            goal = next(
                (g for g in goals_cache if int(g["id"]) == int(goal_id)), None
            )
            if goal is not None:
                actual_minutes = int(actual["actual_minutes"] or 0)
                amount = (
                    1.0
                    if goal.get("target_unit") == "sessions"
                    else actual_minutes / 60
                )
                if amount > 0:
                    pending, retryable, applied = _progress_markers(
                        event.get("checklist_id"), local_date, task_id
                    )
                    if applied in notes:
                        continue
                    if pending in notes:
                        details = _attempt_details(
                            notes,
                            event.get("checklist_id"),
                            local_date,
                            task_id,
                        )
                        if details is None:
                            visible = None
                        else:
                            (
                                attempted_goal,
                                captured_baseline,
                                attempted_at,
                                attempted_amount,
                            ) = details
                            visible = await _goal_progress_attempt_visible(
                                store,
                                attempted_goal,
                                attempted_amount,
                                attempted_at,
                                captured_baseline,
                            )
                        if visible is True:
                            notes = _without_attempt(
                                notes,
                                event.get("checklist_id"),
                                local_date,
                                task_id,
                            ).replace(pending, applied)
                            await store.upsert_daily_log(
                                local_date, {"notes": notes}
                            )
                            continue
                        if visible is None:
                            # No exact reconciliation is possible. Preserve the
                            # in-flight claim and choose at-most-once rather than
                            # risk duplicating committed progress.
                            uncertain_progress.append(task_id)
                            LOGGER.warning(
                                "Goal progress for task %s remains ambiguous; "
                                "not inserting a duplicate",
                                task_id,
                            )
                            continue
                        notes = _without_attempt(
                            notes,
                            event.get("checklist_id"),
                            local_date,
                            task_id,
                        ).replace(pending, retryable)
                        await store.upsert_daily_log(
                            local_date, {"notes": notes}
                        )

                    baseline_id = await _goal_progress_baseline(
                        store, int(goal_id)
                    )
                    if baseline_id is None:
                        if retryable not in notes:
                            notes = _append_note(notes, retryable)
                        await store.upsert_daily_log(
                            local_date, {"notes": notes}
                        )
                        raise RuntimeError(
                            "Could not establish a safe goal-progress baseline; "
                            "the checklist remains retryable"
                        )
                    attempted_at = timeutil.now_utc()
                    attempt = _attempt_marker(
                        event.get("checklist_id"),
                        local_date,
                        task_id,
                        int(goal_id),
                        baseline_id,
                        attempted_at,
                        amount,
                    )
                    if retryable in notes:
                        notes = notes.replace(retryable, pending)
                    else:
                        notes = _append_note(notes, pending)
                    notes = _append_note(notes, attempt)
                    await store.upsert_daily_log(local_date, {"notes": notes})
                    try:
                        await store.log_goal_progress(
                            int(goal_id), amount, "task", attempted_at, task_id=task_id
                        )
                    except Exception:
                        visible = await _goal_progress_attempt_visible(
                            store,
                            int(goal_id),
                            amount,
                            attempted_at,
                            baseline_id,
                        )
                        if visible is True:
                            notes = _without_attempt(
                                notes,
                                event.get("checklist_id"),
                                local_date,
                                task_id,
                            ).replace(pending, applied)
                            await store.upsert_daily_log(
                                local_date, {"notes": notes}
                            )
                            continue
                        if visible is False:
                            notes = _without_attempt(
                                notes,
                                event.get("checklist_id"),
                                local_date,
                                task_id,
                            ).replace(pending, retryable)
                        await store.upsert_daily_log(
                            local_date, {"notes": notes}
                        )
                        raise
                    notes = _without_attempt(
                        notes,
                        event.get("checklist_id"),
                        local_date,
                        task_id,
                    ).replace(pending, applied)
                    await store.upsert_daily_log(local_date, {"notes": notes})

    planned_ids = {
        int(item["task_id"])
        for item in planned
        if isinstance(item, dict) and str(item.get("task_id", "")).isdigit()
    }
    checkable_ids = {
        int(item["task_id"])
        for item in planned
        if isinstance(item, dict)
        and item.get("checklist_included", True)
        and str(item.get("task_id", "")).isdigit()
    }
    completed_ids = {
        int(item["task_id"])
        for item in completed
        if isinstance(item, dict) and str(item.get("task_id", "")).isdigit()
    }
    checked_count = len(checkable_ids & completed_ids)
    misses = max(0, len(checkable_ids) - checked_count)
    unplanned_minutes = _event_unplanned_minutes(event)
    notable = (
        len(checkable_ids) >= 2 and misses > len(checkable_ids) / 2
    ) or (
        isinstance(unplanned_minutes, (int, float)) and unplanned_minutes >= 60
    )
    followup_kind: str | None = None
    if notable:
        followup_kind = "unexpected" if unplanned_minutes else "miss"
    # Commit task outcomes before extraction so a retried callback cannot
    # duplicate goal progress. The processed marker is deliberately written
    # only after extraction succeeds, ensuring training data is never skipped.
    if user_reflection:
        reflection_line = f"Debrief response: {user_reflection}"
        if reflection_line not in notes:
            notes = _append_note(notes, reflection_line)
    await store.upsert_daily_log(
        local_date,
        {"planned": planned, "completed": completed, "notes": notes or None},
    )

    day_payload: Record = {
        "date": local_date,
        "checklist_id": event.get("checklist_id"),
        "planned": planned,
        "actual": completed,
        "newly_completed": newly_completed,
        "completion_rate": (
            len(planned_ids & completed_ids) / len(planned_ids)
            if planned_ids else 1.0
        ),
        "checklist_completion_rate": (
            checked_count / len(checkable_ids) if checkable_ids else 1.0
        ),
        "checklist_item_count": len(checkable_ids),
        "overflow_count": max(0, len(planned) - len(checkable_ids)),
        "unplanned_minutes": unplanned_minutes,
        "goal_progress_uncertain": uncertain_progress,
        "notes": user_reflection or None,
        "session_id": session_id,
    }
    persisted_log = await store.get_daily_log(local_date) or {}
    learning_log = dict(persisted_log)
    learning_log.update(day_payload)
    conversation, decisions = await _day_learning_evidence(store, local_date)
    await _extract_day(facts_engine, learning_log, conversation, decisions)
    if marker:
        notes = _append_note(notes, marker)
    if followup_kind:
        pending_followup = _followup_marker(
            event.get("checklist_id"), followup_kind, "pending"
        )
        if not _FOLLOWUP_RE.search(notes):
            notes = _append_note(notes, pending_followup)
    if marker or followup_kind:
        await store.upsert_daily_log(
            local_date,
            {"notes": notes},
        )
    if _runtime is not None:
        try:
            await _goal_hook(
                _runtime.engine, "replan_missed_goal_sessions", timeutil.now_utc()
            )
        except Exception:
            # The 15-minute reconciliation job retries this independently;
            # a planner outage must not make a completed checklist retry.
            LOGGER.exception("missed_goal_replan_after_debrief_failed")
    if followup_kind:
        await _deliver_pending_followup(store, telegram, local_date)


async def _week_logs(store: Store, sunday: date) -> list[Record]:
    monday = sunday - timedelta(days=6)
    logs: list[Record] = []
    for offset in range(7):
        log = await store.get_daily_log(monday + timedelta(days=offset))
        if log is not None:
            logs.append(log)
    return logs


def _sentence(text: Any) -> str:
    normalized = re.sub(r"\s+", " ", str(text or "")).strip(" .!?")
    # User-authored goal/fact text can contain sentence punctuation. Flatten it
    # so the review contract remains exactly three sentences.
    return re.sub(r"[.!?]+", ",", normalized).strip(" ,") + "."


def _safe_behavior_pattern(facts: list[Record]) -> str:
    """Aggregate fact metadata without exposing private fact content."""
    buckets: dict[str, int] = {}
    for fact in facts:
        category = str(fact.get("category", "")).casefold()
        if any(
            token in category
            for token in ("time", "timing", "schedul", "calendar")
        ):
            label = "timing"
        elif any(token in category for token in ("energy", "focus", "sleep")):
            label = "energy management"
        elif any(token in category for token in ("work", "product", "task")):
            label = "work rhythm"
        elif any(token in category for token in ("habit", "routine", "behavior")):
            label = "routine"
        else:
            label = "planning behavior"
        buckets[label] = buckets.get(label, 0) + max(
            1, int(fact.get("evidence_count", 1))
        )
    if not buckets:
        return "Your behavioral pattern is still emerging from the weekly check-ins"
    label, signals = max(buckets.items(), key=lambda item: (item[1], item[0]))
    unit = "check-in" if signals == 1 else "check-ins"
    return f"Your strongest behavioral signal was {label}, backed by {signals} {unit}"


async def send_weekly_review(store: Store, telegram: Any, local_date: date) -> None:
    """Serialize and send one Sunday review occurrence."""
    async with _occurrence_lock("weekly", local_date):
        await _send_weekly_review_once(store, telegram, local_date)


async def _send_weekly_review_once(
    store: Store, telegram: Any, local_date: date
) -> None:
    """Send a restart-safe, exactly-three-sentence Sunday review."""
    if local_date.weekday() != 6:
        return
    log = await store.get_daily_log(local_date) or {}
    marker = "[weekly-review-sent]"
    if marker in str(log.get("notes") or "") or _is_quiet():
        return
    goals = await store.query_goals(active=True)
    for goal in goals:
        period_start = (
            local_date - timedelta(days=6)
            if goal.get("period") == "week"
            else local_date.replace(day=1)
        )
        goal["progress"] = await store.get_goal_progress(
            int(goal["id"]), _day_bounds(period_start)[0]
        )
    if goals:
        pieces = [
            f"{g['title']} reached {float(g['progress']['amount_done']):g} of "
            f"{float(g['target_amount']):g} {g['target_unit']}"
            for g in goals[:2]
        ]
        if len(goals) > len(pieces):
            pieces.append(f"+{len(goals) - len(pieces)} more active goals")
        goal_sentence = "Goals this week: " + "; ".join(pieces)
    else:
        goal_sentence = "No active goal target was on the board this week"
    logs = await _week_logs(store, local_date)
    planned = sum(len(item.get("planned") or []) for item in logs)
    completed = sum(len(item.get("completed") or []) for item in logs)
    rate = round(100 * completed / planned) if planned else 100
    completion_sentence = (
        f"You completed {completed} of {planned} planned items ({rate}%)"
        if planned else "You had no checklist items planned this week"
    )
    week_start, _ = _day_bounds(local_date - timedelta(days=6))
    week_end, _ = _day_bounds(local_date + timedelta(days=1))
    facts = [
        fact for fact in await store.query_facts(active=True)
        if isinstance(fact.get("last_confirmed_at"), datetime)
        and week_start <= fact["last_confirmed_at"] < week_end
    ]
    pattern_sentence = _safe_behavior_pattern(facts)
    message = " ".join(
        (_sentence(goal_sentence), _sentence(completion_sentence), _sentence(pattern_sentence))
    )
    if _is_quiet():
        LOGGER.info("Weekly review entered quiet hours while preparing; holding it")
        return
    await _send_text(telegram, message)
    await store.upsert_daily_log(
        local_date, {"notes": _append_note(log.get("notes"), marker)}
    )


async def run_daily_planning(engine: SchedulerEngine, local_date: date) -> None:
    """Run an optional goal refresh, then autonomously place the day's work."""
    await _goal_hook(engine, "refresh_goal_plan", local_date)
    await engine.plan_day(local_date)


async def reconcile_calendar(engine: SchedulerEngine) -> None:
    """Resolve conflicts, coalescing or alerting according to brief proximity."""
    start = timeutil.now_utc()
    end = start + timedelta(days=max(1, config.SCHEDULER_LOOKAHEAD_DAYS))
    await _goal_hook(engine, "replan_missed_goal_sessions", start)
    decisions = await engine.detect_conflicts(start, end)
    if decisions and _inside_brief_coalesce_window():
        LOGGER.info(
            "Coalescing %s reconciliation decision(s) into the morning brief",
            len(decisions),
        )
    elif _runtime is not None:
        await _send_change_alert(_runtime)


async def _send_change_alert(runtime: _Runtime) -> None:
    """Send one compact, durably acknowledged reconciliation batch at a time."""
    local_date = timeutil.now_local().date()
    async with _occurrence_lock("change-alert", local_date):
        log = await runtime.store.get_daily_log(local_date) or {}
        if _change_decision_ids(log.get("notes")):
            await _retry_change_surfacing(
                runtime.store, local_date, log.get("notes")
            )
            log = await runtime.store.get_daily_log(local_date) or {}
        if _change_decision_ids(log.get("notes")):
            return
        if _is_quiet() or await _conversation_is_active(runtime.store):
            LOGGER.info("Holding reconciliation alert for the next brief")
            return
        decisions = [
            decision
            for decision in await runtime.store.get_unsurfaced_decisions(
                _UNSURFACED_SINCE
            )
            if decision.get("trigger") == "conflict"
        ]
        rendered: list[str] = []
        represented: list[int] = []
        for decision in decisions:
            if len(rendered) >= 2:
                break
            task = await runtime.store.get_task(int(decision["task_id"]))
            if task is None:
                continue
            rendered.append(
                f"{_short_text(task['title'], 34)} — "
                f"{await _format_decision(decision, task)}"
            )
            represented.append(int(decision["id"]))
        if not rendered:
            return
        if len(decisions) > len(rendered):
            rendered.append(f"+{len(decisions) - len(rendered)} more in your brief")
        text = "Plan update: " + "; ".join(rendered)
        if _is_quiet() or await _conversation_is_active(runtime.store):
            LOGGER.info("Reconciliation alert guard changed; retaining it for the brief")
            return
        await _send_text(runtime.telegram, text)
        latest = await runtime.store.get_daily_log(local_date) or {}
        notes = _with_change_decisions(latest.get("notes"), represented)
        await runtime.store.upsert_daily_log(
            local_date,
            {"notes": notes},
        )
        persisted = await runtime.store.get_daily_log(local_date) or {}
        await _retry_change_surfacing(
            runtime.store, local_date, persisted.get("notes")
        )


def _inside_brief_coalesce_window(now: datetime | None = None) -> bool:
    local_now = (now or timeutil.now_local()).astimezone(_zone())
    hour, minute = _clock_setting("DAILY_BRIEF_TIME", "08:00")
    brief = datetime.combine(local_now.date(), time(hour, minute), _zone())
    window = max(0, int(os.getenv("BRIEF_COALESCE_MINUTES", "30")))
    return -timedelta(minutes=window) <= brief - local_now <= timedelta(minutes=window)


def _runtime_required() -> _Runtime:
    if _runtime is None:
        raise RuntimeError("configure_jobs must be called before proactive jobs run")
    return _runtime


def _defer(
    job_id: str,
    callback: Callable[..., Awaitable[None]],
    when: datetime,
    args: list[Any] | None = None,
) -> None:
    runtime = _runtime_required()
    runtime.scheduler.add_job(
        callback,
        trigger="date",
        run_date=when,
        args=args or [],
        id=f"{job_id}-deferred",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
        misfire_grace_time=900,
    )


def _retry_time(minutes: int = 5) -> datetime:
    return (
        _quiet_end() + timedelta(minutes=1)
        if _is_quiet()
        else timeutil.now_local() + timedelta(minutes=minutes)
    )


async def _scheduled_debrief_followup(local_date: date) -> None:
    runtime = _runtime_required()
    await _deliver_pending_followup(
        runtime.store, runtime.telegram, local_date
    )


async def _scheduled_decision_ack(local_date: date) -> None:
    runtime = _runtime_required()
    log = await runtime.store.get_daily_log(local_date) or {}
    failed = await _retry_decision_surfacing(
        runtime.store, local_date, log.get("notes")
    )
    if failed:
        _defer(
            DECISION_ACK_JOB_ID,
            _scheduled_decision_ack,
            _retry_time(),
            [local_date],
        )


async def _scheduled_change_ack(local_date: date) -> None:
    runtime = _runtime_required()
    log = await runtime.store.get_daily_log(local_date) or {}
    failed = await _retry_change_surfacing(
        runtime.store, local_date, log.get("notes")
    )
    if failed:
        _defer(
            CHANGE_ACK_JOB_ID,
            _scheduled_change_ack,
            _retry_time(),
            [local_date],
        )


async def _scheduled_morning() -> None:
    await _scheduled_morning_for(timeutil.now_local().date())


async def _scheduled_morning_for(local_date: date) -> None:
    runtime = _runtime_required()
    if _is_quiet():
        _defer(
            MORNING_JOB_ID,
            _scheduled_morning_for,
            _quiet_end() + timedelta(minutes=1),
            [local_date],
        )
        return
    if await _conversation_is_active(runtime.store):
        _defer(
            MORNING_JOB_ID,
            _scheduled_morning_for,
            timeutil.now_local() + timedelta(minutes=5),
            [local_date],
        )
        return
    try:
        await _goal_hook(runtime.engine, "refresh_goal_plan", local_date)
        await send_daily_brief(
            runtime.store,
            runtime.telegram,
            local_date,
            getattr(runtime.engine, "calendar", None),
        )
    except Exception:
        _defer(
            MORNING_JOB_ID,
            _scheduled_morning_for,
            _retry_time(),
            [local_date],
        )
        raise
    log = await runtime.store.get_daily_log(local_date)
    if not log or not log.get("brief_sent_at"):
        _defer(
            MORNING_JOB_ID,
            _scheduled_morning_for,
            _retry_time(),
            [local_date],
        )
    elif _brief_decision_ids(log.get("notes")):
        _defer(
            DECISION_ACK_JOB_ID,
            _scheduled_decision_ack,
            _retry_time(),
            [local_date],
        )


async def _scheduled_debrief() -> None:
    await _scheduled_debrief_for(timeutil.now_local().date())


async def _scheduled_debrief_for(local_date: date) -> None:
    runtime = _runtime_required()
    if _is_quiet():
        _defer(
            DEBRIEF_JOB_ID,
            _scheduled_debrief_for,
            _quiet_end() + timedelta(minutes=1),
            [local_date],
        )
        return
    try:
        await send_daily_debrief(runtime.store, runtime.telegram, local_date)
    except Exception:
        _defer(
            DEBRIEF_JOB_ID,
            _scheduled_debrief_for,
            _retry_time(),
            [local_date],
        )
        raise
    log = await runtime.store.get_daily_log(local_date)
    if not log or not log.get("debrief_sent_at"):
        _defer(
            DEBRIEF_JOB_ID,
            _scheduled_debrief_for,
            _retry_time(),
            [local_date],
        )


async def _scheduled_weekly() -> None:
    await _scheduled_weekly_for(timeutil.now_local().date())


async def _scheduled_weekly_for(local_date: date) -> None:
    runtime = _runtime_required()
    if _is_quiet():
        _defer(
            WEEKLY_JOB_ID,
            _scheduled_weekly_for,
            _quiet_end() + timedelta(minutes=2),
            [local_date],
        )
        return
    try:
        await send_weekly_review(runtime.store, runtime.telegram, local_date)
    except Exception:
        _defer(
            WEEKLY_JOB_ID,
            _scheduled_weekly_for,
            _retry_time(),
            [local_date],
        )
        raise
    log = await runtime.store.get_daily_log(local_date) or {}
    if "[weekly-review-sent]" not in str(log.get("notes") or ""):
        _defer(
            WEEKLY_JOB_ID,
            _scheduled_weekly_for,
            _retry_time(),
            [local_date],
        )


async def _scheduled_planning() -> None:
    runtime = _runtime_required()
    await run_daily_planning(runtime.engine, timeutil.now_local().date())


async def _scheduled_reconcile() -> None:
    await reconcile_calendar(_runtime_required().engine)


def _register_completion_handler(runtime: _Runtime) -> None:
    async def callback(event: Record, session_id: str | None = None) -> None:
        await handle_debrief_submission(
            runtime.store,
            runtime.facts_engine,
            runtime.telegram,
            event,
            session_id,
        )

    for owner in (runtime.telegram, getattr(runtime.telegram, "agent", None)):
        register = getattr(owner, "register_checklist_handler", None)
        if callable(register):
            register(_CHECKLIST_PREFIX, callback)
            return
    agent = getattr(runtime.telegram, "agent", None)
    if agent is None:
        return
    original = getattr(agent, "handle_checklist_completion", None)
    if not callable(original):
        original = getattr(agent, "on_checklist_completed", None)

    async def routed(_self: Any, event: Record, session_id: str) -> None:
        if str(event.get("callback_prefix", "")).startswith(f"{_CHECKLIST_PREFIX}:"):
            await callback(event, session_id)
        elif callable(original):
            await _maybe_await(original(event, session_id))
        else:
            raise ValueError("Unknown checklist callback prefix")

    agent.handle_checklist_completion = MethodType(routed, agent)


def _job_defaults() -> Record:
    return {
        "replace_existing": True,
        "coalesce": True,
        "max_instances": 1,
        "misfire_grace_time": 900,
    }


def configure_jobs(
    scheduler: Any,
    store: Store,
    engine: SchedulerEngine,
    telegram: Any,
    facts_engine: Any | None = None,
) -> None:
    """Register stable, coalescing jobs using the user's local timezone."""
    global _runtime
    learning = (
        facts_engine
        or getattr(getattr(telegram, "agent", None), "facts_engine", None)
        or FactsEngine(store)
    )
    _runtime = _Runtime(scheduler, store, engine, telegram, learning)
    if not callable(getattr(learning, "extract_from_day", None)):
        LOGGER.error(
            "Proactive jobs configured without FactsEngine.extract_from_day; "
            "debrief submissions will remain retryable instead of discarding training data"
        )
    _register_completion_handler(_runtime)

    try:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to enable proactive jobs") from exc

    zone = _zone()
    morning_h, morning_m = _clock_setting("DAILY_BRIEF_TIME", "08:00")
    debrief_h, debrief_m = _clock_setting("DAILY_DEBRIEF_TIME", "21:30")
    weekly_h, weekly_m = _clock_setting("WEEKLY_REVIEW_TIME", "20:30")
    planning_at = datetime.combine(
        date.today(), time(morning_h, morning_m)
    ) - timedelta(minutes=15)
    defaults = _job_defaults()
    scheduler.add_job(
        _scheduled_planning,
        CronTrigger(
            hour=planning_at.hour, minute=planning_at.minute, timezone=zone
        ),
        id=PLANNING_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_morning,
        CronTrigger(hour=morning_h, minute=morning_m, timezone=zone),
        id=MORNING_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_debrief,
        CronTrigger(hour=debrief_h, minute=debrief_m, timezone=zone),
        id=DEBRIEF_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_weekly,
        CronTrigger(
            day_of_week="sun", hour=weekly_h, minute=weekly_m, timezone=zone
        ),
        id=WEEKLY_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_reconcile,
        IntervalTrigger(minutes=15, timezone=zone),
        id=RECONCILE_JOB_ID,
        **defaults,
    )


async def run_startup_catchup() -> None:
    """Backfill the latest durable daily and weekly occurrences after restart."""
    runtime = _runtime_required()
    now, today = timeutil.now_local(), timeutil.now_local().date()
    await _goal_hook(runtime.engine, "replan_missed_goal_sessions", now)
    morning_h, morning_m = _clock_setting("DAILY_BRIEF_TIME", "08:00")
    debrief_h, debrief_m = _clock_setting("DAILY_DEBRIEF_TIME", "21:30")
    weekly_h, weekly_m = _clock_setting("WEEKLY_REVIEW_TIME", "20:30")

    for offset in range(0, 8):
        marker_date = today - timedelta(days=offset)
        marker_log = await runtime.store.get_daily_log(marker_date)
        if marker_log and _brief_decision_ids(marker_log.get("notes")):
            await _scheduled_decision_ack(marker_date)
        if marker_log and _change_decision_ids(marker_log.get("notes")):
            await _scheduled_change_ack(marker_date)
        if marker_log and _pending_followup(marker_log.get("notes")):
            await _scheduled_debrief_followup(marker_date)

    if now >= datetime.combine(today, time(morning_h, morning_m), _zone()):
        today_log = await runtime.store.get_daily_log(today)
        if not today_log or not today_log.get("brief_sent_at"):
            await run_daily_planning(runtime.engine, today)
        await _scheduled_morning_for(today)

    # The most recent elapsed debrief occurrence is relevant even when Railway
    # died before any daily_log row was created. The send path creates the full
    # planned snapshot and its durable occurrence marker under the date lock.
    if now >= datetime.combine(today, time(debrief_h, debrief_m), _zone()):
        debrief_date = today
    else:
        debrief_date = today - timedelta(days=1)
    await _scheduled_debrief_for(debrief_date)

    # Backfill the most recent elapsed Sunday, including yesterday after a
    # Sunday-night Railway restart. Absence of a Sunday row is itself allowed:
    # the review send path creates it and writes the durable sent marker.
    for offset in range(0, 8):
        candidate = today - timedelta(days=offset)
        if candidate.weekday() != 6:
            continue
        occurrence = datetime.combine(candidate, time(weekly_h, weekly_m), _zone())
        if occurrence > now:
            continue
        await _scheduled_weekly_for(candidate)
        break


def _finish_startup_task(task: asyncio.Task[None]) -> None:
    _startup_tasks.discard(task)
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception:
        LOGGER.exception("Proactive-job startup catch-up failed")


def start_job_scheduler(scheduler: Any, *, catch_up: bool = True) -> Any:
    """Start the configured scheduler and enqueue safe startup catch-up jobs."""
    result = scheduler.start()
    if catch_up:
        try:
            task = asyncio.get_running_loop().create_task(run_startup_catchup())
        except RuntimeError:
            LOGGER.error(
                "Scheduler started outside an event loop; call await "
                "run_startup_catchup() from application startup"
            )
        else:
            _startup_tasks.add(task)
            task.add_done_callback(_finish_startup_task)
    return result


def jobs_integration_status() -> Record:
    """Return lifecycle/readiness facts for the application startup layer."""
    learning_ready = bool(
        _runtime
        and callable(getattr(_runtime.facts_engine, "extract_from_day", None))
    )
    return {
        "configured": _runtime is not None,
        "learning_ready": learning_ready,
        "persistent_jobstore": _persistent_jobstore_enabled,
        "required_start_hook": "start_job_scheduler",
        "required_shutdown_hook": "shutdown_job_scheduler",
        "catchup_hook": "run_startup_catchup",
    }


def assert_jobs_ready() -> None:
    """Fail application startup if required proactive-job integrations are absent."""
    status = jobs_integration_status()
    missing = [
        name for name in ("configured", "learning_ready") if not status[name]
    ]
    if missing:
        raise RuntimeError(
            "Proactive jobs are not integration-ready: " + ", ".join(missing)
        )


def shutdown_job_scheduler(scheduler: Any, *, wait: bool = True) -> Any:
    """Shut down without failing an already-stopped process."""
    for task in tuple(_startup_tasks):
        task.cancel()
    try:
        return scheduler.shutdown(wait=wait)
    except Exception as exc:
        if exc.__class__.__name__ != "SchedulerNotRunningError":
            raise
        return None


start_jobs = start_job_scheduler
shutdown_jobs = shutdown_job_scheduler


def create_job_scheduler() -> Any:
    """Create a timezone scheduler, using a persistent job store when available."""
    global _persistent_jobstore_enabled
    _persistent_jobstore_enabled = False
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to enable proactive jobs") from exc

    kwargs: Record = {"timezone": config.USER_TIMEZONE}
    try:
        from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

        database_path = str(config.DATABASE_PATH)
        if database_path != ":memory:":
            jobs_path = str(config.APSCHEDULER_DATABASE_PATH)
            kwargs["jobstores"] = {
                "default": SQLAlchemyJobStore(url=f"sqlite:///{jobs_path}")
            }
            _persistent_jobstore_enabled = True
    except ImportError:
        # APScheduler 3 has no stdlib SQLite JobStore. Implementing one here
        # would require duplicating its private Job serialization contract and
        # would be less reliable than durable occurrence markers plus catch-up.
        LOGGER.warning(
            "PERSISTENT APSCHEDULER JOB STORE UNAVAILABLE (SQLAlchemy missing); "
            "using in-memory cron state with durable daily-log markers and catch-up"
        )
    if str(config.DATABASE_PATH) == ":memory:":
        LOGGER.warning(
            "In-memory application database disables durable proactive-job markers"
        )
    return AsyncIOScheduler(**kwargs)
