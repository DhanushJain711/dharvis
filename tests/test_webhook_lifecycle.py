"""Offline lifecycle seams for the webhook transport."""

from __future__ import annotations

import sqlite3
import importlib
import sys
from dataclasses import replace
from datetime import date
from types import SimpleNamespace

import pytest

from src import jobs
from src.config import config


def test_build_application_is_sync_and_attaches_runtime(monkeypatch) -> None:
    from src import telegram_handler

    monkeypatch.setattr(
        telegram_handler, "config", replace(config, TELEGRAM_BOT_TOKEN="123:test")
    )
    application = telegram_handler.build_application()

    assert application.bot_data["runtime_initialized"] is False
    assert {"store", "calendar", "scheduler_engine", "facts_engine", "telegram_handler"} <= set(
        application.bot_data
    )
    assert len(application.handlers) == 1


@pytest.mark.asyncio
async def test_runtime_initialization_is_once(monkeypatch) -> None:
    from src import telegram_handler

    calls: list[str] = []

    class Store:
        async def initialize(self) -> None:
            calls.append("store")

    async def bindings(*_args):
        calls.append("bindings")
        return {"add_task": object()}

    monkeypatch.setattr("src.integration.build_tool_handlers", bindings)
    app = SimpleNamespace(
        bot_data={
            "store": Store(),
            "calendar": object(),
            "scheduler_engine": object(),
            "facts_engine": object(),
            "telegram_handler": SimpleNamespace(agent=SimpleNamespace(tool_handlers={})),
            "runtime_initialized": False,
        }
    )

    await telegram_handler.initialize_application_runtime(app)
    await telegram_handler.initialize_application_runtime(app)

    assert calls == ["store", "bindings"]
    assert app.bot_data["runtime_initialized"] is True


def test_start_and_stop_scheduler_use_application_runtime(monkeypatch) -> None:
    events: list[object] = []

    class Scheduler:
        running = False

        def start(self) -> None:
            events.append("start")
            self.running = True

        def shutdown(self, *, wait: bool) -> None:
            events.append(("stop", wait))
            self.running = False

    scheduler = Scheduler()
    handler = SimpleNamespace()
    app = SimpleNamespace(bot_data={
        "store": object(), "scheduler_engine": object(), "facts_engine": object(),
        "telegram_handler": handler,
    })
    monkeypatch.setattr(jobs, "create_job_scheduler", lambda: scheduler)
    monkeypatch.setattr(jobs, "configure_jobs", lambda *args: events.append("configure"))
    monkeypatch.setattr(jobs, "assert_jobs_ready", lambda: events.append("ready"))
    monkeypatch.setattr(jobs, "start_job_scheduler", lambda value: value.start())
    monkeypatch.setattr(jobs, "shutdown_job_scheduler", lambda value, wait: value.shutdown(wait=wait))
    jobs._managed_scheduler = None

    jobs.start_scheduler(app)
    jobs.start_scheduler(app)
    jobs.stop_scheduler()

    assert events == ["configure", "ready", "start", ("stop", True)]
    assert handler.job_scheduler is scheduler


def test_running_managed_scheduler_blocks_a_second_application(monkeypatch) -> None:
    class RunningScheduler:
        running = True

    existing = RunningScheduler()
    jobs._managed_scheduler = existing
    monkeypatch.setattr(jobs, "_prepare_scheduler", lambda _app: (_ for _ in ()).throw(AssertionError()))

    jobs.start_scheduler(SimpleNamespace(bot_data={}))

    assert jobs._managed_scheduler is existing
    jobs._managed_scheduler = None


def test_scheduler_second_application_does_not_start_or_prepare(monkeypatch) -> None:
    """One process must not accidentally run proactive jobs twice."""
    calls: list[object] = []

    class Scheduler:
        running = False

        def start(self) -> None:
            calls.append("start")
            self.running = True

        def shutdown(self, *, wait: bool) -> None:
            calls.append(("shutdown", wait))
            self.running = False

    first = Scheduler()
    first_app = SimpleNamespace(bot_data={})
    second_app = SimpleNamespace(bot_data={})
    monkeypatch.setattr(jobs, "_prepare_scheduler", lambda app: first if app is first_app else (_ for _ in ()).throw(AssertionError("second app prepared")))
    monkeypatch.setattr(jobs, "start_job_scheduler", lambda scheduler: scheduler.start())
    monkeypatch.setattr(jobs, "shutdown_job_scheduler", lambda scheduler, wait: scheduler.shutdown(wait=wait))
    jobs._managed_scheduler = None

    jobs.start_scheduler(first_app)
    jobs.start_scheduler(second_app)
    jobs.stop_scheduler()

    assert calls == ["start", ("shutdown", True)]
    assert jobs._managed_scheduler is None


@pytest.mark.asyncio
async def test_nightly_facts_uses_persisted_day_evidence(monkeypatch) -> None:
    local_date = date(2026, 8, 30)
    calls: list[object] = []
    daily_log = {"date": local_date.isoformat(), "notes": ""}

    class Store:
        async def get_daily_log(self, value):
            calls.append(("log", value))
            return daily_log

        async def upsert_daily_log(self, value, changes):
            calls.append(("upsert", value, changes))
            daily_log.update(changes)
            return daily_log

        async def get_messages_between(self, start, end):
            calls.append(("messages", start, end))
            return [{"content": "message"}]

        async def get_schedule_decisions_between(self, start, end):
            calls.append(("decisions", start, end))
            return [{"reasoning": "reason"}]

    class Facts:
        async def extract_from_day(self, *, daily_log, conversation, decisions):
            calls.append(("extract", daily_log, conversation, decisions))

    monkeypatch.setattr(jobs.timeutil, "now_local", lambda: SimpleNamespace(date=lambda: local_date))
    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=Store(), facts_engine=Facts()))

    await jobs._scheduled_nightly_facts()
    await jobs._scheduled_nightly_facts()

    assert calls[0] == ("log", local_date)
    assert [call[0] for call in calls].count("extract") == 1
    assert [call for call in calls if call[0] == "upsert"] == [
        ("upsert", local_date, {"notes": "[nightly-facts:2026-08-30]"})
    ]


@pytest.mark.asyncio
async def test_nightly_facts_skips_processed_debrief(monkeypatch) -> None:
    extracted = False

    class Store:
        async def get_daily_log(self, _value):
            return {"notes": "[debrief-checklist:completed]"}

    class Facts:
        async def extract_from_day(self, **_kwargs):
            nonlocal extracted
            extracted = True

    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=Store(), facts_engine=Facts()))

    await jobs._scheduled_nightly_facts()

    assert extracted is False


@pytest.mark.asyncio
async def test_nightly_facts_waits_for_a_sent_debrief(monkeypatch) -> None:
    extracted = False

    class Store:
        async def get_daily_log(self, _value):
            return {"debrief_sent_at": object(), "notes": ""}

    class Facts:
        async def extract_from_day(self, **_kwargs):
            nonlocal extracted
            extracted = True

    monkeypatch.setattr(jobs, "_runtime", SimpleNamespace(store=Store(), facts_engine=Facts()))

    await jobs._scheduled_nightly_facts()

    assert extracted is False


@pytest.mark.asyncio
async def test_late_debrief_persists_outcomes_without_repeating_nightly_evidence(
    tmp_path, monkeypatch,
) -> None:
    from src.store import Store
    from src import timeutil

    store = Store(tmp_path / "late-debrief.sqlite")
    await store.initialize()
    local_date = timeutil.now_local().date()
    task = (await store.add_tasks([{"title": "late completion"}]))[0]
    await store.upsert_daily_log(local_date, {
        "planned": [{"task_id": task["id"], "checklist_included": True}],
        "notes": f"[nightly-facts:{local_date.isoformat()}]",
    })
    calls: list[object] = []

    class Facts:
        async def extract_from_day(self, **_kwargs):
            calls.append("extract")

    monkeypatch.setattr(jobs, "_runtime", None)
    await jobs.handle_debrief_submission(
        store, Facts(), object(), {
            "callback_prefix": f"daily-debrief:{local_date.isoformat()}",
            "checklist_id": "late", "items": [{"checked": True, "value": {
                "task_id": task["id"], "actual_minutes": 20,
            }}],
        },
    )

    persisted = await store.get_task(task["id"])
    log = await store.get_daily_log(local_date)
    assert calls == []
    assert persisted["status"] == "completed"
    assert "[debrief-checklist:late]" in log["notes"]


def test_sqlite_backup_failure_preserves_existing_daily_snapshot(tmp_path) -> None:
    source = tmp_path  # SQLite rejects a directory before the temp snapshot publishes.
    destination = tmp_path / "backups" / "agenda-2026-08-30.db"
    destination.parent.mkdir()
    destination.write_bytes(b"previous snapshot")

    with pytest.raises(sqlite3.DatabaseError):
        jobs._write_sqlite_backup(source, destination)

    assert destination.read_bytes() == b"previous snapshot"
    assert not list(destination.parent.glob("*.tmp"))


@pytest.mark.asyncio
async def test_webhook_route_checks_exact_path_secret_and_queues(monkeypatch, caplog) -> None:
    pytest.importorskip("fastapi")
    import src.config as config_module
    import src.telegram_handler as telegram_handler

    queued: list[object] = []

    class Queue:
        async def put(self, value):
            queued.append(value)

    telegram_app = SimpleNamespace(bot=object(), update_queue=Queue(), bot_data={})
    valid = replace(
        config, TELEGRAM_BOT_TOKEN="123:test", ALLOWED_USER_ID=1,
        RUN_MODE="webhook", PUBLIC_BASE_URL="https://example.test",
        TELEGRAM_WEBHOOK_PATH="internal", TELEGRAM_WEBHOOK_SECRET="secret",
    )
    monkeypatch.setattr(config_module, "config", valid)
    monkeypatch.setattr(telegram_handler, "build_application", lambda: telegram_app)
    sys.modules.pop("src.web", None)
    web = importlib.import_module("src.web")
    monkeypatch.setattr(web.Update, "de_json", lambda payload, bot: (payload, bot))

    class Request:
        headers = {"X-Telegram-Bot-Api-Secret-Token": "secret"}

        async def json(self):
            return {"update_id": 1}

    response = await web.telegram_webhook("internal", Request())

    assert response.status_code == 200
    assert queued == [({"update_id": 1}, telegram_app.bot)]
    assert await web.healthz() == {"ok": True}
    assert web.application is telegram_app
    assert web.app is not telegram_app

    class BadRequest(Request):
        headers = {"X-Telegram-Bot-Api-Secret-Token": "wrong"}

    with pytest.raises(web.HTTPException) as rejected:
        await web.telegram_webhook("internal", BadRequest())
    assert rejected.value.status_code == 403
    assert "invalid secret" in caplog.text

    with pytest.raises(web.HTTPException) as missing:
        await web.telegram_webhook("not-internal", Request())
    assert missing.value.status_code == 404

    events: list[object] = []

    async def initialize(_application):
        events.append("runtime")

    async def lifecycle(name):
        events.append(name)

    class Bot:
        async def set_webhook(self, **kwargs):
            events.append(("webhook", kwargs))

    telegram_app.bot = Bot()
    telegram_app.initialize = lambda: lifecycle("initialize")
    telegram_app.start = lambda: lifecycle("start")
    telegram_app.stop = lambda: lifecycle("stop")
    telegram_app.shutdown = lambda: lifecycle("shutdown")
    monkeypatch.setattr(web, "initialize_application_runtime", initialize)
    monkeypatch.setattr(web, "start_scheduler", lambda _app: events.append("scheduler"))
    monkeypatch.setattr(web, "stop_scheduler", lambda: events.append("stop-scheduler"))

    async with web.lifespan(web.app):
        pass

    assert events[:4] == ["runtime", "initialize", "start", (
        "webhook", {
            "url": "https://example.test/internal", "secret_token": "secret",
            "allowed_updates": web.Update.ALL_TYPES, "drop_pending_updates": False,
        },
    )]
    assert events[-3:] == ["stop-scheduler", "stop", "shutdown"]


def test_web_module_fails_before_build_for_invalid_webhook_config(monkeypatch) -> None:
    pytest.importorskip("fastapi")
    import src.config as config_module
    import src.telegram_handler as telegram_handler

    invalid = replace(
        config, TELEGRAM_BOT_TOKEN="123:test", ALLOWED_USER_ID=1,
        RUN_MODE="webhook", PUBLIC_BASE_URL="", TELEGRAM_WEBHOOK_PATH="",
        TELEGRAM_WEBHOOK_SECRET="",
    )
    monkeypatch.setattr(config_module, "config", invalid)
    monkeypatch.setattr(
        telegram_handler, "build_application",
        lambda: (_ for _ in ()).throw(AssertionError("must not build")),
    )
    sys.modules.pop("src.web", None)

    with pytest.raises(RuntimeError, match="PUBLIC_BASE_URL.*TELEGRAM_WEBHOOK_PATH"):
        importlib.import_module("src.web")


def test_factory_registers_handlers_without_starting_application(monkeypatch) -> None:
    """Import-safe composition must leave PTB lifecycle work to its caller."""
    from src import telegram_handler

    monkeypatch.setattr(
        telegram_handler, "config", replace(config, TELEGRAM_BOT_TOKEN="123:test")
    )

    # Use the normal factory to preserve coverage of PTB's real handler wiring;
    # no lifecycle method is invoked by it.
    application = telegram_handler.build_application()
    assert application.bot_data["runtime_initialized"] is False
    assert application.running is False


def test_main_cli_rejects_webhook_mode_without_running_polling(monkeypatch) -> None:
    from src import main as main_module

    valid_webhook = replace(
        config,
        TELEGRAM_BOT_TOKEN="123:test",
        ALLOWED_USER_ID=1,
        RUN_MODE="webhook",
        PUBLIC_BASE_URL="https://example.test",
        TELEGRAM_WEBHOOK_PATH="internal",
        TELEGRAM_WEBHOOK_SECRET="secret",
    )
    monkeypatch.setattr(main_module, "config", valid_webhook)
    monkeypatch.setattr(
        main_module.argparse.ArgumentParser,
        "parse_args",
        lambda _self: SimpleNamespace(check=False),
    )
    monkeypatch.setattr(
        main_module.asyncio,
        "run",
        lambda _coroutine: (_ for _ in ()).throw(AssertionError("polling started")),
    )

    with pytest.raises(SystemExit) as exited:
        main_module.run()

    assert exited.value.code == 1


def test_sqlite_backup_prunes_only_expired_agenda_files(tmp_path) -> None:
    source = tmp_path / "source.db"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE entries (value TEXT)")
        connection.execute("INSERT INTO entries VALUES ('safe')")
    backup_dir = tmp_path / "backups"
    destination = backup_dir / "agenda-2026-08-30.db"

    jobs._write_sqlite_backup(source, destination)
    (backup_dir / "agenda-2026-08-15.db").touch()
    retained = backup_dir / "agenda-2026-08-16.db"
    retained.touch()
    unrelated = backup_dir / "other.db"
    unrelated.touch()
    jobs._prune_agenda_backups(backup_dir, date(2026, 8, 30))

    with sqlite3.connect(destination) as connection:
        assert connection.execute("SELECT value FROM entries").fetchone()[0] == "safe"
    assert not (backup_dir / "agenda-2026-08-15.db").exists()
    assert retained.exists()
    assert unrelated.exists()
