"""Utility functions for date/time handling and formatting."""

from datetime import datetime, timedelta
from typing import Any
import pytz

from src.config import config


def get_current_time(timezone: str | None = None) -> datetime:
    """Get current time in the specified timezone.

    Args:
        timezone: Timezone string (e.g., 'America/Chicago'). Uses config default if None.

    Returns:
        Current datetime in the specified timezone.
    """
    tz = pytz.timezone(timezone or config.USER_TIMEZONE)
    return datetime.now(tz)


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
    return dt.isoformat()


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
        # Ensure timezone awareness
        if dt.tzinfo is None:
            tz = pytz.timezone(config.USER_TIMEZONE)
            dt = tz.localize(dt)
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
    if date is None:
        date = get_current_time()

    tz = pytz.timezone(config.USER_TIMEZONE)
    start = datetime(date.year, date.month, date.day, 0, 0, 0)
    end = datetime(date.year, date.month, date.day, 23, 59, 59)

    if start.tzinfo is None:
        start = tz.localize(start)
        end = tz.localize(end)

    return start, end


def get_week_range(date: datetime | None = None) -> tuple[datetime, datetime]:
    """Get start and end of a week (Monday to Sunday).

    Args:
        date: Date within the week. Uses current date if None.

    Returns:
        Tuple of (start_of_week, end_of_week) datetimes.
    """
    if date is None:
        date = get_current_time()

    tz = pytz.timezone(config.USER_TIMEZONE)
    # Get Monday of the week
    days_since_monday = date.weekday()
    monday = date - timedelta(days=days_since_monday)
    sunday = monday + timedelta(days=6)

    start = datetime(monday.year, monday.month, monday.day, 0, 0, 0)
    end = datetime(sunday.year, sunday.month, sunday.day, 23, 59, 59)

    if start.tzinfo is None:
        start = tz.localize(start)
        end = tz.localize(end)

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
