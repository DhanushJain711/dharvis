"""Google Calendar integration with isolated, reversible Kalendra writes."""

from __future__ import annotations

from copy import deepcopy
from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path
from time import monotonic
from typing import Any

from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import config
from .timeutil import day_bounds, now_local, now_utc, to_utc

CalendarRecord = dict[str, Any]
SCOPES = ["https://www.googleapis.com/auth/calendar"]
CACHE_TTL_SECONDS = 60.0
CALENDAR_OWNERSHIP_MARKER = "kalendra-managed-calendar:v1"
WORK_BLOCK_MARKER_KEY = "kalendra"
WORK_BLOCK_MARKER_VALUE = "work-block"
RATIONALE_PREFIX = "Scheduling rationale:"
logger = logging.getLogger(__name__)


class CalendarError(RuntimeError):
    """Base class for failures at the Google Calendar boundary."""


class CalendarReconnectRequiredError(CalendarError):
    """OAuth authorization is no longer usable and must be re-established."""


CalendarReconnectRequired = CalendarReconnectRequiredError


def _write_securely(path: Path, content: str) -> None:
    """Write private application state without a permissive-umask window."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as output_file:
            descriptor = -1
            output_file.write(content)
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _write_token_securely(path: Path, content: str) -> None:
    """Compatibility wrapper for securely persisted OAuth token material."""
    _write_securely(path, content)


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


def _aware_datetime(value: datetime | str, name: str) -> datetime:
    parsed = (
        datetime.fromisoformat(value.replace("Z", "+00:00"))
        if isinstance(value, str)
        else value
    )
    try:
        return to_utc(parsed)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a timezone-aware datetime") from exc


def _validate_range(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    start_utc = _aware_datetime(start, "start")
    end_utc = _aware_datetime(end, "end")
    if end_utc <= start_utc:
        raise ValueError("end must be later than start")
    return start_utc, end_utc


def _http_status(exc: HttpError) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


class CalendarService:
    """Merged calendar reads and writes isolated to a Kalendra calendar."""

    def __init__(
        self,
        credentials_path: Path | None = None,
        token_path: Path | None = None,
        calendar_id: str | None = None,
    ) -> None:
        self.credentials_path = credentials_path or config.GOOGLE_CALENDAR_CREDENTIALS_PATH
        self.token_path = token_path or config.GOOGLE_CALENDAR_TOKEN_PATH
        self.calendar_id = calendar_id or config.GOOGLE_CALENDAR_ID
        self.kalendra_id_path = self.token_path.with_name(
            f"{self.token_path.name}.kalendra-calendar-id"
        )
        self._credentials: Credentials | None = None
        self._service: Any | None = None
        self._kalendra_calendar_id: str | None = (
            config.KALENDRA_CALENDAR_ID or self._read_persisted_kalendra_id()
        )
        self._kalendra_id_validated = False
        self._event_cache: dict[
            tuple[str, str], tuple[float, list[CalendarRecord]]
        ] = {}
        self._last_query_complete = False

    def _read_persisted_kalendra_id(self) -> str | None:
        try:
            value = self.kalendra_id_path.read_text(encoding="utf-8").strip()
            if value:
                self.kalendra_id_path.chmod(0o600)
                return value
        except FileNotFoundError:
            pass
        except OSError:
            logger.warning("Could not read persisted Kalendra calendar id", exc_info=True)
        return None

    def _persist_kalendra_id(self, calendar_id: str) -> None:
        _write_securely(self.kalendra_id_path, f"{calendar_id}\n")
        self._kalendra_calendar_id = calendar_id
        self._kalendra_id_validated = True

    def _get_credentials(self) -> Credentials | None:
        """Load and transparently refresh OAuth credentials without opening a flow."""
        credentials = self._credentials
        if credentials is None:
            if not self.token_path.exists():
                return None
            try:
                credentials = Credentials.from_authorized_user_file(
                    str(self.token_path), SCOPES
                )
            except (OSError, ValueError) as exc:
                raise CalendarReconnectRequiredError(
                    "Google Calendar authorization is unreadable; reconnect your calendar"
                ) from exc

        if credentials.expired:
            if not credentials.refresh_token:
                raise CalendarReconnectRequiredError(
                    "Google Calendar authorization expired; reconnect your calendar"
                )
            try:
                credentials.refresh(Request())
                _write_token_securely(self.token_path, credentials.to_json())
            except Exception as exc:
                self._credentials = None
                self._service = None
                raise CalendarReconnectRequiredError(
                    "Google Calendar authorization could not be refreshed; reconnect your calendar"
                ) from exc
        if not credentials.valid:
            raise CalendarReconnectRequiredError(
                "Google Calendar authorization is invalid; reconnect your calendar"
            )
        self._credentials = credentials
        return credentials

    def _get_service(self) -> Any | None:
        """Lazily build the Google Calendar API client."""
        if self._service is not None:
            return self._service
        credentials = self._get_credentials()
        if credentials is None:
            return None
        try:
            self._service = build(
                "calendar", "v3", credentials=credentials, cache_discovery=False
            )
        except RefreshError as exc:
            raise CalendarReconnectRequiredError(
                "Google Calendar authorization could not be refreshed; reconnect your calendar"
            ) from exc
        except Exception as exc:
            logger.warning("Google Calendar client could not be built", exc_info=True)
            raise CalendarError("Google Calendar client could not be created") from exc
        return self._service

    def is_available(self) -> bool:
        """Return whether an authenticated API client can be constructed."""
        try:
            return self._get_service() is not None
        except CalendarError:
            return False

    def _invalidate_cache(self) -> None:
        self._event_cache.clear()

    def _execute(self, request: Any) -> Any:
        """Execute one request after proactively refreshing cached credentials."""
        credentials = self._get_credentials()
        if credentials is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )
        try:
            return request.execute()
        except RefreshError as exc:
            self._credentials = None
            self._service = None
            raise CalendarReconnectRequiredError(
                "Google Calendar authorization could not be refreshed; reconnect your calendar"
            ) from exc

    def _raise_if_reconnect_required(self, exc: HttpError) -> None:
        if _http_status(exc) in (401, 403):
            self._credentials = None
            self._service = None
            raise CalendarReconnectRequiredError(
                "Google Calendar authorization was rejected; reconnect your calendar"
            ) from exc

    def _calendar_ids(self, service: Any) -> list[str]:
        """Return every visible calendar id, paginating the calendar list."""
        ids: list[str] = []
        page_token: str | None = None
        while True:
            response = self._execute(
                service.calendarList().list(pageToken=page_token)
            )
            items = response.get("items", [])
            if isinstance(items, list):
                ids.extend(
                    str(item["id"])
                    for item in items
                    if isinstance(item, dict) and item.get("id")
                )
            page_token = response.get("nextPageToken")
            if not isinstance(page_token, str) or not page_token:
                break
        if self.calendar_id and self.calendar_id not in ids:
            ids.append(self.calendar_id)
        return list(dict.fromkeys(ids))

    async def list_events(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        """List overlapping events across all visible calendars, with a 60s cache."""
        start_utc, end_utc = _validate_range(start, end)
        cache_key = (start_utc.isoformat(), end_utc.isoformat())
        cached = self._event_cache.get(cache_key)
        if cached and monotonic() - cached[0] < CACHE_TTL_SECONDS:
            self._last_query_complete = True
            return deepcopy(cached[1])

        service = self._get_service()
        if service is None:
            self._last_query_complete = False
            return []
        normalized: list[CalendarRecord] = []
        complete = True
        try:
            for calendar_id in self._calendar_ids(service):
                page_token: str | None = None
                while True:
                    response = self._execute(
                        service.events().list(
                            calendarId=calendar_id,
                            timeMin=start_utc.isoformat(),
                            timeMax=end_utc.isoformat(),
                            singleEvents=True,
                            orderBy="startTime",
                            pageToken=page_token,
                        )
                    )
                    for event in response.get("items", []):
                        try:
                            formatted = self._format_event(event)
                            formatted["calendar_id"] = calendar_id
                            normalized.append(formatted)
                        except (TypeError, ValueError):
                            complete = False
                            logger.warning(
                                "Skipping malformed Google Calendar event %r",
                                event.get("id", "<unknown>"),
                                exc_info=True,
                            )
                    page_token = response.get("nextPageToken")
                    if not page_token:
                        break
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            complete = False
            logger.warning("Google Calendar event query failed", exc_info=True)
        except (OSError, TypeError, ValueError):
            complete = False
            logger.warning("Google Calendar event query failed", exc_info=True)

        normalized.sort(key=lambda item: (item["start_time"], item["end_time"]))
        self._last_query_complete = complete
        if complete:
            self._event_cache[cache_key] = (monotonic(), deepcopy(normalized))
        return normalized

    async def get_events_between(self, start: datetime, end: datetime) -> list[CalendarRecord]:
        return await self.list_events(start, end)

    async def get_today_events(self) -> list[CalendarRecord]:
        start, end = day_bounds(now_local())
        return await self.list_events(start, end)

    async def get_upcoming_events(self, days: int = 7) -> list[CalendarRecord]:
        if days <= 0:
            raise ValueError("days must be positive")
        start = now_utc()
        return await self.list_events(start, start + timedelta(days=days))

    async def check_availability(self, start: datetime, end: datetime) -> bool:
        events = await self.list_events(start, end)
        return self._last_query_complete and not events

    def _ensure_kalendra_calendar(self) -> str:
        """Find or create the dedicated secondary calendar, then persist its id."""
        if self._kalendra_calendar_id and self._kalendra_id_validated:
            return self._kalendra_calendar_id
        service = self._get_service()
        if service is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )

        page_token: str | None = None
        try:
            entries: list[CalendarRecord] = []
            while True:
                response = self._execute(
                    service.calendarList().list(pageToken=page_token)
                )
                entries.extend(
                    entry for entry in response.get("items", []) if isinstance(entry, dict)
                )
                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            candidate_id = self._kalendra_calendar_id
            if candidate_id:
                candidate = next(
                    (entry for entry in entries if str(entry.get("id")) == candidate_id),
                    None,
                )
                if candidate is not None and self._is_owned_secondary_calendar(candidate):
                    self._persist_kalendra_id(candidate_id)
                    return candidate_id

            # A marker is authoritative; a user-created name-only calendar is not.
            owned = next(
                (entry for entry in entries if self._is_owned_secondary_calendar(entry)),
                None,
            )
            if owned is not None:
                calendar_id = str(owned["id"])
                self._persist_kalendra_id(calendar_id)
                return calendar_id

            created = self._execute(
                service.calendars().insert(
                    body={
                        "summary": config.KALENDRA_CALENDAR_NAME,
                        "description": (
                            "Work blocks scheduled by Kalendra.\n\n"
                            f"{CALENDAR_OWNERSHIP_MARKER}"
                        ),
                        "timeZone": config.USER_TIMEZONE,
                    }
                )
            )
            calendar_id = str(created.get("id", "")).strip()
            if not calendar_id or calendar_id == "primary" or created.get("primary") is True:
                raise CalendarError(
                    "Google did not return a safe secondary id for the Kalendra calendar"
                )
            self._persist_kalendra_id(calendar_id)
            return calendar_id
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            raise CalendarError("Could not create the Kalendra calendar") from exc

    @staticmethod
    def _is_owned_secondary_calendar(entry: CalendarRecord) -> bool:
        calendar_id = str(entry.get("id", "")).strip()
        return bool(
            calendar_id
            and calendar_id != "primary"
            and entry.get("primary") is not True
            and CALENDAR_OWNERSHIP_MARKER in str(entry.get("description") or "")
        )

    @staticmethod
    def _is_work_block(event: CalendarRecord) -> bool:
        private = (event.get("extendedProperties") or {}).get("private") or {}
        return private.get(WORK_BLOCK_MARKER_KEY) == WORK_BLOCK_MARKER_VALUE

    @staticmethod
    def _description_with_reasoning(description: Any, reasoning: Any) -> str:
        rationale = str(reasoning or "").strip()
        if not rationale:
            raise ValueError("reasoning must contain non-whitespace text")
        base = str(description or "").strip()
        if RATIONALE_PREFIX in base:
            base = base.split(RATIONALE_PREFIX, 1)[0].rstrip()
        rendered = f"{RATIONALE_PREFIX} {rationale}"
        return f"{base}\n\n{rendered}" if base else rendered

    def _event_body(
        self, event: CalendarRecord, reasoning: str | None = None
    ) -> CalendarRecord:
        start_value = event.get("start_time", event.get("start"))
        end_value = event.get("end_time", event.get("end"))
        if isinstance(start_value, dict):
            start_value = start_value.get("dateTime")
        if isinstance(end_value, dict):
            end_value = end_value.get("dateTime")
        if start_value is None or end_value is None:
            raise ValueError("event requires start_time and end_time")
        start, end = _validate_range(
            _aware_datetime(start_value, "start_time"),
            _aware_datetime(end_value, "end_time"),
        )
        rationale = reasoning if reasoning is not None else event.get("reasoning")
        description = self._description_with_reasoning(
            event.get("description"), rationale
        )
        body: CalendarRecord = {
            "summary": event.get("title", event.get("summary", "Untitled Event")),
            "start": {"dateTime": start.isoformat()},
            "end": {"dateTime": end.isoformat()},
        }
        if description:
            body["description"] = description
        if event.get("location"):
            body["location"] = event["location"]
        extended = deepcopy(event.get("extendedProperties") or {})
        private = extended.setdefault("private", {})
        if not isinstance(private, dict):
            raise ValueError("extendedProperties.private must be an object")
        private[WORK_BLOCK_MARKER_KEY] = WORK_BLOCK_MARKER_VALUE
        body["extendedProperties"] = extended
        return body

    async def create_event(
        self, event: CalendarRecord, reasoning: str | None = None
    ) -> CalendarRecord:
        """Create a reasoned, application-owned event on Kalendra."""
        service = self._get_service()
        if service is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )
        calendar_id = self._ensure_kalendra_calendar()
        try:
            created = self._execute(
                service.events().insert(
                    calendarId=calendar_id, body=self._event_body(event, reasoning)
                )
            )
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            raise CalendarError("Could not create the calendar event") from exc
        self._invalidate_cache()
        formatted = self._format_event(created)
        formatted["calendar_id"] = calendar_id
        return formatted

    async def update_event(
        self, gcal_event_id: str, changes: CalendarRecord
    ) -> CalendarRecord:
        """Patch an event in Kalendra; primary and other calendars are untouched."""
        if not gcal_event_id:
            raise ValueError("gcal_event_id must be non-empty")
        service = self._get_service()
        if service is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )
        calendar_id = self._ensure_kalendra_calendar()
        body: CalendarRecord = {}
        if "title" in changes or "summary" in changes:
            body["summary"] = changes.get("title", changes.get("summary"))
        if "description" in changes:
            body["description"] = changes["description"]
        if "location" in changes:
            body["location"] = changes["location"]
        try:
            current = self._execute(
                service.events().get(
                    calendarId=calendar_id, eventId=gcal_event_id
                )
            )
            if not self._is_work_block(current):
                raise CalendarError("Refusing to update an event not owned by Kalendra")
            if "reasoning" in changes:
                body["description"] = self._description_with_reasoning(
                    changes.get("description", current.get("description")),
                    changes["reasoning"],
                )
            if any(key in changes for key in ("start_time", "end_time", "start", "end")):
                start_value = changes.get("start_time", changes.get("start", current["start"]))
                end_value = changes.get("end_time", changes.get("end", current["end"]))
                start_time, end_time = _validate_range(
                    _aware_datetime(
                        start_value.get("dateTime") if isinstance(start_value, dict) else start_value,
                        "start_time",
                    ),
                    _aware_datetime(
                        end_value.get("dateTime") if isinstance(end_value, dict) else end_value,
                        "end_time",
                    ),
                )
                body["start"] = {"dateTime": start_time.isoformat()}
                body["end"] = {"dateTime": end_time.isoformat()}
            updated = self._execute(
                service.events().patch(
                    calendarId=calendar_id, eventId=gcal_event_id, body=body
                )
            )
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            raise CalendarError("Could not update the calendar event") from exc
        self._invalidate_cache()
        formatted = self._format_event(updated)
        formatted["calendar_id"] = calendar_id
        return formatted

    async def delete_event(self, gcal_event_id: str) -> None:
        """Delete an event from Kalendra only."""
        if not gcal_event_id:
            raise ValueError("gcal_event_id must be non-empty")
        service = self._get_service()
        if service is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )
        calendar_id = self._ensure_kalendra_calendar()
        try:
            current = self._execute(
                service.events().get(calendarId=calendar_id, eventId=gcal_event_id)
            )
            if not self._is_work_block(current):
                raise CalendarError("Refusing to delete an event not owned by Kalendra")
            self._execute(
                service.events().delete(
                    calendarId=calendar_id, eventId=gcal_event_id
                )
            )
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            raise CalendarError("Could not delete the calendar event") from exc
        self._invalidate_cache()

    async def clear_kalendra_range(self, start: datetime, end: datetime) -> None:
        """Delete every Kalendra block overlapping an aware half-open range."""
        start_utc, end_utc = _validate_range(start, end)
        service = self._get_service()
        if service is None:
            raise CalendarReconnectRequiredError(
                "Google Calendar is not connected; reconnect your calendar"
            )
        calendar_id = self._ensure_kalendra_calendar()
        page_token: str | None = None
        event_ids: list[str] = []
        try:
            while True:
                response = self._execute(
                    service.events().list(
                        calendarId=calendar_id,
                        timeMin=start_utc.isoformat(),
                        timeMax=end_utc.isoformat(),
                        singleEvents=True,
                        pageToken=page_token,
                    )
                )
                for event in response.get("items", []):
                    if event.get("id") and self._is_work_block(event):
                        event_ids.append(str(event["id"]))
                page_token = response.get("nextPageToken")
                if not page_token:
                    break
            for event_id in event_ids:
                self._execute(
                    service.events().delete(
                        calendarId=calendar_id, eventId=event_id
                    )
                )
        except HttpError as exc:
            self._raise_if_reconnect_required(exc)
            raise CalendarError("Could not clear the Kalendra calendar range") from exc
        self._invalidate_cache()

    async def create_work_block(
        self,
        task_id: int,
        title: str,
        start: datetime,
        end: datetime,
        reasoning: str | None = None,
    ) -> str:
        """Create a reasoned task block on the dedicated Kalendra calendar."""
        rationale = str(reasoning or "").strip()
        if not rationale:
            raise ValueError("reasoning must contain non-whitespace text")
        created = await self.create_event(
            {
                "title": title,
                "start_time": start,
                "end_time": end,
                "extendedProperties": {
                    "private": {"kalendra": "work-block", "task_id": str(task_id)}
                },
            },
            rationale,
        )
        return str(created["gcal_event_id"])

    async def update_work_block(
        self,
        gcal_event_id: str,
        title: str,
        start: datetime,
        end: datetime,
        reasoning: str | None = None,
    ) -> None:
        changes: CalendarRecord = {
            "title": title,
            "start_time": start,
            "end_time": end,
        }
        if reasoning is not None:
            changes["reasoning"] = reasoning
        await self.update_event(
            gcal_event_id,
            changes,
        )

    async def delete_work_block(self, gcal_event_id: str) -> None:
        await self.delete_event(gcal_event_id)

    def _format_event(self, event: CalendarRecord) -> CalendarRecord:
        """Normalize a raw Google event into the shared schedule shape."""
        start_time = _google_time_to_utc(event.get("start", {}))
        end_time = _google_time_to_utc(event.get("end", {}))
        if start_time is None:
            raise ValueError("Google event is missing a start date or dateTime")
        if end_time is None:
            end_time = (datetime.fromisoformat(start_time) + timedelta(hours=1)).isoformat()
        if datetime.fromisoformat(end_time) <= datetime.fromisoformat(start_time):
            raise ValueError("Google event end must be later than its start")
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
    """Authorize Google Calendar interactively with read/write scope."""
    credentials_file = credentials_path or config.GOOGLE_CALENDAR_CREDENTIALS_PATH
    token_file = token_path or config.GOOGLE_CALENDAR_TOKEN_PATH
    if not credentials_file.exists():
        logger.error("Google OAuth client credentials file does not exist: %s", credentials_file)
        return False
    try:
        flow = InstalledAppFlow.from_client_secrets_file(str(credentials_file), SCOPES)
        credentials = flow.run_local_server(port=0)
        _write_token_securely(token_file, credentials.to_json())
        return True
    except (OSError, ValueError, RefreshError):
        logger.exception("Google Calendar OAuth flow failed")
        return False


async def create_calendar_service() -> CalendarService:
    """Create the calendar service without making a network request."""
    return CalendarService()
