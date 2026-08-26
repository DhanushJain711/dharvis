"""Timezone-safe date handling used by prompts, tools, and persistence."""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import dateparser

from .config import config

_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def _local_zone() -> ZoneInfo:
    try:
        return ZoneInfo(config.USER_TIMEZONE)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown USER_TIMEZONE: {config.USER_TIMEZONE!r}") from exc


def _require_aware(value: datetime, name: str = "datetime") -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are not allowed")


def now_local() -> datetime:
    """Return the current aware datetime in ``USER_TIMEZONE``."""
    return datetime.now(_local_zone())


def now_utc() -> datetime:
    """Return the current aware datetime in UTC."""
    return datetime.now(UTC)


def to_utc(value: datetime) -> datetime:
    """Convert an aware datetime to UTC, rejecting naive inputs."""
    _require_aware(value)
    return value.astimezone(UTC)


def to_local(value: datetime) -> datetime:
    """Convert an aware datetime to ``USER_TIMEZONE``, rejecting naive inputs."""
    _require_aware(value)
    return value.astimezone(_local_zone())


def _clock(value: datetime) -> str:
    rendered = value.strftime("%I:%M %p")
    return rendered[1:] if rendered.startswith("0") else rendered


def _short_day(value: date) -> str:
    return f"{value.strftime('%a %b')} {value.day}"


def _month_day(value: date) -> str:
    return f"{value.strftime('%b')} {value.day}"


def format_time_context() -> str:
    """Build the explicit local date context injected into every system prompt."""
    current = now_local()
    today = current.date()
    tomorrow = today + timedelta(days=1)
    monday = today - timedelta(days=today.weekday())
    sunday = monday + timedelta(days=6)
    next_monday = monday + timedelta(days=7)
    zone_name = getattr(current.tzinfo, "key", config.USER_TIMEZONE)
    abbreviation = current.tzname() or zone_name
    return "\n".join(
        (
            f"Right now it is {_clock(current)} on {current.strftime('%A, %B')} "
            f"{current.day}, {current.year} ({zone_name}, {abbreviation}).",
            f"Today is {today.strftime('%A')} {_month_day(today)}. "
            f"Tomorrow is {tomorrow.strftime('%A')} {_month_day(tomorrow)}.",
            f"This week: {_short_day(monday)} – {_short_day(sunday)}. "
            f"Next week starts {next_monday.strftime('%a')} "
            f"{next_monday.strftime('%b')} {next_monday.day}.",
        )
    )


def _normalize_wall_time(parsed: datetime, zone: ZoneInfo) -> datetime:
    """Attach ZoneInfo rules to parser output and normalize nonexistent DST times."""
    wall = datetime(
        parsed.year, parsed.month, parsed.day, parsed.hour, parsed.minute,
        parsed.second, parsed.microsecond, tzinfo=zone,
    )
    round_trip = wall.astimezone(UTC).astimezone(zone)
    if round_trip.replace(tzinfo=None) != wall.replace(tzinfo=None):
        return round_trip
    return wall


def resolve_relative(phrase: str, ref: datetime | None = None) -> datetime:
    """Resolve a natural-language date phrase to an aware local datetime.

    Parsing is delegated to :mod:`dateparser`. A weekday named on that same
    weekday means today when its parsed clock time has not passed; otherwise it
    means the following week. A bare weekday has no passed clock time, so on
    that weekday it resolves to the reference time today. An explicit ``next``
    always selects a future occurrence. ``tonight`` stays on the reference
    calendar day, including at 11 PM; without a clock it means 8 PM or the
    current time if later.
    """
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("phrase must be a non-empty string")
    reference = ref or now_local()
    _require_aware(reference, "ref")
    zone = _local_zone()
    local_ref = reference.astimezone(zone)
    original = phrase.strip().lower()
    text = original
    bare_weekday = bool(
        re.fullmatch(r"(?:next\s+)?(?:" + "|".join(_WEEKDAYS) + r")", original)
    )

    explicit_next = bool(re.search(r"\bnext\s+(?:mon|tues|wednes|thurs|fri|satur|sun)day\b", text))
    if explicit_next:
        text = re.sub(r"\bnext\s+", "", text, count=1)

    text = re.sub(r"\bmorning\b", "at 9:00 AM", text)
    text = re.sub(r"\bafternoon\b", "at 3:00 PM", text)
    text = re.sub(r"\bevening\b", "at 7:00 PM", text)
    if "tonight" in text:
        if re.search(r"\btonight\s+at\b", text):
            text = re.sub(r"\btonight\b", "today", text)
        else:
            hour = max(20, local_ref.hour)
            minute = local_ref.minute if local_ref.hour >= 20 else 0
            text = re.sub(
                r"\btonight\b", f"today at {hour:02d}:{minute:02d}", text
            )

    parsed = dateparser.parse(
        text,
        settings={
            "RELATIVE_BASE": local_ref,
            "RETURN_AS_TIMEZONE_AWARE": True,
            "TIMEZONE": zone.key,
            "TO_TIMEZONE": zone.key,
            "PREFER_DATES_FROM": "future",
            "PREFER_DAY_OF_MONTH": "current",
        },
        languages=["en"],
    )
    if parsed is None:
        raise ValueError(f"Could not resolve relative date phrase: {phrase!r}")
    result = _normalize_wall_time(parsed, zone)

    weekday_match = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", original)
    if weekday_match:
        target_weekday = _WEEKDAYS[weekday_match.group(1)]
        if explicit_next:
            days = (target_weekday - local_ref.weekday()) % 7 or 7
            target_date = local_ref.date() + timedelta(days=days)
            result = _normalize_wall_time(
                datetime.combine(target_date, result.timetz().replace(tzinfo=None)), zone
            )
        elif target_weekday == local_ref.weekday():
            if bare_weekday:
                return local_ref
            today_result = _normalize_wall_time(
                datetime.combine(local_ref.date(), result.timetz().replace(tzinfo=None)), zone
            )
            result = (
                today_result
                if today_result >= local_ref
                else _normalize_wall_time(today_result + timedelta(days=7), zone)
            )

    return result


def day_bounds(value: date | datetime) -> tuple[datetime, datetime]:
    """Return the UTC half-open bounds ``[start, next_start)`` of a local day."""
    zone = _local_zone()
    if isinstance(value, datetime):
        _require_aware(value)
        local_date = value.astimezone(zone).date()
    elif isinstance(value, date):
        local_date = value
    else:
        raise TypeError("value must be a date or timezone-aware datetime")
    local_start = datetime.combine(local_date, time.min, tzinfo=zone)
    local_next = datetime.combine(local_date + timedelta(days=1), time.min, tzinfo=zone)
    return local_start.astimezone(UTC), local_next.astimezone(UTC)
