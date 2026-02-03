"""Tests for the calendar service module."""

import json
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import pytz

from src.calendar_service import CalendarService


class TestCalendarService:
    """Tests for the CalendarService class."""

    def test_init(self):
        """Test service initialization."""
        service = CalendarService(
            credentials_path=Path("/fake/credentials.json"),
            token_path=Path("/fake/token.json"),
        )

        assert service.credentials_path == Path("/fake/credentials.json")
        assert service.token_path == Path("/fake/token.json")

    def test_is_available_no_credentials(self):
        """Test availability check with no credentials."""
        service = CalendarService(
            credentials_path=Path("/fake/credentials.json"),
            token_path=Path("/nonexistent/token.json"),
        )

        # Should return False when token doesn't exist
        assert not service.is_available()

    @patch("src.calendar_service.Credentials")
    @patch("src.calendar_service.build")
    def test_is_available_with_credentials(self, mock_build, mock_creds_class):
        """Test availability check with valid credentials."""
        # Set up mock credentials - must set valid=True and expired=False
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        # Create temp token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"token": "test"}')
            token_path = Path(f.name)

        try:
            service = CalendarService(
                credentials_path=Path("/fake/credentials.json"),
                token_path=token_path,
            )

            assert service.is_available()
        finally:
            token_path.unlink(missing_ok=True)


class TestEventFormatting:
    """Tests for event formatting."""

    def test_format_event_basic(self):
        """Test formatting a basic event."""
        service = CalendarService()

        gcal_event = {
            "id": "event123",
            "summary": "Team Meeting",
            "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
            "end": {"dateTime": "2024-01-20T15:00:00-06:00"},
        }

        formatted = service._format_event(gcal_event)

        assert formatted["id"] == "event123"
        assert formatted["title"] == "Team Meeting"
        assert formatted["start_time"] == "2024-01-20T14:00:00-06:00"
        assert formatted["end_time"] == "2024-01-20T15:00:00-06:00"
        assert formatted["source"] == "gcal"

    def test_format_event_with_location(self):
        """Test formatting an event with location."""
        service = CalendarService()

        gcal_event = {
            "id": "event123",
            "summary": "Coffee Meeting",
            "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
            "end": {"dateTime": "2024-01-20T15:00:00-06:00"},
            "location": "Starbucks",
        }

        formatted = service._format_event(gcal_event)

        assert formatted["location"] == "Starbucks"

    def test_format_event_with_description(self):
        """Test formatting an event with description."""
        service = CalendarService()

        gcal_event = {
            "id": "event123",
            "summary": "Planning Meeting",
            "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
            "description": "Discuss Q1 goals",
        }

        formatted = service._format_event(gcal_event)

        assert formatted["description"] == "Discuss Q1 goals"

    def test_format_event_all_day(self):
        """Test formatting an all-day event."""
        service = CalendarService()

        gcal_event = {
            "id": "event123",
            "summary": "Holiday",
            "start": {"date": "2024-01-20"},
            "end": {"date": "2024-01-21"},
        }

        formatted = service._format_event(gcal_event)

        assert formatted["start_time"] == "2024-01-20"
        assert formatted["end_time"] == "2024-01-21"

    def test_format_event_missing_summary(self):
        """Test formatting an event with missing summary."""
        service = CalendarService()

        gcal_event = {
            "id": "event123",
            "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
        }

        formatted = service._format_event(gcal_event)

        assert formatted["title"] == "Untitled Event"


class TestEventFetching:
    """Tests for event fetching with mocked API."""

    @pytest.mark.asyncio
    async def test_get_today_events_no_service(self):
        """Test getting today's events when service unavailable."""
        service = CalendarService(
            credentials_path=Path("/fake/credentials.json"),
            token_path=Path("/nonexistent/token.json"),
        )

        events = await service.get_today_events()

        assert events == []

    @pytest.mark.asyncio
    @patch("src.calendar_service.build")
    @patch("src.calendar_service.Credentials")
    async def test_get_events_between_success(self, mock_creds_class, mock_build):
        """Test successful event fetching."""
        # Set up mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        # Set up mock service
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Meeting 1",
                    "start": {"dateTime": "2024-01-20T10:00:00-06:00"},
                    "end": {"dateTime": "2024-01-20T11:00:00-06:00"},
                },
                {
                    "id": "event2",
                    "summary": "Meeting 2",
                    "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
                    "end": {"dateTime": "2024-01-20T15:00:00-06:00"},
                },
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        # Create temp token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"token": "test"}')
            token_path = Path(f.name)

        try:
            service = CalendarService(
                credentials_path=Path("/fake/credentials.json"),
                token_path=token_path,
            )

            tz = pytz.timezone("America/Chicago")
            start = datetime(2024, 1, 20, 0, 0, 0, tzinfo=tz)
            end = datetime(2024, 1, 20, 23, 59, 59, tzinfo=tz)

            events = await service.get_events_between(start, end)

            assert len(events) == 2
            assert events[0]["title"] == "Meeting 1"
            assert events[1]["title"] == "Meeting 2"
        finally:
            token_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    @patch("src.calendar_service.build")
    @patch("src.calendar_service.Credentials")
    async def test_get_events_api_error(self, mock_creds_class, mock_build):
        """Test handling of API errors."""
        from googleapiclient.errors import HttpError

        # Set up mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        # Set up mock service that raises an error
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.side_effect = HttpError(
            resp=MagicMock(status=500), content=b"Server Error"
        )
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        # Create temp token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"token": "test"}')
            token_path = Path(f.name)

        try:
            service = CalendarService(
                credentials_path=Path("/fake/credentials.json"),
                token_path=token_path,
            )

            tz = pytz.timezone("America/Chicago")
            start = datetime(2024, 1, 20, 0, 0, 0, tzinfo=tz)
            end = datetime(2024, 1, 20, 23, 59, 59, tzinfo=tz)

            events = await service.get_events_between(start, end)

            # Should return empty list on error
            assert events == []
        finally:
            token_path.unlink(missing_ok=True)


class TestAvailabilityCheck:
    """Tests for availability checking."""

    @pytest.mark.asyncio
    @patch("src.calendar_service.build")
    @patch("src.calendar_service.Credentials")
    async def test_check_availability_free(self, mock_creds_class, mock_build):
        """Test checking availability when slot is free."""
        # Set up mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        # Set up mock service with no events
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {"items": []}
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        # Create temp token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"token": "test"}')
            token_path = Path(f.name)

        try:
            service = CalendarService(
                credentials_path=Path("/fake/credentials.json"),
                token_path=token_path,
            )

            tz = pytz.timezone("America/Chicago")
            start = datetime(2024, 1, 20, 14, 0, 0, tzinfo=tz)
            end = datetime(2024, 1, 20, 15, 0, 0, tzinfo=tz)

            is_available = await service.check_availability(start, end)

            assert is_available is True
        finally:
            token_path.unlink(missing_ok=True)

    @pytest.mark.asyncio
    @patch("src.calendar_service.build")
    @patch("src.calendar_service.Credentials")
    async def test_check_availability_busy(self, mock_creds_class, mock_build):
        """Test checking availability when slot is busy."""
        # Set up mock credentials
        mock_creds = MagicMock()
        mock_creds.valid = True
        mock_creds.expired = False
        mock_creds_class.from_authorized_user_file.return_value = mock_creds

        # Set up mock service with an event
        mock_service = MagicMock()
        mock_events = MagicMock()
        mock_list = MagicMock()
        mock_list.execute.return_value = {
            "items": [
                {
                    "id": "event1",
                    "summary": "Existing Meeting",
                    "start": {"dateTime": "2024-01-20T14:00:00-06:00"},
                    "end": {"dateTime": "2024-01-20T15:00:00-06:00"},
                }
            ]
        }
        mock_events.list.return_value = mock_list
        mock_service.events.return_value = mock_events
        mock_build.return_value = mock_service

        # Create temp token file
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write('{"token": "test"}')
            token_path = Path(f.name)

        try:
            service = CalendarService(
                credentials_path=Path("/fake/credentials.json"),
                token_path=token_path,
            )

            tz = pytz.timezone("America/Chicago")
            start = datetime(2024, 1, 20, 14, 0, 0, tzinfo=tz)
            end = datetime(2024, 1, 20, 15, 0, 0, tzinfo=tz)

            is_available = await service.check_availability(start, end)

            assert is_available is False
        finally:
            token_path.unlink(missing_ok=True)
