"""Behavioral tests for the read/write Google Calendar boundary."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from google.auth.exceptions import RefreshError
from googleapiclient.errors import HttpError

from src.calendar_service import (
    CALENDAR_OWNERSHIP_MARKER,
    CACHE_TTL_SECONDS,
    CATEGORY_COLOR_IDS,
    CalendarError,
    CalendarReconnectRequiredError,
    CalendarService,
    FIXED_EVENT_KIND,
    GOAL_SESSION_KIND,
    KIND_MARKER_KEY,
    OWNERSHIP_MARKER_KEY,
    OWNERSHIP_MARKER_VALUE,
    SCOPES,
    TASK_BLOCK_KIND,
    WORK_BLOCK_MARKER_KEY,
    WORK_BLOCK_MARKER_VALUE,
)


class Request:
    """Small executable request used by the fake Google resources."""

    def __init__(self, value=None, error: Exception | None = None):
        self.value = value
        self.error = error

    def execute(self):
        if self.error is not None:
            raise self.error
        return self.value


def google_event(
    event_id: str,
    title: str,
    start: str = "2026-08-26T15:00:00+00:00",
    end: str = "2026-08-26T16:00:00+00:00",
    *,
    marked: bool = False,
    description: str | None = None,
) -> dict:
    event = {
        "id": event_id,
        "summary": title,
        "start": {"dateTime": start},
        "end": {"dateTime": end},
    }
    if description is not None:
        event["description"] = description
    if marked:
        event["extendedProperties"] = {
            "private": {WORK_BLOCK_MARKER_KEY: WORK_BLOCK_MARKER_VALUE}
        }
    return event


def connected_service(tmp_path: Path, google: MagicMock) -> CalendarService:
    service = CalendarService(token_path=tmp_path / "token.json")
    credentials = MagicMock()
    credentials.expired = False
    credentials.valid = True
    service._credentials = credentials
    service._service = google
    service._kalendra_calendar_id = None
    return service


def calendar_list(google: MagicMock, pages: dict[str | None, dict]) -> MagicMock:
    resource = MagicMock()
    resource.list.side_effect = lambda pageToken=None: Request(pages[pageToken])
    google.calendarList.return_value = resource
    return resource


def event_resource(google: MagicMock) -> MagicMock:
    resource = MagicMock()
    google.events.return_value = resource
    return resource


def owned_calendar(calendar_id: str = "kalendra-id") -> dict:
    return {
        "id": calendar_id,
        "summary": "Kalendra",
        "description": f"Bot work blocks\n{CALENDAR_OWNERSHIP_MARKER}",
        "primary": False,
    }


RANGE_START = datetime(2026, 8, 26, tzinfo=UTC)
RANGE_END = datetime(2026, 8, 27, tzinfo=UTC)


def test_calendar_uses_read_write_scope():
    assert SCOPES == ["https://www.googleapis.com/auth/calendar"]


def test_fixed_event_kind_legacy_spelling_is_persisted_canonically():
    service = CalendarService()

    body = service._event_body(
        {
            "title": "Appointment",
            "start_time": RANGE_START,
            "end_time": RANGE_END,
        },
        "the user requested this fixed-time event",
        kind="fixed_event",
    )

    assert body["extendedProperties"]["private"][KIND_MARKER_KEY] == FIXED_EVENT_KIND


def test_is_available_without_token(tmp_path: Path):
    service = CalendarService(token_path=tmp_path / "missing.json")

    assert service.is_available() is False


@patch("src.calendar_service.build")
@patch("src.calendar_service.Credentials")
def test_is_available_builds_with_valid_credentials(
    credentials_class: MagicMock, build: MagicMock, tmp_path: Path
):
    token = tmp_path / "token.json"
    token.write_text("{}", encoding="utf-8")
    credentials = MagicMock(expired=False, valid=True)
    credentials_class.from_authorized_user_file.return_value = credentials

    assert CalendarService(token_path=token).is_available() is True
    credentials_class.from_authorized_user_file.assert_called_once_with(str(token), SCOPES)
    build.assert_called_once_with(
        "calendar", "v3", credentials=credentials, cache_discovery=False
    )


def test_format_event_normalizes_offsets_and_all_day_dst_boundaries():
    service = CalendarService()

    timed = service._format_event(
        google_event(
            "meeting",
            "Meeting",
            "2026-08-26T10:00:00-05:00",
            "2026-08-26T11:00:00-05:00",
        )
    )
    spring = service._format_event(
        {
            "id": "spring",
            "summary": "Spring day",
            "start": {"date": "2026-03-08"},
            "end": {"date": "2026-03-09"},
        }
    )
    fall = service._format_event(
        {
            "id": "fall",
            "summary": "Fall day",
            "start": {"date": "2026-11-01"},
            "end": {"date": "2026-11-02"},
        }
    )

    assert timed["start_time"] == "2026-08-26T15:00:00+00:00"
    assert timed["end_time"] == "2026-08-26T16:00:00+00:00"
    assert spring["start_time"] == "2026-03-08T06:00:00+00:00"
    assert spring["end_time"] == "2026-03-09T05:00:00+00:00"
    assert fall["start_time"] == "2026-11-01T05:00:00+00:00"
    assert fall["end_time"] == "2026-11-02T06:00:00+00:00"


@pytest.mark.asyncio
async def test_list_events_reads_every_calendar_and_paginates_every_resource(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_api = calendar_list(
        google,
        {
            None: {"items": [{"id": "primary"}], "nextPageToken": "cal-2"},
            "cal-2": {"items": [{"id": "school"}]},
        },
    )
    events = event_resource(google)
    pages = {
        ("primary", None): {
            "items": [google_event("late", "Late", "2026-08-26T18:00:00Z", "2026-08-26T19:00:00Z")],
            "nextPageToken": "event-2",
        },
        ("primary", "event-2"): {
            "items": [google_event("early", "Early", "2026-08-26T13:00:00Z", "2026-08-26T14:00:00Z")]
        },
        ("school", None): {
            "items": [google_event("class", "Class", "2026-08-26T15:00:00Z", "2026-08-26T16:00:00Z")]
        },
    }
    events.list.side_effect = lambda **kwargs: Request(
        pages[(kwargs["calendarId"], kwargs["pageToken"])]
    )
    service = connected_service(tmp_path, google)

    result = await service.list_events(RANGE_START, RANGE_END)

    assert [(item["id"], item["calendar_id"]) for item in result] == [
        ("early", "primary"),
        ("class", "school"),
        ("late", "primary"),
    ]
    assert [call.kwargs["pageToken"] for call in calendar_api.list.call_args_list] == [
        None,
        "cal-2",
    ]
    assert len(events.list.call_args_list) == 3
    assert all(call.kwargs["singleEvents"] is True for call in events.list.call_args_list)


@pytest.mark.asyncio
async def test_list_events_preserves_event_and_source_calendar_metadata(tmp_path: Path):
    google = MagicMock()
    calendar_list(
        google,
        {
            None: {
                "items": [
                    {
                        "id": "school",
                        "summary": "School",
                        "accessRole": "reader",
                        "colorId": "9",
                        "backgroundColor": "#7bd148",
                        "foregroundColor": "#ffffff",
                    }
                ]
            }
        },
    )
    events = event_resource(google)
    event = google_event("class", "Class")
    event["colorId"] = "5"
    events.list.return_value = Request({"items": [event]})
    service = connected_service(tmp_path, google)

    result = await service.list_events(RANGE_START, RANGE_END)

    assert result == [
        {
            "id": "class",
            "title": "Class",
            "description": None,
            "start_time": "2026-08-26T15:00:00+00:00",
            "end_time": "2026-08-26T16:00:00+00:00",
            "location": None,
            "source": "gcal",
            "gcal_event_id": "class",
            "color_id": "5",
            "extended_properties": {},
            "kalendra_owned": False,
            "kalendra_kind": None,
            "category": None,
            "calendar_id": "school",
            "calendar_summary": "School",
            "calendar_primary": False,
            "calendar_access_role": "reader",
            "calendar_color_id": "9",
            "calendar_background_color": "#7bd148",
            "calendar_foreground_color": "#ffffff",
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("color_id", [None, "6"])
async def test_get_owned_event_uses_exact_kalendra_lookup_and_preserves_color(
    tmp_path: Path, color_id: str | None
):
    google = MagicMock()
    events = event_resource(google)
    event = google_event("shared-id", "CS311 discussion")
    event["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    if color_id is not None:
        event["colorId"] = color_id
    events.get.return_value = Request(event)
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = "kalendra-id"
    service._kalendra_id_validated = True

    result = await service.get_owned_event("shared-id")

    assert events.get.call_args.kwargs == {
        "calendarId": "kalendra-id",
        "eventId": "shared-id",
    }
    events.list.assert_not_called()
    google.calendarList.return_value.list.assert_not_called()
    assert result["calendar_id"] == "kalendra-id"
    assert result["gcal_event_id"] == "shared-id"
    assert result["kalendra_owned"] is True
    assert result["color_id"] == color_id


@pytest.mark.asyncio
async def test_get_owned_event_requires_event_id(
    tmp_path: Path,
):
    google = MagicMock()
    events = event_resource(google)
    service = connected_service(tmp_path, google)

    with pytest.raises(ValueError, match="gcal_event_id"):
        await service.get_owned_event("  ")

    events.get.assert_not_called()


@pytest.mark.asyncio
async def test_get_owned_event_discovers_marker_owned_calendar_without_cached_id(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(
        google,
        {
            None: {
                "items": [
                    {"id": "primary", "primary": True},
                    owned_calendar("found-kalendra"),
                ]
            }
        },
    )
    events = event_resource(google)
    event = google_event("event-id", "Dinner")
    event["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(event)
    service = connected_service(tmp_path, google)

    result = await service.get_owned_event("event-id")

    assert result["calendar_id"] == "found-kalendra"
    assert events.get.call_args.kwargs["calendarId"] == "found-kalendra"
    google.calendars.return_value.insert.assert_not_called()
    events.list.assert_not_called()


@pytest.mark.asyncio
async def test_get_owned_event_recovers_stale_persisted_calendar_id(tmp_path: Path):
    google = MagicMock()
    calendar_list(
        google,
        {
            None: {
                "items": [
                    {"id": "stale", "summary": "Kalendra", "primary": False},
                    owned_calendar("recovered-kalendra"),
                ]
            }
        },
    )
    events = event_resource(google)
    event = google_event("event-id", "Dinner")
    event["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(event)
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = "stale"

    result = await service.get_owned_event("event-id")

    assert result["calendar_id"] == "recovered-kalendra"
    assert events.get.call_args.kwargs["calendarId"] == "recovered-kalendra"
    assert service.kalendra_id_path.read_text(encoding="utf-8").strip() == (
        "recovered-kalendra"
    )
    events.list.assert_not_called()


@pytest.mark.asyncio
async def test_get_owned_event_never_uses_primary_or_name_only_calendar(tmp_path: Path):
    google = MagicMock()
    calendar_list(
        google,
        {
            None: {
                "items": [
                    {
                        "id": "primary",
                        "summary": "Kalendra",
                        "primary": True,
                        "description": CALENDAR_OWNERSHIP_MARKER,
                    },
                    {"id": "name-only", "summary": "Kalendra", "primary": False},
                ]
            }
        },
    )
    calendars = MagicMock()
    calendars.insert.return_value = Request(
        {"id": "new-owned-kalendra", "primary": False}
    )
    google.calendars.return_value = calendars
    events = event_resource(google)
    event = google_event("event-id", "Dinner")
    event["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(event)
    service = connected_service(tmp_path, google)

    result = await service.get_owned_event("event-id")

    assert result["calendar_id"] == "new-owned-kalendra"
    assert events.get.call_args.kwargs["calendarId"] == "new-owned-kalendra"
    events.list.assert_not_called()


@pytest.mark.asyncio
async def test_get_owned_event_surfaces_not_found_plainly(tmp_path: Path):
    google = MagicMock()
    events = event_resource(google)
    events.get.return_value = Request(
        error=HttpError(
            resp=SimpleNamespace(status=404, reason="not found"), content=b"missing"
        )
    )
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = "kalendra-id"
    service._kalendra_id_validated = True

    with pytest.raises(CalendarError, match="event was not found"):
        await service.get_owned_event("missing")


@pytest.mark.asyncio
async def test_get_owned_event_fails_closed_on_google_error(tmp_path: Path):
    google = MagicMock()
    events = event_resource(google)
    events.get.return_value = Request(
        error=HttpError(
            resp=SimpleNamespace(status=500, reason="server error"), content=b"failed"
        )
    )
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = "kalendra-id"
    service._kalendra_id_validated = True

    with pytest.raises(CalendarError, match="Could not retrieve"):
        await service.get_owned_event("event-id")


@pytest.mark.asyncio
async def test_get_owned_event_rejects_same_id_without_owned_marker(tmp_path: Path):
    google = MagicMock()
    events = event_resource(google)
    # Event ids are interpreted only inside the exact Kalendra calendar.  A
    # same-named event from another calendar is never discovered or accepted.
    events.get.return_value = Request(google_event("collision", "External copy"))
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = "kalendra-id"
    service._kalendra_id_validated = True

    with pytest.raises(CalendarError, match="not owned by Kalendra"):
        await service.get_owned_event("collision")

    assert events.get.call_args.kwargs["calendarId"] == "kalendra-id"
    events.list.assert_not_called()
    google.calendarList.return_value.list.assert_not_called()


@pytest.mark.asyncio
async def test_list_events_cache_is_60_seconds_and_returns_defensive_copies(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request({"items": [google_event("one", "Original")]})
    service = connected_service(tmp_path, google)

    with patch(
        "src.calendar_service.monotonic",
        side_effect=[100.0, 159.9, 160.1, 160.1],
    ):
        first = await service.list_events(RANGE_START, RANGE_END)
        first[0]["title"] = "mutated by caller"
        second = await service.list_events(RANGE_START, RANGE_END)
        third = await service.list_events(RANGE_START, RANGE_END)

    assert second[0]["title"] == "Original"
    assert third[0]["title"] == "Original"
    assert events.list.call_count == 2
    assert CACHE_TTL_SECONDS == 60.0


@pytest.mark.asyncio
async def test_list_events_force_refresh_bypasses_cached_google_result(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.side_effect = [
        Request({"items": [google_event("first", "First")]}),
        Request(
            {
                "items": [
                    google_event("first", "First"),
                    google_event("new", "Added upstream"),
                ]
            }
        ),
    ]
    service = connected_service(tmp_path, google)

    first = await service.list_events(RANGE_START, RANGE_END)
    refreshed = await service.list_events(
        RANGE_START, RANGE_END, force_refresh=True
    )

    assert [event["id"] for event in first] == ["first"]
    assert [event["id"] for event in refreshed] == ["first", "new"]
    assert events.list.call_count == 2


@pytest.mark.asyncio
async def test_zero_duration_event_is_skipped_without_poisoning_complete_cached_read(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        {
            "items": [
                google_event("valid", "Valid"),
                google_event(
                    "empty",
                    "Empty",
                    "2026-08-26T17:00:00Z",
                    "2026-08-26T17:00:00Z",
                ),
            ]
        }
    )
    service = connected_service(tmp_path, google)

    first = await service.list_events(RANGE_START, RANGE_END)
    second = await service.list_events(RANGE_START, RANGE_END)

    assert [item["id"] for item in first] == ["valid"]
    assert [item["id"] for item in second] == ["valid"]
    assert service._last_query_complete is True
    assert events.list.call_count == 1


@pytest.mark.asyncio
async def test_only_zero_duration_events_leave_range_complete_and_available(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        {
            "items": [
                google_event(
                    "empty",
                    "Empty",
                    "2026-08-26T17:00:00Z",
                    "2026-08-26T17:00:00Z",
                )
            ]
        }
    )
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is True
    assert await service.check_availability(RANGE_START, RANGE_END) is True
    assert events.list.call_count == 1


@pytest.mark.asyncio
async def test_equal_instants_with_different_offsets_are_zero_duration(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        {
            "items": [
                google_event(
                    "offset-empty",
                    "Offset empty",
                    "2026-08-26T10:00:00-05:00",
                    "2026-08-26T11:00:00-04:00",
                )
            ]
        }
    )
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is True


@pytest.mark.asyncio
async def test_equal_all_day_dates_are_zero_duration(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        {
            "items": [
                {
                    "id": "all-day-empty",
                    "summary": "All-day empty",
                    "start": {"date": "2026-08-26"},
                    "end": {"date": "2026-08-26"},
                }
            ]
        }
    )
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is True


@pytest.mark.asyncio
async def test_reversed_event_range_remains_incomplete_and_uncached(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.side_effect = [
        Request(
            {
                "items": [
                    google_event(
                        "reversed",
                        "Reversed",
                        "2026-08-26T17:00:00Z",
                        "2026-08-26T16:00:00Z",
                    )
                ]
            }
        ),
        Request({"items": []}),
    ]
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is False
    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is True
    assert events.list.call_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_event",
    [
        {
            "id": "missing-end",
            "summary": "Missing end",
            "start": {"dateTime": "2026-08-26T15:00:00Z"},
        },
        {
            "id": "null-end",
            "summary": "Null end",
            "start": {"dateTime": "2026-08-26T15:00:00Z"},
            "end": None,
        },
        {
            "id": "non-mapping-end",
            "summary": "Non-mapping end",
            "start": {"dateTime": "2026-08-26T15:00:00Z"},
            "end": "2026-08-26T16:00:00Z",
        },
        {
            "id": "null-start",
            "summary": "Null start",
            "start": None,
            "end": {"dateTime": "2026-08-26T16:00:00Z"},
        },
        {
            "id": "non-mapping-start",
            "summary": "Non-mapping start",
            "start": "2026-08-26T15:00:00Z",
            "end": {"dateTime": "2026-08-26T16:00:00Z"},
        },
    ],
    ids=[
        "omitted-end",
        "null-end",
        "non-mapping-end",
        "null-start",
        "non-mapping-start",
    ],
)
async def test_invalid_event_endpoint_is_incomplete_and_uncached(
    tmp_path: Path, invalid_event: dict,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.side_effect = [
        Request({"items": [invalid_event]}),
        Request({"items": []}),
    ]
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is False
    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is True
    assert events.list.call_count == 2


@pytest.mark.asyncio
async def test_partial_reads_are_not_cached(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.side_effect = [
        Request(
            {
                "items": [
                    google_event("valid", "Valid"),
                    {"id": "broken", "summary": "Broken", "start": {}},
                ]
            }
        ),
        Request({"items": []}),
    ]
    service = connected_service(tmp_path, google)

    first = await service.list_events(RANGE_START, RANGE_END)
    second = await service.list_events(RANGE_START, RANGE_END)

    assert [item["id"] for item in first] == ["valid"]
    assert second == []
    assert events.list.call_count == 2


@pytest.mark.asyncio
async def test_server_error_marks_read_incomplete_and_availability_false(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        error=HttpError(resp=SimpleNamespace(status=500, reason="error"), content=b"error")
    )
    service = connected_service(tmp_path, google)

    assert await service.list_events(RANGE_START, RANGE_END) == []
    assert service._last_query_complete is False
    assert await service.check_availability(RANGE_START, RANGE_END) is False


def test_expired_token_refreshes_transparently_and_is_persisted_private(tmp_path: Path):
    token = tmp_path / "token.json"
    token.write_text("old", encoding="utf-8")
    service = CalendarService(token_path=token)
    credentials = MagicMock()
    credentials.expired = True
    credentials.refresh_token = "refresh-token"
    credentials.valid = True
    credentials.to_json.return_value = '{"token":"new"}'
    service._credentials = credentials

    assert service._get_credentials() is credentials
    credentials.refresh.assert_called_once()
    assert token.read_text(encoding="utf-8") == '{"token":"new"}'
    assert token.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("failure", [RefreshError("bad refresh"), OSError("offline")])
def test_refresh_failure_raises_typed_reconnect_error(tmp_path: Path, failure: Exception):
    token = tmp_path / "token.json"
    token.write_text("old", encoding="utf-8")
    service = CalendarService(token_path=token)
    credentials = MagicMock()
    credentials.expired = True
    credentials.refresh_token = "refresh-token"
    credentials.valid = False
    credentials.refresh.side_effect = failure
    service._credentials = credentials

    with pytest.raises(CalendarReconnectRequiredError, match="reconnect"):
        service._get_credentials()


@pytest.mark.asyncio
async def test_http_auth_rejection_raises_typed_reconnect_error(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [{"id": "primary"}]}})
    events = event_resource(google)
    events.list.return_value = Request(
        error=HttpError(resp=SimpleNamespace(status=401, reason="unauthorized"), content=b"")
    )
    service = connected_service(tmp_path, google)

    with pytest.raises(CalendarReconnectRequiredError, match="reconnect"):
        await service.list_events(RANGE_START, RANGE_END)


@pytest.mark.asyncio
async def test_first_create_makes_secondary_calendar_persists_id_and_never_writes_primary(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(
        google,
        {
            None: {
                "items": [
                    {"id": "primary", "summary": "Personal", "primary": True},
                    {"id": "lookalike", "summary": "Kalendra", "primary": False},
                ]
            }
        },
    )
    calendars = MagicMock()
    calendars.insert.return_value = Request({"id": "kalendra-secondary", "primary": False})
    google.calendars.return_value = calendars
    events = event_resource(google)
    events.insert.side_effect = lambda **kwargs: Request({"id": "block-1", **kwargs["body"]})
    service = connected_service(tmp_path, google)

    result = await service.create_event(
        {
            "title": "Math pset",
            "description": "Chapter 6",
            "start_time": datetime(2026, 8, 26, 15, tzinfo=UTC),
            "end_time": datetime(2026, 8, 26, 16, tzinfo=UTC),
        },
        reasoning="the only gap between class and practice",
    )

    assert result["calendar_id"] == "kalendra-secondary"
    calendar_body = calendars.insert.call_args.kwargs["body"]
    assert calendar_body["summary"] == "Kalendra"
    assert CALENDAR_OWNERSHIP_MARKER in calendar_body["description"]
    insert = events.insert.call_args
    assert insert.kwargs["calendarId"] == "kalendra-secondary"
    assert insert.kwargs["calendarId"] != "primary"
    assert insert.kwargs["body"]["description"] == (
        "Chapter 6\n\nScheduling rationale: the only gap between class and practice"
    )
    assert insert.kwargs["body"]["extendedProperties"]["private"] == {
        OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
        KIND_MARKER_KEY: FIXED_EVENT_KIND,
    }
    assert insert.kwargs["body"]["colorId"] == "11"
    assert service.kalendra_id_path.read_text(encoding="utf-8").strip() == "kalendra-secondary"
    assert service.kalendra_id_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_create_event_writes_explicit_google_event_color(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    events.insert.side_effect = lambda **kwargs: Request(
        {"id": "colored-event", **kwargs["body"]}
    )
    service = connected_service(tmp_path, google)

    created = await service.create_event(
        {
            "title": "CS311 discussion",
            "start_time": RANGE_START,
            "end_time": RANGE_END,
            "color_id": "6",
        },
        "the user requested this fixed-time event",
    )

    assert events.insert.call_args.kwargs["body"]["colorId"] == "6"
    assert created["color_id"] == "6"


@pytest.mark.asyncio
async def test_create_event_null_color_uses_deterministic_category_default(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    events.insert.side_effect = lambda **kwargs: Request(
        {"id": "default-colored-event", **kwargs["body"]}
    )
    service = connected_service(tmp_path, google)

    created = await service.create_event(
        {
            "title": "CS311 discussion",
            "start_time": RANGE_START,
            "end_time": RANGE_END,
            "category": "school",
            "color_id": None,
        },
        "the user requested this fixed-time event",
    )

    expected = CATEGORY_COLOR_IDS["school"]
    assert events.insert.call_args.kwargs["body"]["colorId"] == expected
    assert created["color_id"] == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_color", ["0", "12", " 5 ", 5])
async def test_create_rejects_invalid_event_color_before_google_mutation(
    tmp_path: Path, invalid_color: object
):
    google = MagicMock()
    events = event_resource(google)
    service = connected_service(tmp_path, google)

    with pytest.raises(ValueError, match="event color id string"):
        await service.create_event(
            {
                "title": "Invalid color",
                "start_time": RANGE_START,
                "end_time": RANGE_END,
                "color_id": invalid_color,
            },
            "the user requested this fixed-time event",
        )

    google.calendarList.return_value.list.assert_not_called()
    google.calendars.return_value.insert.assert_not_called()
    events.insert.assert_not_called()


@pytest.mark.asyncio
async def test_persisted_owned_calendar_is_reused_without_creating_another(tmp_path: Path):
    token = tmp_path / "token.json"
    id_path = token.with_name(f"{token.name}.kalendra-calendar-id")
    id_path.write_text("existing-kalendra\n", encoding="utf-8")
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar("existing-kalendra")]}})
    events = event_resource(google)
    events.insert.side_effect = lambda **kwargs: Request({"id": "new", **kwargs["body"]})
    service = connected_service(tmp_path, google)
    service._kalendra_calendar_id = service._read_persisted_kalendra_id()

    await service.create_event(
        {
            "title": "Work",
            "start_time": RANGE_START,
            "end_time": RANGE_START.replace(hour=1),
        },
        "morning focus window",
    )

    google.calendars.return_value.insert.assert_not_called()
    assert events.insert.call_args.kwargs["calendarId"] == "existing-kalendra"


@pytest.mark.asyncio
async def test_update_and_delete_target_only_marked_kalendra_events(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    current = google_event("block", "Old", marked=True, description="old rationale")
    events.get.return_value = Request(current)
    events.patch.side_effect = lambda **kwargs: Request(
        {**current, **kwargs["body"], "id": kwargs["eventId"]}
    )
    events.delete.return_value = Request(None)
    service = connected_service(tmp_path, google)

    updated = await service.update_event(
        "block",
        {
            "title": "New",
            "reasoning": "moved after the lecture",
            "start_time": datetime(2026, 8, 26, 17, tzinfo=UTC),
            "end_time": datetime(2026, 8, 26, 18, tzinfo=UTC),
        },
    )
    await service.delete_event("block")

    assert updated["title"] == "New"
    patch_call = events.patch.call_args
    assert patch_call.kwargs["calendarId"] == "kalendra-id"
    assert "Scheduling rationale: moved after the lecture" in patch_call.kwargs["body"]["description"]
    delete_call = events.delete.call_args
    assert delete_call.kwargs == {"calendarId": "kalendra-id", "eventId": "block"}


@pytest.mark.asyncio
async def test_update_event_patches_explicit_color_and_returns_it(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    current = google_event("fixed", "Dinner", marked=True)
    current["colorId"] = "2"
    current["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(current)
    events.patch.side_effect = lambda **kwargs: Request(
        {**current, **kwargs["body"], "id": kwargs["eventId"]}
    )
    service = connected_service(tmp_path, google)

    updated = await service.update_event("fixed", {"color_id": "4"})

    assert events.patch.call_args.kwargs["body"] == {"colorId": "4"}
    assert updated["color_id"] == "4"


@pytest.mark.asyncio
async def test_update_event_null_color_clears_override_to_inherit_calendar_color(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    current = google_event("fixed", "Dinner", marked=True)
    current["colorId"] = "2"
    current["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(current)
    events.patch.side_effect = lambda **kwargs: Request(
        {**current, **kwargs["body"], "id": kwargs["eventId"]}
    )
    service = connected_service(tmp_path, google)

    updated = await service.update_event("fixed", {"color_id": None})

    assert events.patch.call_args.kwargs["body"] == {"colorId": None}
    assert updated["color_id"] is None


@pytest.mark.asyncio
async def test_title_only_update_preserves_existing_explicit_color(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    current = google_event("fixed", "Dinner", marked=True)
    current["colorId"] = "3"
    current["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(current)
    events.patch.side_effect = lambda **kwargs: Request(
        {**current, **kwargs["body"], "id": kwargs["eventId"]}
    )
    service = connected_service(tmp_path, google)

    updated = await service.update_event("fixed", {"title": "Late dinner"})

    assert events.patch.call_args.kwargs["body"] == {"summary": "Late dinner"}
    assert updated["color_id"] == "3"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_color", ["0", "12", " 5 ", 5])
async def test_update_rejects_invalid_event_color_before_google_request(
    tmp_path: Path, invalid_color: object
):
    google = MagicMock()
    events = event_resource(google)
    service = connected_service(tmp_path, google)

    with pytest.raises(ValueError, match="event color id string"):
        await service.update_event("fixed", {"color_id": invalid_color})

    events.get.assert_not_called()
    events.patch.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["update", "delete"])
async def test_mutations_refuse_unmarked_events(tmp_path: Path, operation: str):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    events.get.return_value = Request(google_event("real", "User event", marked=False))
    service = connected_service(tmp_path, google)

    with pytest.raises(CalendarError, match="not owned"):
        if operation == "update":
            await service.update_event("real", {"title": "Changed"})
        else:
            await service.delete_event("real")
    events.patch.assert_not_called()
    events.delete.assert_not_called()


@pytest.mark.asyncio
async def test_clear_range_paginates_and_deletes_only_marked_events(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    pages = {
        None: {
            "items": [
                google_event("owned-1", "Owned", marked=True),
                google_event("user-1", "Not ours", marked=False),
            ],
            "nextPageToken": "next",
        },
        "next": {"items": [google_event("owned-2", "Owned too", marked=True)]},
    }
    events.list.side_effect = lambda **kwargs: Request(pages[kwargs["pageToken"]])
    events.delete.return_value = Request(None)
    service = connected_service(tmp_path, google)

    await service.clear_kalendra_range(RANGE_START, RANGE_END)

    assert [call.kwargs["pageToken"] for call in events.list.call_args_list] == [None, "next"]
    assert {call.kwargs["eventId"] for call in events.delete.call_args_list} == {
        "owned-1",
        "owned-2",
    }
    assert all(call.kwargs["calendarId"] == "kalendra-id" for call in events.delete.call_args_list)


@pytest.mark.asyncio
async def test_clear_range_preserves_fixed_events_and_removes_goal_and_task_blocks(
    tmp_path: Path,
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)

    def marked_event(event_id: str, kind: str) -> dict:
        event = google_event(event_id, kind)
        event["extendedProperties"] = {
            "private": {
                OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
                KIND_MARKER_KEY: kind,
            }
        }
        return event

    events.list.return_value = Request(
        {
            "items": [
                marked_event("fixed", FIXED_EVENT_KIND),
                marked_event("task", TASK_BLOCK_KIND),
                marked_event("goal", GOAL_SESSION_KIND),
            ]
        }
    )
    events.delete.return_value = Request(None)
    service = connected_service(tmp_path, google)

    await service.clear_kalendra_range(RANGE_START, RANGE_END)

    assert {call.kwargs["eventId"] for call in events.delete.call_args_list} == {
        "task",
        "goal",
    }


@pytest.mark.asyncio
async def test_delete_work_block_refuses_an_owned_fixed_event(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    fixed = google_event("fixed", "Appointment")
    fixed["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
        }
    }
    events.get.return_value = Request(fixed)
    service = connected_service(tmp_path, google)

    with pytest.raises(CalendarError, match="movable work block"):
        await service.delete_work_block("fixed")

    events.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["event", "work_block"])
async def test_delete_retries_treat_missing_google_event_as_success(
    tmp_path: Path, operation: str
):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    events.get.return_value = Request(
        error=HttpError(
            resp=SimpleNamespace(status=404, reason="not found"), content=b"missing"
        )
    )
    service = connected_service(tmp_path, google)

    if operation == "event":
        await service.delete_event("deleted-before-retry")
    else:
        await service.delete_work_block("deleted-before-retry")

    events.delete.assert_not_called()


@pytest.mark.asyncio
async def test_goal_work_block_has_kind_category_color_and_identifiers(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    events.insert.side_effect = lambda **kwargs: Request(
        {"id": "goal-block", **kwargs["body"]}
    )
    service = connected_service(tmp_path, google)

    event_id = await service.create_work_block(
        42,
        "Gym",
        RANGE_START,
        RANGE_END,
        "the open session keeps this week's quota reachable",
        category="fitness",
        kind=GOAL_SESSION_KIND,
        goal_id=8,
    )

    body = events.insert.call_args.kwargs["body"]
    assert event_id == "goal-block"
    assert body["colorId"] == CATEGORY_COLOR_IDS["fitness"]
    assert body["extendedProperties"]["private"] == {
        "task_id": "42",
        "goal_id": "8",
        OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
        KIND_MARKER_KEY: GOAL_SESSION_KIND,
        "category": "fitness",
    }


@pytest.mark.asyncio
async def test_category_update_preserves_a_manual_google_event_color(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    current = google_event("fixed", "Dinner")
    current["colorId"] = "3"
    current["extendedProperties"] = {
        "private": {
            OWNERSHIP_MARKER_KEY: OWNERSHIP_MARKER_VALUE,
            KIND_MARKER_KEY: FIXED_EVENT_KIND,
            "category": "personal",
        }
    }
    events.get.return_value = Request(current)
    events.patch.side_effect = lambda **kwargs: Request(
        {**current, **kwargs["body"], "id": kwargs["eventId"]}
    )
    service = connected_service(tmp_path, google)

    await service.update_event("fixed", {"category": "work"})

    assert "colorId" not in events.patch.call_args.kwargs["body"]


@pytest.mark.asyncio
async def test_create_requires_nonblank_reasoning_before_google_insert(tmp_path: Path):
    google = MagicMock()
    calendar_list(google, {None: {"items": [owned_calendar()]}})
    events = event_resource(google)
    service = connected_service(tmp_path, google)

    with pytest.raises(ValueError, match="reasoning"):
        await service.create_event(
            {"title": "Opaque", "start_time": RANGE_START, "end_time": RANGE_END},
            "   ",
        )
    events.insert.assert_not_called()
