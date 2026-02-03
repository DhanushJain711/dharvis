"""Configuration management for Dharvis."""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()


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
