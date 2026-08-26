"""Centralized environment configuration for every Dharvis component."""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from dotenv import load_dotenv
from pydantic import TypeAdapter, ValidationError

load_dotenv()

ReasoningVerbosity = Literal["brief", "full"]


def _optional_int(name: str) -> int | None:
    value = os.getenv(name)
    if not value:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _reasoning_verbosity() -> ReasoningVerbosity:
    value = os.getenv("REASONING_VERBOSITY", "brief").lower()
    try:
        return TypeAdapter(ReasoningVerbosity).validate_python(value)
    except ValidationError as exc:
        raise ValueError("REASONING_VERBOSITY must be 'brief' or 'full'") from exc


def _materialize_google_token() -> None:
    """Write a base64 deployment token only when one is explicitly supplied."""
    encoded = os.getenv("GOOGLE_CALENDAR_TOKEN_BASE64")
    if not encoded:
        return
    path = Path(os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json"))
    if path.exists():
        path.chmod(0o600)
        return
    try:
        token_data = base64.b64decode(encoded, validate=True)
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as token_file:
                descriptor = -1
                token_file.write(token_data)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (ValueError, OSError) as exc:
        raise ValueError("GOOGLE_CALENDAR_TOKEN_BASE64 is invalid or cannot be written") from exc


@dataclass(frozen=True, slots=True)
class Config:
    """Immutable settings loaded once from environment variables."""

    # Telegram
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ALLOWED_USER_ID: int | None = _optional_int("ALLOWED_USER_ID")
    TELEGRAM_POLL_TIMEOUT_SECONDS: int = int(
        os.getenv("TELEGRAM_POLL_TIMEOUT_SECONDS", "30")
    )

    # OpenAI models
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    AGENT_MODEL_ID: str = os.getenv("AGENT_MODEL_ID", "gpt-5-mini")
    SCHEDULER_MODEL_ID: str = os.getenv("SCHEDULER_MODEL_ID", "gpt-5-mini")
    FACTS_MODEL_ID: str = os.getenv("FACTS_MODEL_ID", "gpt-5-mini")
    OPENAI_REASONING_EFFORT: str = os.getenv("OPENAI_REASONING_EFFORT", "medium")

    # Legacy compatibility while the Anthropic classifier is retired.
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Local time policy. Clock strings use 24-hour HH:MM format.
    USER_TIMEZONE: str = os.getenv("USER_TIMEZONE", "America/Chicago")
    QUIET_HOURS_START: str = os.getenv("QUIET_HOURS_START", "22:00")
    QUIET_HOURS_END: str = os.getenv("QUIET_HOURS_END", "07:00")
    DAILY_BRIEF_TIME: str = os.getenv("DAILY_BRIEF_TIME", "07:30")
    DAILY_DEBRIEF_TIME: str = os.getenv("DAILY_DEBRIEF_TIME", "20:30")
    REASONING_VERBOSITY: ReasoningVerbosity = _reasoning_verbosity()

    # Google Calendar
    GOOGLE_CALENDAR_CREDENTIALS_PATH: Path = Path(
        os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "./credentials.json")
    )
    GOOGLE_CALENDAR_TOKEN_PATH: Path = Path(
        os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json")
    )
    GOOGLE_CALENDAR_TOKEN_BASE64: str = os.getenv(
        "GOOGLE_CALENDAR_TOKEN_BASE64", ""
    )
    GOOGLE_CALENDAR_ID: str = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    KALENDRA_CALENDAR_NAME: str = os.getenv(
        "KALENDRA_CALENDAR_NAME", "Kalendra"
    )
    KALENDRA_CALENDAR_ID: str | None = os.getenv("KALENDRA_CALENDAR_ID") or None

    # Persistence and scheduling
    DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", "./dharvis.db"))
    MESSAGE_HISTORY_LIMIT: int = int(os.getenv("MESSAGE_HISTORY_LIMIT", "100"))
    DEFAULT_TASK_MINUTES: int = int(os.getenv("DEFAULT_TASK_MINUTES", "30"))
    SCHEDULER_LOOKAHEAD_DAYS: int = int(
        os.getenv("SCHEDULER_LOOKAHEAD_DAYS", "14")
    )

    def validate(self) -> list[str]:
        """Return missing values required for the production Telegram process."""
        missing: list[str] = []
        if not self.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if self.ALLOWED_USER_ID is None:
            missing.append("ALLOWED_USER_ID")
        return missing


_materialize_google_token()
config = Config()
