"""Fail-closed configuration and Telegram authorization checks."""

from dataclasses import replace

from src.config import config
from src.telegram_handler import TelegramHandler


def test_config_requires_token_and_allowed_user() -> None:
    missing = replace(config, TELEGRAM_BOT_TOKEN="", ALLOWED_USER_ID=None)
    assert missing.validate() == ["TELEGRAM_BOT_TOKEN", "ALLOWED_USER_ID"]


def test_telegram_authorization_fails_closed(monkeypatch) -> None:
    monkeypatch.setattr("src.telegram_handler.config", replace(config, ALLOWED_USER_ID=None))
    assert TelegramHandler().is_authorized(123) is False


def test_telegram_authorizes_only_configured_user(monkeypatch) -> None:
    monkeypatch.setattr("src.telegram_handler.config", replace(config, ALLOWED_USER_ID=123))
    handler = TelegramHandler()
    assert handler.is_authorized(123) is True
    assert handler.is_authorized(456) is False
