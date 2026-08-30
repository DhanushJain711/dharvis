"""Focused checks for webhook runtime configuration and SQLite durability."""

from __future__ import annotations

from dataclasses import replace
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import pytest

from src.config import config
from src.store import Store


def _config_paths_from_clean_process(environment: dict[str, str]) -> list[str]:
    code = (
        "from src.config import config; "
        "print(*[config.DATA_DIR, config.DATABASE_PATH, "
        "config.GOOGLE_CALENDAR_TOKEN_PATH, "
        "config.GOOGLE_CALENDAR_CREDENTIALS_PATH, "
        "config.APSCHEDULER_DATABASE_PATH], sep='\\n')"
    )
    project_root = Path(__file__).parents[1]
    isolated_environment = environment | {
        "PYTHONPATH": str(project_root)
        + (os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else "")
    }
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            cwd=directory,
            env=isolated_environment,
        )
    return result.stdout.splitlines()


def test_data_dir_derives_all_runtime_paths_and_explicit_paths_override(tmp_path) -> None:
    environment = os.environ.copy()
    environment.update({"DATA_DIR": str(tmp_path / "runtime")})
    for name in (
        "DATABASE_PATH",
        "GOOGLE_CALENDAR_TOKEN_PATH",
        "GOOGLE_CALENDAR_CREDENTIALS_PATH",
        "APSCHEDULER_DATABASE_PATH",
    ):
        environment.pop(name, None)

    data_dir = Path(environment["DATA_DIR"])
    assert _config_paths_from_clean_process(environment) == [
        str(data_dir),
        str(data_dir / "dharvis.db"),
        str(data_dir / "token.json"),
        str(data_dir / "credentials.json"),
        str(data_dir / "dharvis.db.jobs.sqlite"),
    ]

    explicit = environment | {
        "DATABASE_PATH": str(tmp_path / "custom.db"),
        "GOOGLE_CALENDAR_TOKEN_PATH": str(tmp_path / "custom-token.json"),
        "GOOGLE_CALENDAR_CREDENTIALS_PATH": str(tmp_path / "custom-credentials.json"),
        "APSCHEDULER_DATABASE_PATH": str(tmp_path / "custom-jobs.sqlite"),
    }
    assert _config_paths_from_clean_process(explicit) == [
        str(data_dir),
        str(tmp_path / "custom.db"),
        str(tmp_path / "custom-token.json"),
        str(tmp_path / "custom-credentials.json"),
        str(tmp_path / "custom-jobs.sqlite"),
    ]


def test_webhook_validation_requires_only_webhook_values_in_webhook_mode() -> None:
    polling = replace(
        config,
        TELEGRAM_BOT_TOKEN="token",
        ALLOWED_USER_ID=123,
        RUN_MODE="polling",
        PUBLIC_BASE_URL="",
        TELEGRAM_WEBHOOK_PATH="",
        TELEGRAM_WEBHOOK_SECRET="",
    )
    assert polling.validate() == []

    webhook = replace(polling, RUN_MODE="webhook")
    assert webhook.validate() == [
        "PUBLIC_BASE_URL",
        "TELEGRAM_WEBHOOK_PATH",
        "TELEGRAM_WEBHOOK_SECRET",
    ]
    assert replace(polling, RUN_MODE="worker").validate() == [
        "RUN_MODE must be 'polling' or 'webhook'"
    ]
    assert replace(
        polling,
        RUN_MODE="webhook",
        PUBLIC_BASE_URL="not-a-url",
        TELEGRAM_WEBHOOK_PATH="telegram",
        TELEGRAM_WEBHOOK_SECRET="secret",
    ).validate() == [
        "PUBLIC_BASE_URL must be an absolute HTTPS URL without a path, query, fragment, or params"
    ]

    for public_base_url in (
        "http://example.test",
        "https://example.test/callback",
        "https://example.test?query=value",
        "https://example.test#fragment",
        "https://example.test/;params",
    ):
        assert replace(
            polling,
            RUN_MODE="webhook",
            PUBLIC_BASE_URL=public_base_url,
            TELEGRAM_WEBHOOK_PATH="telegram",
            TELEGRAM_WEBHOOK_SECRET="secret",
        ).validate() == [
            "PUBLIC_BASE_URL must be an absolute HTTPS URL without a path, query, fragment, or params"
        ]

    assert replace(
        polling,
        RUN_MODE="webhook",
        PUBLIC_BASE_URL="https://example.test",
        TELEGRAM_WEBHOOK_PATH="telegram/path",
        TELEGRAM_WEBHOOK_SECRET="secret",
    ).validate() == ["TELEGRAM_WEBHOOK_PATH must be a URL-safe token"]
    assert replace(
        polling,
        RUN_MODE="webhook",
        PUBLIC_BASE_URL="https://example.test",
        TELEGRAM_WEBHOOK_PATH="telegram",
        TELEGRAM_WEBHOOK_SECRET="secret!",
    ).validate() == [
        "TELEGRAM_WEBHOOK_SECRET must be a URL-safe token of at most 256 characters"
    ]
    assert replace(
        polling,
        RUN_MODE="webhook",
        PUBLIC_BASE_URL="https://example.test",
        TELEGRAM_WEBHOOK_PATH="telegram",
        TELEGRAM_WEBHOOK_SECRET="a" * 257,
    ).validate() == [
        "TELEGRAM_WEBHOOK_SECRET must be a URL-safe token of at most 256 characters"
    ]


def test_webhook_config_normalizes_whitespace_and_slashes() -> None:
    code = (
        "from src.config import config; "
        "print(config.PUBLIC_BASE_URL, config.TELEGRAM_WEBHOOK_PATH, "
        "config.TELEGRAM_WEBHOOK_SECRET, sep='\\n')"
    )
    environment = os.environ | {
        "PUBLIC_BASE_URL": "  https://example.test///  ",
        "TELEGRAM_WEBHOOK_PATH": "  ///telegram  ",
        "TELEGRAM_WEBHOOK_SECRET": "  header-secret  ",
    }
    project_root = Path(__file__).parents[1]
    environment["PYTHONPATH"] = str(project_root) + (
        os.pathsep + environment["PYTHONPATH"] if environment.get("PYTHONPATH") else ""
    )
    with tempfile.TemporaryDirectory() as directory:
        result = subprocess.run(
            [sys.executable, "-c", code],
            check=True,
            capture_output=True,
            text=True,
            cwd=directory,
            env=environment,
        )
    assert result.stdout.splitlines() == [
        "https://example.test",
        "telegram",
        "header-secret",
    ]


@pytest.mark.asyncio
async def test_store_creates_parent_and_applies_durable_pragmas(tmp_path) -> None:
    path = tmp_path / "nested" / "runtime" / "dharvis.db"
    store = Store(path)
    await store.initialize()

    assert path.parent.is_dir()
    async with store.connection() as db:
        assert (await (await db.execute("PRAGMA journal_mode")).fetchone())[0] == "delete"
        assert (await (await db.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000
        assert (await (await db.execute("PRAGMA synchronous")).fetchone())[0] == 2
        assert (await (await db.execute("PRAGMA foreign_keys")).fetchone())[0] == 1


@pytest.mark.asyncio
async def test_memory_keeper_applies_durable_pragmas() -> None:
    store = Store(":memory:")
    await store.initialize()
    try:
        assert store._keeper is not None
        assert (await (await store._keeper.execute("PRAGMA busy_timeout")).fetchone())[0] == 5000
        assert (await (await store._keeper.execute("PRAGMA synchronous")).fetchone())[0] == 2
        assert (await (await store._keeper.execute("PRAGMA foreign_keys")).fetchone())[0] == 1
    finally:
        if store._keeper is not None:
            await store._keeper.close()
