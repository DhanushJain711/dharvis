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
import json
import logging
import os
import re
import sqlite3
import tempfile
from calendar import monthrange
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from types import MethodType
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

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
BACKUP_JOB_ID = "proactive-daily-backup"
NIGHTLY_FACTS_JOB_ID = "proactive-nightly-facts"
REMINDER_DISPATCH_JOB_ID = "proactive-reminder-dispatch"
_CHECKLIST_PREFIX = "daily-debrief"
_RECENT_CONVERSATION = timedelta(minutes=5)
_MAX_BRIEF_CHARS = 1_350
_CHECKLIST_LIMIT = 20
_UNSURFACED_SINCE = datetime(1970, 1, 1, tzinfo=UTC)
_REMINDER_LEASE = timedelta(minutes=2)
_REMINDER_BATCH_SIZE = 20
_MATERIALLY_LATE = timedelta(minutes=5)
_occurrence_locks: dict[str, asyncio.Lock] = {}
_startup_tasks: set[asyncio.Task[None]] = set()
_persistent_jobstore_enabled = False
_managed_scheduler: Any | None = None
_notification_times: dict[str, str] = {}
_notification_lock = asyncio.Lock()
FACTS_RETRY_JOB_ID = "proactive-debrief-facts-retry"


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


def _notification_clock(kind: str) -> tuple[int, int]:
    """Use the saved user choice, falling back to the deployment setting."""
    value = _notification_times.get(kind)
    if value is not None:
        hour, minute = value.split(":")
        return int(hour), int(minute)
    name = "DAILY_BRIEF_TIME" if kind == "morning" else "DAILY_DEBRIEF_TIME"
    return _clock_setting(name, getattr(config, name))


async def load_notification_times(store: Store) -> None:
    """Load durable local notification clocks before registering cron jobs."""
    global _notification_times
    _notification_times = dict(await store.get_notification_times())


def _register_daily_clocks(scheduler: Any) -> None:
    """Replace only the three clock-dependent jobs, preserving stable IDs."""
    from apscheduler.triggers.cron import CronTrigger

    morning_h, morning_m = _notification_clock("morning")
    evening_h, evening_m = _notification_clock("evening")
    planning_minutes = (morning_h * 60 + morning_m - 15) % (24 * 60)
    for callback, job_id, hour, minute in (
        (_scheduled_planning, PLANNING_JOB_ID, *divmod(planning_minutes, 60)),
        (_scheduled_morning, MORNING_JOB_ID, morning_h, morning_m),
        (_scheduled_debrief, DEBRIEF_JOB_ID, evening_h, evening_m),
    ):
        scheduler.add_job(
            callback,
            CronTrigger(hour=hour, minute=minute, timezone=_zone()),
            id=job_id,
            **_job_defaults(),
        )


async def update_notification_times(
    store: Store, *, morning: str | None = None, evening: str | None = None
) -> dict[str, str]:
    """Save local HH:MM choices and refresh cron jobs without resending today.

    Times inside quiet hours are rejected rather than silently shifted. Daily
    delivery markers remain authoritative after edits and process restarts.
    """
    global _notification_times
    for value in (morning, evening):
        if value is None:
            continue
        if not isinstance(value, str) or re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value) is None:
            raise ValueError("Use a local time like 08:30 or 20:00.")
        hour, minute = map(int, value.split(":"))
        probe = datetime.combine(timeutil.now_local().date(), time(hour, minute), _zone())
        if _is_quiet(probe):
            raise ValueError(
                f"{value} is during your quiet hours "
                f"({config.QUIET_HOURS_START}–{config.QUIET_HOURS_END}). Choose a time outside them."
            )
    async with _notification_lock:
        saved = await store.set_notification_times(morning=morning, evening=evening)
        _notification_times = dict(saved)
        if _runtime is not None and getattr(_runtime, "store", None) is store:
            _register_daily_clocks(_runtime.scheduler)
            # A previously deferred clock occurrence must not ignore the new
            # preference. Successful deliveries retain their durable markers.
            for job_id in (MORNING_JOB_ID, DEBRIEF_JOB_ID, PLANNING_JOB_ID):
                deferred = f"{job_id}-deferred"
                if _runtime.scheduler.get_job(deferred) is not None:
                    _runtime.scheduler.remove_job(deferred)
        return saved


def _day_bounds(local_date: date) -> tuple[datetime, datetime]:
    return timeutil.day_bounds(local_date)


def _format_clock(value: datetime) -> str:
    local = timeutil.to_local(value)
    rendered = local.strftime("%I:%M%p").lstrip("0").lower()
    return rendered.replace(":00", "")


def _format_span(start: datetime, end: datetime) -> str:
    local_start, local_end = timeutil.to_local(start), timeutil.to_local(end)
    end_label = _format_clock(end)
    if local_end.date() != local_start.date():
        end_label = f"{local_end.strftime('%a')} {end_label}"
    if local_start.utcoffset() != local_end.utcoffset():
        return f"{_format_clock(start)} {local_start.tzname()}–{end_label} {local_end.tzname()}"
    return f"{_format_clock(start)}–{end_label}"


_ISO_IN_TEXT = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})"
)


def _humanize_timestamps(text: str) -> str:
    """Render aware ISO timestamps embedded in saved prose using local dates."""
    def render(match: re.Match[str]) -> str:
        try:
            value = timeutil.to_local(datetime.fromisoformat(match[0].replace("Z", "+00:00")))
        except ValueError:
            return "the saved time"
        return f"{value.strftime('%a %b')} {value.day} at {_format_clock(value)}"

    return _ISO_IN_TEXT.sub(render, text)


def _one_clause(value: Any, limit: int = 105) -> str:
    text = re.sub(r"\s+", " ", _humanize_timestamps(str(value or ""))).strip(" .;—-")
    if not text:
        return ""
    first = re.split(r"(?<=[.!?])\s+", text, maxsplit=1)[0].strip(" .")
    if len(first) > limit:
        first = first[: limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return first


def _short_text(value: Any, limit: int = 48) -> str:
    text = re.sub(r"\s+", " ", _humanize_timestamps(str(value or ""))).strip()
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


def _format_reminder_due(value: datetime) -> str:
    """Render a reminder's original due time in the user's local timezone."""
    local = value.astimezone(_zone())
    clock = local.strftime("%I:%M %p").lstrip("0")
    return f"{local.strftime('%a %b')} {local.day} at {clock}"


def _reminder_retry_at(attempts: int, failed_at: datetime) -> datetime:
    """Return a bounded exponential retry time for a failed delivery."""
    exponent = min(7, max(0, attempts - 1))
    delay_seconds = min(3_600, 30 * (2 ** exponent))
    return failed_at + timedelta(seconds=delay_seconds)


async def deliver_due_reminders(
    store: Store,
    telegram: Any,
    *,
    now: datetime | None = None,
) -> int:
    """Claim and deliver a bounded batch of durable, due reminders.

    Explicit reminder times are honored even during quiet hours or an active
    conversation. A reminder is acknowledged only after Telegram accepts the
    send; failures are released for a capped exponential retry, and one bad
    delivery never blocks the remainder of the claimed batch.
    """
    reference = timeutil.to_utc(now or timeutil.now_utc())
    reminders = await store.claim_due_reminders(
        reference,
        lease_for=_REMINDER_LEASE,
        limit=_REMINDER_BATCH_SIZE,
    )
    delivered = 0
    for reminder in reminders:
        reminder_id = int(reminder["id"])
        claim_token = reminder.get("lease_token") or reminder.get("claim_token")
        if not claim_token:
            LOGGER.error("Claimed reminder %s has no lease token", reminder_id)
            continue
        due = reminder.get("remind_at")
        text = f"reminder: {_short_text(reminder.get('message'), 800)}"
        if isinstance(due, datetime) and reference - due.astimezone(UTC) >= _MATERIALLY_LATE:
            text += f" — set for {_format_reminder_due(due)}"
        try:
            await _send_text(telegram, text)
        except Exception as exc:
            failed_at = timeutil.now_utc()
            attempts = max(1, int(reminder.get("delivery_attempts") or 1))
            try:
                await store.release_reminder_delivery(
                    reminder_id,
                    str(claim_token),
                    failed_at,
                    _reminder_retry_at(attempts, failed_at),
                )
            except Exception:
                LOGGER.exception(
                    "Could not release failed reminder claim id=%s", reminder_id
                )
            LOGGER.warning(
                "Reminder delivery failed id=%s error_type=%s",
                reminder_id,
                type(exc).__name__,
            )
            continue
        try:
            await store.ack_reminder_delivery(
                reminder_id,
                str(claim_token),
                delivered_at=timeutil.now_utc(),
            )
        except Exception:
            # Keep the lease intact. If the process survives, the reminder is
            # retried after expiry; a duplicate is safer than a silent loss.
            LOGGER.exception("Could not acknowledge reminder delivery id=%s", reminder_id)
            continue
        delivered += 1
    return delivered


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
    """Return just the saved cause, never a second assignment confirmation."""
    reason = _humanize_timestamps(str(decision.get("reasoning") or "")).strip()
    # Scheduling summaries already include the title and placement. Using
    # their output after a brief's title/time repeats the entire assignment.
    # Legacy stored reasons can also contain that phrasing; retain its actual
    # causal aside when present instead of inventing one.
    for separator in (" — ", " – ", " because ", " since "):
        if separator in reason and re.match(
            r"(?i)\s*(?:i\s+)?(?:put|moved|scheduled|placed|rescheduled)\b", reason
        ):
            reason = reason.split(separator, 1)[1]
            break
    limit = 240 if config.REASONING_VERBOSITY == "full" else 120
    return _one_clause(reason, limit)


async def _format_change(decision: Record, task: Record) -> str:
    """Keep the actual move visible alongside its saved causal aside."""
    reason = await _format_decision(decision, task)
    if decision.get("action") == "unscheduled":
        change = "taken off the calendar"
    else:
        start, end = decision.get("start"), decision.get("end")
        if not isinstance(start, datetime) or not isinstance(end, datetime):
            return reason
        day = timeutil.to_local(start).strftime("%a")
        verb = "moved to" if decision.get("action") == "moved" else "now"
        change = f"{verb} {day} {_format_span(start, end)}"
    return change + (f" — {reason}" if reason else "")


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


async def _brief_reminders(store: Store, local_date: date) -> list[Record]:
    """Read overdue and near-term reminders without claiming or mutating them."""
    query = getattr(store, "query_reminders", None)
    if not callable(query):
        # Compatibility for lightweight test doubles and staged deployments;
        # the concrete Store always provides this method.
        return []
    _, window_end = _day_bounds(local_date + timedelta(days=2))
    reminders = await query(status="pending", remind_before=window_end)
    return sorted(
        reminders,
        key=lambda item: (item["remind_at"], int(item.get("id", 0))),
    )


def _render_brief_reminder(reminder: Record, local_date: date) -> str:
    due = reminder["remind_at"].astimezone(_zone())
    title = _short_text(reminder.get("message"), 48)
    if due.date() < local_date:
        return f"overdue {title} (was {_format_reminder_due(reminder['remind_at'])})"
    if due.date() == local_date:
        return f"{_format_clock(reminder['remind_at'])} {title}"
    if due.date() == local_date + timedelta(days=1):
        return f"tomorrow {_format_clock(reminder['remind_at'])} {title}"
    return f"{due.strftime('%a')} {_format_clock(reminder['remind_at'])} {title}"


async def _render_brief(
    store: Store,
    local_date: date,
    represented_elsewhere: set[int] | None = None,
    calendar: Any | None = None,
) -> tuple[str, list[int]]:
    events, due, blocks, goals = await _brief_data(store, local_date, calendar)
    reminders = await _brief_reminders(store, local_date)
    excluded = represented_elsewhere or set()
    decisions = [
        decision
        for decision in await store.get_unsurfaced_decisions(_UNSURFACED_SINCE)
        if int(decision["id"]) not in excluded
    ]
    if not (events or due or blocks or goals or reminders or decisions):
        return "Nothing is on the plan today.", []

    by_task: dict[int, Record] = {}
    for decision in decisions:
        by_task[int(decision["task_id"])] = decision

    lines = [f"Today · {local_date.strftime('%a %b')} {local_date.day}"]
    if events:
        lines.append("\nEvents:")
        for item in events[:5]:
            span = "all day" if item.get("all_day") else _format_span(item["start_time"], item["end_time"])
            lines.append(f"{span} · {_short_text(item['title'], 64)}")
        if len(events) > 5:
            lines.append(f"+{len(events) - 5} more events")
    if due:
        lines.append(
            "\nDue today: " + _compact_items(due, lambda item: _short_text(item["title"]), 5)
        )
    if reminders:
        lines.append(
            "\nReminders:\n" + "\n".join(
                _render_brief_reminder(item, local_date) for item in reminders[:5]
            ) + (f"\n+{len(reminders) - 5} more reminders" if len(reminders) > 5 else "")
        )

    included: list[int] = []
    represented: set[int] = set()
    if blocks:
        lines.append("\nWork:")
        rendered_blocks = 0
        for task in blocks:
            if rendered_blocks >= 5:
                break
            block_decision = by_task.get(int(task["id"]))
            if block_decision is not None and (
                block_decision.get("action") == "unscheduled"
                or block_decision.get("start") != task["scheduled_start"]
                or block_decision.get("end") != task["scheduled_end"]
            ):
                block_decision = None
            if block_decision is None:
                # Reasons already surfaced still describe today's placement;
                # they must not be replaced with a generic fabricated reason.
                history = await store.get_schedule_decisions(int(task["id"]))
                block_decision = next((item for item in reversed(history)
                    if item.get("action") != "unscheduled"
                    and item.get("start") == task["scheduled_start"]
                    and item.get("end") == task["scheduled_end"]), None)
            why = (
                await _format_decision(block_decision, task)
                if block_decision else ""
            )
            candidate = (
                f"{_format_span(task['scheduled_start'], task['scheduled_end'])} · "
                f"{_short_text(task['title'])}" + (f" — {why}" if why else "")
            )
            if len("\n".join([*lines, candidate])) > _MAX_BRIEF_CHARS - 250:
                break
            lines.append(candidate)
            rendered_blocks += 1
            if block_decision is not None and not block_decision.get("surfaced_to_user"):
                decision_id = int(block_decision["id"])
                included.append(decision_id)
                represented.add(decision_id)
        if len(blocks) > rendered_blocks:
            lines.append(f"+{len(blocks) - rendered_blocks} more work blocks")
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
        lines.append("\nGoals to catch up on: " + ", ".join(goal_bits))

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
        summary = await _format_change(decision, changed_task)
        rendered_changes.append(
            f"{_short_text(changed_task['title'], 34)} — {summary}"
        )
        included.append(int(decision["id"]))
    if rendered_changes:
        remaining = len(unmatched) - len(rendered_changes)
        if remaining:
            rendered_changes.append(f"+{remaining} more changes")
        lines.append("\nChanges:\n" + "\n".join(rendered_changes))
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
        await _send_text(telegram, "Nothing to check off today. How did the day go?")
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
                f"Showing the first {_CHECKLIST_LIMIT} tasks here. There are {overflow} "
                "more — you can tell me about those in a message.",
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
        try:
            await _send_text(telegram, _followup_question(kind))
        except Exception:
            latest = await store.get_daily_log(local_date) or {}
            await store.upsert_daily_log(local_date, {"notes": str(latest.get("notes") or "").replace(
                claimed_marker, pending_marker
            )})
            _schedule_followup(local_date)
            raise
        latest = await store.get_daily_log(local_date) or {}
        sent = str(latest.get("notes") or "").replace(
            claimed_marker,
            f"[debrief-followup:{identity}:{kind}:sent]",
        )
        await store.upsert_daily_log(local_date, {"notes": sent})


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
        "FactsEngine.extract_from_day is unavailable; saved debrief learning will retry"
    )


async def _save_debrief_learning(
    store: Store, local_date: date, changes: Record, metadata: Record,
) -> None:
    """Save outcomes and the exact producing evidence before acknowledging Done."""
    conversation, decisions = await _day_learning_evidence(store, local_date)
    await store.save_debrief_learning(local_date, changes, {
        "metadata": metadata,
        "conversation": conversation,
        "decisions": decisions,
    })


async def _retry_debrief_learning(store: Store, facts_engine: Any, local_date: date) -> None:
    """Retry immutable snapshots; newly arriving conversation cannot alter a retry."""
    for attempt in await store.get_pending_debrief_learning(local_date):
        try:
            snapshot = attempt["snapshot"]
            persisted = snapshot["daily_log"]
            invalidated = False
            for completed in persisted.get("completed") or []:
                if not isinstance(completed, dict) or type(completed.get("task_id")) is not int:
                    continue
                task = await store.get_task(completed["task_id"])
                if (task and task.get("reopened_at")
                        and task["reopened_at"] > attempt["created_at"]):
                    invalidated = True
                    break
            if invalidated:
                # An explicit undo invalidates this observation. Do not rewrite
                # its snapshot and accidentally turn a retry into new evidence.
                await store.ack_debrief_learning(attempt["id"])
                continue
            notes = str(persisted.get("notes") or "")
            # Delivery markers are not user observations. Sanitization only
            # uses immutable queued data, including on an acknowledgement retry.
            reflections = [line.removeprefix("Debrief response: ")
                for line in notes.splitlines() if line.startswith("Debrief response: ")]
            learning_log = {**persisted, **snapshot.get("metadata", {}),
                "date": local_date, "planned": persisted.get("planned") or [],
                "actual": persisted.get("completed") or [],
                "notes": "\n".join(reflections) or None}
            planned_ids = {item["task_id"] for item in learning_log["planned"]
                if isinstance(item, dict) and type(item.get("task_id")) is int}
            checkable_ids = {item["task_id"] for item in learning_log["planned"]
                if isinstance(item, dict) and type(item.get("task_id")) is int
                and item.get("checklist_included", True)}
            completed_ids = {item["task_id"] for item in learning_log["actual"]
                if isinstance(item, dict) and type(item.get("task_id")) is int}
            # The atomic save may remove a concurrently reopened completion;
            # rates must describe the persisted snapshot, not pre-save guesses.
            learning_log["completion_rate"] = (
                len(planned_ids & completed_ids) / len(planned_ids) if planned_ids else 1.0
            )
            learning_log["checklist_completion_rate"] = (
                len(checkable_ids & completed_ids) / len(checkable_ids) if checkable_ids else 1.0
            )
            await asyncio.wait_for(_extract_day(
                facts_engine, learning_log, snapshot["conversation"], snapshot["decisions"],
            ), timeout=30)
            await store.ack_debrief_learning(attempt["id"])
        except Exception:
            LOGGER.exception("debrief_learning_deferred date=%s", local_date.isoformat())
            break


async def _scheduled_debrief_learning() -> None:
    """Recover pending learning after restarts, including days older than a week."""
    runtime = _runtime_required()
    pending_attempts = getattr(runtime.store, "get_pending_debrief_learning", None)
    if not callable(pending_attempts):
        return
    pending = await pending_attempts()
    for day in dict.fromkeys(str(row["local_date"]) for row in pending):
        local_date = date.fromisoformat(day)
        # Model work must never hold the checklist's save/acknowledgement lock.
        async with _occurrence_lock("debrief-learning", local_date):
            await _retry_debrief_learning(runtime.store, runtime.facts_engine, local_date)


async def handle_debrief_submission(
    store: Store,
    facts_engine: Any,
    telegram: Any,
    event: Record,
    session_id: str | None = None,
) -> None:
    """Save checklist outcomes; retry optional learning and cleanup separately."""
    async with _occurrence_lock("debrief-processing", _event_date(event)):
        await _handle_debrief_submission(store, facts_engine, telegram, event, session_id)


async def _handle_debrief_submission(
    store: Store,
    facts_engine: Any,
    telegram: Any,
    event: Record,
    session_id: str | None,
) -> None:
    from .integration import complete_task_with_calendar

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
        if reflection_line in notes:
            return
        notes = _append_note(notes, reflection_line)
        await _save_debrief_learning(store, local_date, {"notes": notes}, {
            "checklist_id": event.get("checklist_id"), "session_id": session_id,
        })
        return

    prior_completed = log.get("completed") or []
    known_ids = {
        int(item["task_id"])
        for item in prior_completed
        if isinstance(item, dict) and str(item.get("task_id", "")).isdigit()
    }
    completed = list(prior_completed)
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
    notes = str(log.get("notes") or "").strip()
    issued_at = event.get("created_at") or log.get("debrief_sent_at")
    if isinstance(issued_at, str):
        try:
            issued_at = datetime.fromisoformat(issued_at.replace("Z", "+00:00"))
        except ValueError:
            issued_at = None
    if not isinstance(issued_at, datetime) or issued_at.utcoffset() is None:
        # Older callbacks lack creation time; never use a later bound that
        # could resurrect a same-day task the user explicitly reopened.
        issued_at = _day_bounds(local_date)[0]
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
        if task is None or task.get("status") == "dropped":
            continue
        if task.get("reopened_at") and task["reopened_at"] >= issued_at:
            continue
        raw_minutes = value.get("actual_minutes")
        minutes = (
            int(raw_minutes)
            if isinstance(raw_minutes, (int, float)) and not isinstance(raw_minutes, bool)
            and raw_minutes >= 0
            else None if task.get("progress_updated_at") else _scheduled_minutes(task)
        )
        calendar = getattr(getattr(_runtime, "engine", None), "calendar", None)
        try:
            task = await complete_task_with_calendar(
                store, calendar, task_id, minutes,
                actual_minutes_source="debrief" if minutes is not None else None,
                observed_at=issued_at,
            )
        except (KeyError, ValueError):
            latest = await store.get_task(task_id)
            if latest is None or latest.get("status") == "dropped":
                continue
            raise
        if task.get("status") != "completed":
            continue
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
        known_ids.add(task_id)

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
    # The UI acknowledgement and learning retry state are separate: a model
    # outage must never turn a saved completion into a failed checklist.
    if user_reflection:
        reflection_line = f"Debrief response: {user_reflection}"
        if reflection_line not in notes:
            notes = _append_note(notes, reflection_line)
    day_payload: Record = {
        "checklist_id": event.get("checklist_id"),
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
        "session_id": session_id,
    }
    if marker:
        notes = _append_note(notes, marker)
    if followup_kind:
        pending_followup = _followup_marker(
            event.get("checklist_id"), followup_kind, "pending"
        )
        if not _FOLLOWUP_RE.search(notes):
            notes = _append_note(notes, pending_followup)
    await _save_debrief_learning(
        store, local_date,
        {"planned": planned, "completed": completed, "notes": notes or None}, day_payload,
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
        try:
            await _deliver_pending_followup(store, telegram, local_date)
        except Exception:
            LOGGER.exception("debrief_followup_deferred")


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


async def send_weekly_review(store: Store, telegram: Any, local_date: date) -> None:
    """Serialize and send one Sunday review occurrence."""
    async with _occurrence_lock("weekly", local_date):
        await _send_weekly_review_once(store, telegram, local_date)


async def _send_weekly_review_once(
    store: Store, telegram: Any, local_date: date
) -> None:
    """Send a short weekly check-in grounded in recorded outcomes and goals."""
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
        goal_sentence = "This week: " + "; ".join(pieces)
    else:
        goal_sentence = "No goals to check on this week"
    logs = await _week_logs(store, local_date)
    planned_ids = {int(task["task_id"]) for item in logs for task in item.get("planned") or []
        if isinstance(task, dict) and str(task.get("task_id", "")).isdigit()}
    completed_ids = {int(task["task_id"]) for item in logs for task in item.get("completed") or []
        if isinstance(task, dict) and str(task.get("task_id", "")).isdigit()}
    planned, completed = len(planned_ids), len(planned_ids & completed_ids)
    rate = round(100 * completed / planned) if planned else 100
    completion_sentence = (
        f"You checked off {completed} of {planned} planned tasks ({rate}%)"
        if planned else "No task check-ins saved this week"
    )
    unfinished: list[str] = []
    for task_id in sorted(planned_ids - completed_ids):
        task = await store.get_task(task_id)
        if task and task.get("status") not in {"completed", "dropped"}:
            unfinished.append(_short_text(task["title"], 50))
    lines = [_sentence(completion_sentence), _sentence(goal_sentence)]
    if unfinished:
        lines.append("Still open: " + ", ".join(unfinished[:3]) +
            (f" (+{len(unfinished) - 3} more)" if len(unfinished) > 3 else "") + ".")
    message = "\n".join(lines)
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
    if _runtime is not None and getattr(_runtime, "store", None) is not None:
        from .integration import drain_calendar_cleanup
        await drain_calendar_cleanup(_runtime.store, getattr(engine, "calendar", None))
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
                f"{await _format_change(decision, task)}"
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
    hour, minute = _notification_clock("morning")
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


async def _scheduled_reminders() -> None:
    """Deliver due reminders without proactive-message deferral guards."""
    runtime = _runtime_required()
    await deliver_due_reminders(runtime.store, runtime.telegram)


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
    # A brief just after midnight has its planning occurrence the prior day.
    local_date = (timeutil.now_local() + timedelta(minutes=15)).date()
    await run_daily_planning(runtime.engine, local_date)


async def _scheduled_reconcile() -> None:
    await reconcile_calendar(_runtime_required().engine)


async def _scheduled_nightly_facts() -> None:
    """Extract day-scoped evidence even when no debrief was submitted."""
    runtime = _runtime_required()
    local_date = timeutil.now_local().date()
    async with _occurrence_lock("nightly-facts", local_date):
        daily_log = await runtime.store.get_daily_log(local_date)
        if daily_log is None:
            return
        if daily_log.get("debrief_sent_at"):
            # A delivered checklist may still receive outcomes or a reflection;
            # its callback owns the richer debrief extraction.
            return
        notes = str(daily_log.get("notes") or "")
        # A debrief already submitted this same day to the facts engine.  Its
        # checklist marker is durable, so do not feed the identical raw day a
        # second time through the fallback path.
        if re.search(r"\[debrief-checklist:[A-Za-z0-9_-]+\]", notes):
            return
        marker = _nightly_facts_marker(local_date)
        if marker in notes:
            return
        conversation, decisions = await _day_learning_evidence(
            runtime.store, local_date
        )
        await runtime.facts_engine.extract_from_day(
            daily_log=daily_log,
            conversation=conversation,
            decisions=decisions,
        )
        await runtime.store.upsert_daily_log(
            local_date, {"notes": _append_note(notes, marker)}
        )


def _nightly_facts_marker(local_date: date) -> str:
    return f"[nightly-facts:{local_date.isoformat()}]"


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
            "debriefs will save normally and retain pending learning"
        )
    _register_completion_handler(_runtime)

    try:
        from apscheduler.triggers.cron import CronTrigger
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError as exc:
        raise RuntimeError("Install requirements.txt to enable proactive jobs") from exc

    zone = _zone()
    weekly_h, weekly_m = _clock_setting("WEEKLY_REVIEW_TIME", "20:30")
    defaults = _job_defaults()
    _register_daily_clocks(scheduler)
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
    scheduler.add_job(
        _scheduled_reminders,
        IntervalTrigger(seconds=30, timezone=zone),
        id=REMINDER_DISPATCH_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_backup,
        CronTrigger(hour=3, minute=0, timezone=zone),
        id=BACKUP_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_nightly_facts,
        CronTrigger(hour=23, minute=30, timezone=zone),
        id=NIGHTLY_FACTS_JOB_ID,
        **defaults,
    )
    scheduler.add_job(
        _scheduled_debrief_learning,
        IntervalTrigger(minutes=5, timezone=zone),
        id=FACTS_RETRY_JOB_ID,
        **defaults,
    )


def _backup_path(local_date: date) -> Any:
    return config.DATA_DIR / "backups" / f"agenda-{local_date.isoformat()}.db"


def _write_sqlite_backup(source_path: Any, destination_path: Any) -> None:
    """Use SQLite's online backup API so a live database stays consistent."""
    destination_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination_path.name}.", suffix=".tmp", dir=destination_path.parent
    )
    os.close(descriptor)
    try:
        with sqlite3.connect(str(source_path)) as source, sqlite3.connect(
            temporary_name
        ) as destination:
            source.backup(destination)
        os.replace(temporary_name, destination_path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def _prune_agenda_backups(backup_dir: Any, today: date) -> None:
    cutoff = today - timedelta(days=14)
    pattern = re.compile(r"agenda-(\d{4}-\d{2}-\d{2})\.db$")
    for candidate in backup_dir.iterdir():
        matched = pattern.fullmatch(candidate.name)
        if matched is None or not candidate.is_file():
            continue
        try:
            backup_date = date.fromisoformat(matched.group(1))
        except ValueError:
            continue
        if backup_date < cutoff:
            candidate.unlink()


async def _scheduled_backup() -> None:
    """Create the local daily agenda snapshot and prune only dated snapshots."""
    runtime = _runtime_required()
    today = timeutil.now_local().date()
    source_path = runtime.store.db_path
    if str(source_path) == ":memory:":
        LOGGER.warning("Skipping SQLite backup for an in-memory database")
        return
    destination = _backup_path(today)
    try:
        await asyncio.to_thread(_write_sqlite_backup, source_path, destination)
        await asyncio.to_thread(_prune_agenda_backups, destination.parent, today)
    except Exception:
        LOGGER.exception("Daily SQLite backup failed")
        raise


async def run_startup_catchup() -> None:
    """Backfill the latest durable daily and weekly occurrences after restart."""
    runtime = _runtime_required()
    await _scheduled_reminders()
    await _scheduled_debrief_learning()
    if hasattr(runtime.store, "list_pending_calendar_cleanup"):
        from .integration import drain_calendar_cleanup
        await drain_calendar_cleanup(runtime.store, getattr(runtime.engine, "calendar", None))
    now, today = timeutil.now_local(), timeutil.now_local().date()
    await _goal_hook(runtime.engine, "replan_missed_goal_sessions", now)
    morning_h, morning_m = _notification_clock("morning")
    debrief_h, debrief_m = _notification_clock("evening")
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


def start_scheduler(application: Any) -> None:
    """Configure and start this application's scheduler exactly once.

    The application factory attaches the service runtime to ``bot_data`` but
    intentionally does not start background work during import.  This seam is
    shared by polling startup and the ASGI lifespan.
    """
    global _managed_scheduler
    if _managed_scheduler is not None and getattr(_managed_scheduler, "running", False):
        LOGGER.warning("A proactive scheduler is already running; ignoring duplicate start")
        return
    scheduler = _prepare_scheduler(application)
    if getattr(scheduler, "running", False):
        LOGGER.warning("Proactive scheduler is already running; ignoring duplicate start")
        _managed_scheduler = scheduler
        return
    start_job_scheduler(scheduler)
    _managed_scheduler = scheduler


def _prepare_scheduler(application: Any) -> Any:
    """Configure application jobs without starting them (used by ``--check``)."""
    runtime = getattr(application, "bot_data", None)
    if runtime is None:
        raise RuntimeError("Telegram application has no bot_data runtime")
    scheduler = runtime.get("job_scheduler")
    if scheduler is None:
        scheduler = create_job_scheduler()
        telegram = runtime["telegram_handler"]
        configure_jobs(
            scheduler,
            runtime["store"],
            runtime["scheduler_engine"],
            telegram,
            runtime["facts_engine"],
        )
        runtime["job_scheduler"] = scheduler
        telegram.job_scheduler = scheduler
        telegram.scheduler_engine = runtime["scheduler_engine"]
        telegram.facts_engine = runtime["facts_engine"]
    assert_jobs_ready()
    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler managed by :func:`start_scheduler`, if any."""
    global _managed_scheduler
    scheduler = _managed_scheduler
    if scheduler is None:
        return
    try:
        shutdown_job_scheduler(scheduler, wait=True)
    finally:
        _managed_scheduler = None


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
