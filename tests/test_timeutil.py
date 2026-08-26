"""Regression tests for local relative-date resolution."""

from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo

import pytest

import src.timeutil as timeutil
from src.timeutil import day_bounds, resolve_relative, to_local, to_utc

CHICAGO = ZoneInfo("America/Chicago")


def test_spring_dst_relative_day_uses_new_offset() -> None:
    ref = datetime(2026, 3, 7, 9, 0, tzinfo=CHICAGO)
    resolved = resolve_relative("in 1 day", ref)
    assert resolved == datetime(2026, 3, 8, 9, 0, tzinfo=CHICAGO)
    assert resolved.utcoffset().total_seconds() == -5 * 3600


def test_fall_dst_relative_day_uses_new_offset() -> None:
    ref = datetime(2026, 10, 31, 9, 0, tzinfo=CHICAGO)
    resolved = resolve_relative("in 1 day", ref)
    assert resolved == datetime(2026, 11, 1, 9, 0, tzinfo=CHICAGO)
    assert resolved.utcoffset().total_seconds() == -6 * 3600


def test_friday_on_friday_is_today_before_requested_time() -> None:
    ref = datetime(2026, 8, 28, 8, 0, tzinfo=CHICAGO)
    assert resolve_relative("friday morning", ref) == datetime(
        2026, 8, 28, 9, 0, tzinfo=CHICAGO
    )


def test_friday_on_friday_is_next_week_after_requested_time() -> None:
    ref = datetime(2026, 8, 28, 10, 0, tzinfo=CHICAGO)
    assert resolve_relative("friday morning", ref) == datetime(
        2026, 9, 4, 9, 0, tzinfo=CHICAGO
    )


def test_bare_friday_on_friday_means_today_at_reference_time() -> None:
    ref = datetime(2026, 8, 28, 16, 45, tzinfo=CHICAGO)
    assert resolve_relative("friday", ref) == ref


def test_bare_friday_before_friday_resolves_upcoming_friday() -> None:
    ref = datetime(2026, 8, 27, 16, 45, tzinfo=CHICAGO)
    assert resolve_relative("friday", ref).date() == date(2026, 8, 28)


def test_midnight_rollover() -> None:
    ref = datetime(2026, 8, 26, 23, 59, tzinfo=CHICAGO)
    assert resolve_relative("tomorrow at 12:30 am", ref) == datetime(
        2026, 8, 27, 0, 30, tzinfo=CHICAGO
    )


def test_tonight_at_11pm_stays_on_same_date() -> None:
    ref = datetime(2026, 8, 26, 23, 0, tzinfo=CHICAGO)
    resolved = resolve_relative("tonight at 11pm", ref)
    assert resolved.date() == ref.date()
    assert resolved.hour == 23


def test_day_bounds_are_half_open_and_dst_aware() -> None:
    start, end = day_bounds(date(2026, 3, 8))
    assert start == datetime(2026, 3, 8, 6, tzinfo=UTC)
    assert end == datetime(2026, 3, 9, 5, tzinfo=UTC)
    assert (end - start).total_seconds() == 23 * 3600


def test_conversions_reject_naive_datetimes() -> None:
    with pytest.raises(ValueError, match="naive"):
        to_utc(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="naive"):
        to_local(datetime(2026, 1, 1))


def test_time_context_spells_out_calculated_weekdays(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        timeutil, "now_local", lambda: datetime(2026, 8, 26, 15, 47, tzinfo=CHICAGO)
    )
    assert timeutil.format_time_context() == (
        "Right now it is 3:47 PM on Wednesday, August 26, 2026 "
        "(America/Chicago, CDT).\n"
        "Today is Wednesday Aug 26. Tomorrow is Thursday Aug 27.\n"
        "This week: Mon Aug 24 – Sun Aug 30. Next week starts Mon Aug 31."
    )
