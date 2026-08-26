"""Utility functions for date/time handling and formatting."""

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from .config import config
from .timeutil import day_bounds, now_local, to_utc


def get_current_time(timezone: str | None = None) -> datetime:
    """Get current time in the specified timezone.

    Args:
        timezone: Timezone string (e.g., 'America/Chicago'). Uses config default if None.

    Returns:
        Current datetime in the specified timezone.
    """
    return datetime.now(ZoneInfo(timezone)) if timezone else now_local()


def format_datetime_for_display(dt: datetime | str | None) -> str:
    """Format datetime for user-friendly display.

    Args:
        dt: Datetime object or ISO string.

    Returns:
        Formatted string like "Thu Jan 18 at 2pm" or "Thu Jan 18 at 2:30pm".
    """
    if dt is None:
        return ""
    if isinstance(dt, str):
        dt = parse_iso_datetime(dt)
    if dt is None:
        return ""

    # Format: "Thu Jan 18 at 2pm" or "Thu Jan 18 at 2:30pm"
    day_str = dt.strftime("%a %b %-d")
    hour = dt.hour
    minute = dt.minute
    am_pm = "am" if hour < 12 else "pm"
    hour_12 = hour % 12 or 12

    if minute == 0:
        time_str = f"{hour_12}{am_pm}"
    else:
        time_str = f"{hour_12}:{minute:02d}{am_pm}"

    return f"{day_str} at {time_str}"


def format_datetime_iso(dt: datetime) -> str:
    """Format datetime as ISO 8601 string.

    Args:
        dt: Datetime object.

    Returns:
        ISO format string.
    """
    return to_utc(dt).isoformat()


def parse_iso_datetime(iso_str: str) -> datetime | None:
    """Parse ISO datetime string to datetime object.

    Args:
        iso_str: ISO format datetime string.

    Returns:
        Datetime object or None if parsing fails.
    """
    if not iso_str:
        return None
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt
    except (ValueError, TypeError):
        return None


def get_day_range(date: datetime | None = None) -> tuple[datetime, datetime]:
    """Get start and end of a day.

    Args:
        date: Date to get range for. Uses current date if None.

    Returns:
        Tuple of (start_of_day, end_of_day) datetimes.
    """
    selected = date or get_current_time()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("date must be timezone-aware; naive datetimes are not allowed")
    return day_bounds(selected)


def get_week_range(date: datetime | None = None) -> tuple[datetime, datetime]:
    """Get start and end of a week (Monday to Sunday).

    Args:
        date: Date within the week. Uses current date if None.

    Returns:
        Tuple of (start_of_week, end_of_week) datetimes.
    """
    selected = date or get_current_time()
    if selected.tzinfo is None or selected.utcoffset() is None:
        raise ValueError("date must be timezone-aware; naive datetimes are not allowed")
    local = selected.astimezone(ZoneInfo(config.USER_TIMEZONE))
    monday = local.date() - timedelta(days=local.weekday())
    next_monday = monday + timedelta(days=7)
    start, _ = day_bounds(monday)
    end, _ = day_bounds(next_monday)
    return start, end


def format_task_for_prompt(task: dict[str, Any]) -> str:
    """Format a task dict for inclusion in Claude prompt.

    Args:
        task: Task dictionary with keys: id, title, deadline, priority, status.

    Returns:
        Formatted string representation.
    """
    task_id = task.get("id", "?")
    title = task.get("title", "Untitled")
    deadline = task.get("deadline")
    priority = task.get("priority", "medium")
    status = task.get("status", "pending")

    deadline_str = ""
    if deadline:
        dt = parse_iso_datetime(deadline) if isinstance(deadline, str) else deadline
        if dt:
            deadline_str = f" (due {format_datetime_for_display(dt)})"

    priority_marker = {"high": "!!!", "medium": "", "low": "(low)"}.get(priority, "")

    return f"[{task_id}] {priority_marker}{title}{deadline_str} - {status}"


def format_event_for_prompt(event: dict[str, Any]) -> str:
    """Format an event dict for inclusion in Claude prompt.

    Args:
        event: Event dictionary with keys: id, title, start_time, end_time, location.

    Returns:
        Formatted string representation.
    """
    event_id = event.get("id", "?")
    title = event.get("title", "Untitled")
    start_time = event.get("start_time")
    end_time = event.get("end_time")
    location = event.get("location")
    source = event.get("source", "bot")

    time_str = ""
    if start_time:
        start_dt = (
            parse_iso_datetime(start_time)
            if isinstance(start_time, str)
            else start_time
        )
        if start_dt:
            time_str = format_datetime_for_display(start_dt)
            if end_time:
                end_dt = (
                    parse_iso_datetime(end_time)
                    if isinstance(end_time, str)
                    else end_time
                )
                if end_dt:
                    end_hour = end_dt.hour % 12 or 12
                    end_minute = end_dt.minute
                    end_am_pm = "am" if end_dt.hour < 12 else "pm"
                    if end_minute == 0:
                        time_str += f" - {end_hour}{end_am_pm}"
                    else:
                        time_str += f" - {end_hour}:{end_minute:02d}{end_am_pm}"

    location_str = f" at {location}" if location else ""
    source_str = f" [gcal]" if source == "gcal" else ""

    return f"[{event_id}] {title} - {time_str}{location_str}{source_str}"


def format_tasks_list(tasks: list[dict[str, Any]]) -> str:
    """Format a list of tasks for Claude prompt.

    Args:
        tasks: List of task dictionaries.

    Returns:
        Formatted multi-line string.
    """
    if not tasks:
        return "No pending tasks."

    lines = [format_task_for_prompt(task) for task in tasks]
    return "\n".join(lines)


def format_events_list(events: list[dict[str, Any]]) -> str:
    """Format a list of events for Claude prompt.

    Args:
        events: List of event dictionaries.

    Returns:
        Formatted multi-line string.
    """
    if not events:
        return "No scheduled events."

    lines = [format_event_for_prompt(event) for event in events]
    return "\n".join(lines)
