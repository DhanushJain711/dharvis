"""Google Calendar boundary; business logic belongs to the calendar agent."""

from __future__ import annotations

from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import config
from .timeutil import day_bounds, now_local, now_utc, to_utc

CalendarRecord = dict[str, Any]
SCOPES = ["https://www.googleapis.com/auth/calendar"]
logger = logging.getLogger(__name__)


def _write_token_securely(path: Path, content: str) -> None:
    """Write OAuth token material without a permissive-umask window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as token_file:
            descriptor = -1
            token_file.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _google_time_to_utc(value: dict[str, Any]) -> str | None:
    """Normalize one Google ``date`` or ``dateTime`` object to UTC text."""
    date_time = value.get("dateTime")
    if date_time:
        parsed = datetime.fromisoformat(str(date_time).replace("Z", "+00:00"))
        return to_utc(parsed).isoformat()
    date_value = value.get("date")
    if date_value:
        start, _ = day_bounds(date.fromisoformat(str(date_value)))
        return start.isoformat()
    return None


class CalendarService:
    """Contract for merged calendar reads and Kalendra work-block writes."""

    def __init__(
        self,
        credentials_path: Path | None = None,
        token_path: Path | None = None,
        calendar_id: str | None = None,
    ) -> None:
        self.credentials_path = credentials_path or config.GOOGLE_CALENDAR_CREDENTIALS_PATH
        self.token_path = token_path or config.GOOGLE_CALENDAR_TOKEN_PATH
        self.calendar_id = calendar_id or config.GOOGLE_CALENDAR_ID
        self._credentials: Credentials | None = None
        self._service: Any | None = None
        self._last_query_complete = False

    def _get_credentials(self) -> Credentials | None:
        """Load or refresh existing OAuth credentials without opening a flow."""
        if self._credentials and self._credentials.valid:
            return self._credentials
        if not self.token_path.exists():
            return None
        try:
            credentials = Credentials.from_authorized_user_file(
                str(self.token_path), SCOPES
            )
            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                _write_token_securely(self.token_path, credentials.to_json())
            self._credentials = credentials
            return credentials
        except Exception:
            logger.warning("Google Calendar credentials are unavailable", exc_info=True)
            return None

    def _get_service(self) -> Any | None:
        """Lazily build the Google Calendar API client."""
        if self._service is not None:
            return self._service
        credentials = self._get_credentials()
        if credentials is None:
            return None
        try:
            self._service = build("calendar", "v3", credentials=credentials)
        except Exception:
            logger.warning("Google Calendar client could not be built", exc_info=True)
            return None
        return self._service

    def is_available(self) -> bool:
        """Return whether an authenticated API client can be constructed."""
        return self._get_service() is not None

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        """List Google Calendar events overlapping an aware UTC range."""
        service = self._get_service()
        if service is None:
            self._last_query_complete = False
            return []
        try:
            response = service.events().list(
                calendarId=self.calendar_id,
                timeMin=to_utc(start).isoformat(),
                timeMax=to_utc(end).isoformat(),
                singleEvents=True,
                orderBy="startTime",
            ).execute()
            normalized: list[CalendarRecord] = []
            self._last_query_complete = True
            for event in response.get("items", []):
                try:
                    normalized.append(self._format_event(event))
                except (TypeError, ValueError):
                    self._last_query_complete = False
                    logger.warning(
                        "Skipping malformed Google Calendar event %r",
                        event.get("id", "<unknown>"),
                        exc_info=True,
                    )
            return normalized
        except (HttpError, OSError, ValueError):
            self._last_query_complete = False
            logger.warning("Google Calendar event query failed", exc_info=True)
            return []

    async def get_events_between(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        """Compatibility alias for :meth:`list_events`."""
        return await self.list_events(start, end)

    async def get_today_events(self) -> list[CalendarRecord]:
        """Return today's Google events; implemented by the calendar agent."""
        start, end = day_bounds(now_local())
        return await self.list_events(start, end)

    async def get_upcoming_events(self, days: int = 7) -> list[CalendarRecord]:
        """Return Google events in the next number of days."""
        return await self.list_events(now_utc(), now_utc() + timedelta(days=days))

    async def check_availability(self, start: datetime, end: datetime) -> bool:
        """Return whether Google Calendar has no event in an aware UTC range."""
        events = await self.list_events(start, end)
        return self._last_query_complete and not events

    async def create_event(self, event: CalendarRecord) -> CalendarRecord:
        """Create a user event on the configured calendar."""
        raise NotImplementedError

    async def update_event(self, gcal_event_id: str, changes: CalendarRecord) -> CalendarRecord:
        """Update a Google Calendar event by provider id."""
        raise NotImplementedError

    async def delete_event(self, gcal_event_id: str) -> None:
        """Delete a Google Calendar event by provider id."""
        raise NotImplementedError

    async def create_work_block(
        self, task_id: int, title: str, start: datetime, end: datetime
    ) -> str:
        """Create a task block on the dedicated Kalendra calendar."""
        raise NotImplementedError

    async def update_work_block(
        self, gcal_event_id: str, title: str, start: datetime, end: datetime
    ) -> None:
        """Move or resize a Kalendra task block."""
        raise NotImplementedError

    async def delete_work_block(self, gcal_event_id: str) -> None:
        """Delete a Kalendra task block."""
        raise NotImplementedError

    def _format_event(self, event: CalendarRecord) -> CalendarRecord:
        """Normalize a raw Google event into the shared schedule shape."""
        start = event.get("start", {})
        end = event.get("end", {})
        start_time = _google_time_to_utc(start)
        end_time = _google_time_to_utc(end)
        if start_time is None:
            raise ValueError("Google event is missing a start date or dateTime")
        if end_time is None:
            end_time = (
                datetime.fromisoformat(start_time) + timedelta(hours=1)
            ).isoformat()
        return {
            "id": event.get("id", ""),
            "title": event.get("summary", "Untitled Event"),
            "description": event.get("description"),
            "start_time": start_time,
            "end_time": end_time,
            "location": event.get("location"),
            "source": "gcal",
            "gcal_event_id": event.get("id", ""),
        }


def run_oauth_flow(
    credentials_path: Path | None = None, token_path: Path | None = None
) -> bool:
    """Authorize Google Calendar interactively; implemented by the calendar agent."""
    raise NotImplementedError


async def create_calendar_service() -> CalendarService:
    """Create the calendar service without making a network request."""
    return CalendarService()
