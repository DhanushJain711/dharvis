"""Generate independent Telegram webhook path and secret values for Azure."""

from __future__ import annotations

import secrets


def generate_webhook_settings() -> tuple[str, str]:
    """Return URL-safe path and header-secret values with sufficient entropy."""
    return secrets.token_urlsafe(24), secrets.token_urlsafe(32)


def main() -> None:
    path, secret = generate_webhook_settings()
    print(
        "az webapp config appsettings set "
        "--name <APP_NAME> --resource-group <RESOURCE_GROUP> --settings "
        f"TELEGRAM_WEBHOOK_PATH={path} TELEGRAM_WEBHOOK_SECRET={secret}"
    )


if __name__ == "__main__":
    main()
