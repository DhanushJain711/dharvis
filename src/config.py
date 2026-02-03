"""Configuration management for Dharvis."""

import base64
import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


def _setup_google_token():
    """Decode base64 Google Calendar token from env var if present."""
    token_base64 = os.getenv("GOOGLE_CALENDAR_TOKEN_BASE64")
    if token_base64:
        token_path = Path(os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json"))
        try:
            token_data = base64.b64decode(token_base64)
            token_path.write_bytes(token_data)
        except Exception as e:
            print(f"Warning: Failed to decode GOOGLE_CALENDAR_TOKEN_BASE64: {e}")


# Setup Google token from base64 env var if present
_setup_google_token()


class Config:
    """Application configuration loaded from environment variables."""

    # Telegram settings
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    ALLOWED_USER_ID: int | None = (
        int(os.getenv("ALLOWED_USER_ID")) if os.getenv("ALLOWED_USER_ID") else None
    )

    # Anthropic settings
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

    # Google Calendar settings
    GOOGLE_CALENDAR_CREDENTIALS_PATH: Path = Path(
        os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "./credentials.json")
    )
    GOOGLE_CALENDAR_TOKEN_PATH: Path = Path(
        os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json")
    )

    # Database settings
    DATABASE_PATH: Path = Path(os.getenv("DATABASE_PATH", "./dharvis.db"))

    # Timezone settings
    USER_TIMEZONE: str = os.getenv("USER_TIMEZONE", "America/Chicago")

    @classmethod
    def validate(cls) -> list[str]:
        """Validate that required configuration is present.

        Returns:
            List of missing configuration keys.
        """
        missing = []
        if not cls.TELEGRAM_BOT_TOKEN:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not cls.ANTHROPIC_API_KEY:
            missing.append("ANTHROPIC_API_KEY")
        return missing


config = Config()
