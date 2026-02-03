"""Google Calendar API integration for reading events."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from src.config import config
from src.utils import format_datetime_iso, get_current_time, get_day_range

logger = logging.getLogger(__name__)

# Read-only scope for calendar access
SCOPES = ["https://www.googleapis.com/auth/calendar.readonly"]


class CalendarService:
    """Google Calendar API wrapper for reading events."""

    def __init__(
        self,
        credentials_path: Path | None = None,
        token_path: Path | None = None,
    ):
        """Initialize the calendar service.

        Args:
            credentials_path: Path to credentials.json file.
            token_path: Path to token.json file.
        """
        self.credentials_path = credentials_path or config.GOOGLE_CALENDAR_CREDENTIALS_PATH
        self.token_path = token_path or config.GOOGLE_CALENDAR_TOKEN_PATH
        self._service = None
        self._credentials = None

    def _get_credentials(self) -> Credentials | None:
        """Get or refresh Google API credentials.

        Returns:
            Valid credentials or None if unavailable.
        """
        if self._credentials and self._credentials.valid:
            return self._credentials

        creds = None

        # Load existing token
        if self.token_path.exists():
            try:
                creds = Credentials.from_authorized_user_file(
                    str(self.token_path), SCOPES
                )
            except Exception as e:
                logger.warning(f"Failed to load token: {e}")

        # Refresh expired credentials
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                # Save refreshed token
                with open(self.token_path, "w") as token:
                    token.write(creds.to_json())
            except Exception as e:
                logger.warning(f"Failed to refresh token: {e}")
                creds = None

        self._credentials = creds
        return creds

    def _get_service(self):
        """Get or create the Calendar API service.

        Returns:
            Calendar API service or None if unavailable.
        """
        if self._service:
            return self._service

        creds = self._get_credentials()
        if not creds:
            logger.warning("No valid credentials available for Google Calendar")
            return None

        try:
            self._service = build("calendar", "v3", credentials=creds)
            return self._service
        except Exception as e:
            logger.error(f"Failed to build Calendar service: {e}")
            return None

    def is_available(self) -> bool:
        """Check if the calendar service is available.

        Returns:
            True if service is configured and authenticated.
        """
        return self._get_service() is not None

    async def get_today_events(self) -> list[dict[str, Any]]:
        """Get today's events from Google Calendar.

        Returns:
            List of event dicts formatted for the bot.
        """
        start, end = get_day_range()
        return await self.get_events_between(start, end)

    async def get_events_between(
        self, start: datetime, end: datetime
    ) -> list[dict[str, Any]]:
        """Get events within a time range.

        Args:
            start: Start datetime.
            end: End datetime.

        Returns:
            List of event dicts formatted for the bot.
        """
        service = self._get_service()
        if not service:
            return []

        try:
            # Format times for API
            time_min = start.isoformat()
            time_max = end.isoformat()

            events_result = (
                service.events()
                .list(
                    calendarId="primary",
                    timeMin=time_min,
                    timeMax=time_max,
                    singleEvents=True,
                    orderBy="startTime",
                )
                .execute()
            )

            events = events_result.get("items", [])
            return [self._format_event(event) for event in events]

        except HttpError as e:
            logger.error(f"Google Calendar API error: {e}")
            return []
        except Exception as e:
            logger.error(f"Failed to fetch calendar events: {e}")
            return []

    def _format_event(self, gcal_event: dict[str, Any]) -> dict[str, Any]:
        """Format a Google Calendar event to match bot event structure.

        Args:
            gcal_event: Raw event from Google Calendar API.

        Returns:
            Formatted event dict.
        """
        # Extract start time (can be date or dateTime)
        start = gcal_event.get("start", {})
        start_time = start.get("dateTime") or start.get("date")

        # Extract end time
        end = gcal_event.get("end", {})
        end_time = end.get("dateTime") or end.get("date")

        return {
            "id": gcal_event.get("id", ""),
            "title": gcal_event.get("summary", "Untitled Event"),
            "description": gcal_event.get("description"),
            "start_time": start_time,
            "end_time": end_time,
            "location": gcal_event.get("location"),
            "source": "gcal",
        }

    async def check_availability(
        self, start: datetime, end: datetime
    ) -> bool:
        """Check if a time slot is available.

        Args:
            start: Start of time slot.
            end: End of time slot.

        Returns:
            True if no events overlap with the time slot.
        """
        events = await self.get_events_between(start, end)
        return len(events) == 0

    async def get_upcoming_events(self, days: int = 7) -> list[dict[str, Any]]:
        """Get events for the next N days.

        Args:
            days: Number of days to look ahead.

        Returns:
            List of event dicts.
        """
        now = get_current_time()
        end = now + timedelta(days=days)
        return await self.get_events_between(now, end)


def run_oauth_flow(
    credentials_path: Path | None = None,
    token_path: Path | None = None,
) -> bool:
    """Run the OAuth flow to authorize Google Calendar access.

    This should be run interactively to authorize the application.

    Args:
        credentials_path: Path to credentials.json file.
        token_path: Path to save token.json file.

    Returns:
        True if authorization was successful.
    """
    credentials_path = credentials_path or config.GOOGLE_CALENDAR_CREDENTIALS_PATH
    token_path = token_path or config.GOOGLE_CALENDAR_TOKEN_PATH

    if not credentials_path.exists():
        print(f"Error: credentials.json not found at {credentials_path}")
        print("Download it from Google Cloud Console:")
        print("1. Go to https://console.cloud.google.com/")
        print("2. Create a project and enable Calendar API")
        print("3. Create OAuth credentials (Desktop app)")
        print("4. Download and save as credentials.json")
        return False

    try:
        flow = InstalledAppFlow.from_client_secrets_file(
            str(credentials_path), SCOPES
        )
        creds = flow.run_local_server(port=0)

        # Save the credentials
        with open(token_path, "w") as token:
            token.write(creds.to_json())

        print(f"Authorization successful! Token saved to {token_path}")
        return True

    except Exception as e:
        print(f"Authorization failed: {e}")
        return False


async def create_calendar_service() -> CalendarService:
    """Create and return a configured CalendarService instance.

    Returns:
        Configured CalendarService.
    """
    return CalendarService()
