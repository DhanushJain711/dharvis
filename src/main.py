"""Production entrypoint for the integrated Dharvis Telegram assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from .agent import Agent
from .calendar_service import CalendarService
from .config import config
from .facts_engine import FactsEngine
from .history import History
from .integration import build_tool_handlers
from .jobs import (
    assert_jobs_ready,
    configure_jobs,
    create_job_scheduler,
    shutdown_job_scheduler,
    start_job_scheduler,
)
from .logging_config import configure_logging
from .scheduler_engine import SchedulerEngine
from .store import Store
from .telegram_handler import TelegramHandler

configure_logging()
logger = logging.getLogger(__name__)


async def build_application() -> tuple[TelegramHandler, object]:
    """Initialize persistence and build, but do not start, the Telegram app."""
    store = Store()
    await store.initialize()
    calendar = CalendarService()
    scheduler_engine = SchedulerEngine(store, calendar)
    facts_engine = FactsEngine(store)
    tool_handlers = await build_tool_handlers(
        store, calendar, scheduler_engine, facts_engine
    )
    agent = Agent(History(store), tool_handlers=tool_handlers)
    agent.facts_engine = facts_engine
    handler = TelegramHandler(
        agent, store=store, calendar_service=calendar
    )
    job_scheduler = create_job_scheduler()
    configure_jobs(
        job_scheduler, store, scheduler_engine, handler, facts_engine
    )
    assert_jobs_ready()
    handler.job_scheduler = job_scheduler
    handler.scheduler_engine = scheduler_engine
    handler.facts_engine = facts_engine
    return handler, handler.create_application()


async def _health_server(handler: TelegramHandler) -> asyncio.AbstractServer | None:
    """Serve Railway's dependency-aware health check without another framework."""
    if config.HEALTH_PORT <= 0:
        return None

    async def respond(
        reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        status, payload = 200, {"status": "ok"}
        try:
            request = await asyncio.wait_for(reader.readline(), timeout=2)
            path = request.decode("ascii", "ignore").split(" ")[1]
            if path != "/healthz":
                status, payload = 404, {"status": "not_found"}
            else:
                async with handler.store.connection() as db:
                    await asyncio.wait_for(db.execute("SELECT 1"), timeout=2)
                assert_jobs_ready()
                if not getattr(handler.job_scheduler, "running", False):
                    raise RuntimeError("job scheduler is not running")
                payload["jobs_running"] = True
        except Exception as exc:
            status, payload = 503, {
                "status": "unhealthy", "dependency": type(exc).__name__
            }
        body = json.dumps(payload, separators=(",", ":")).encode()
        reason = "OK" if status == 200 else "Service Unavailable" if status == 503 else "Not Found"
        writer.write(
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
            + body
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    return await asyncio.start_server(respond, "0.0.0.0", config.HEALTH_PORT)


async def main(*, check_only: bool = False) -> None:
    """Run Telegram polling, proactive jobs, health checks, and clean shutdown."""
    missing = config.validate()
    if missing:
        logger.error(
            "Refusing to start: missing required configuration: %s",
            ", ".join(missing),
        )
        return
    handler, app = await build_application()
    if check_only:
        logger.info("Configuration, schema, tools, jobs, and Telegram application are valid")
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            pass

    initialized = started = polling = jobs_started = False
    health_server: asyncio.AbstractServer | None = None
    try:
        await app.initialize()
        initialized = True
        await app.start()
        started = True
        if app.updater is None:
            raise RuntimeError("Telegram updater is unavailable")
        await app.updater.start_polling(
            drop_pending_updates=True,
            timeout=config.TELEGRAM_POLL_TIMEOUT_SECONDS,
        )
        polling = True
        start_job_scheduler(handler.job_scheduler)
        jobs_started = True
        health_server = await _health_server(handler)
        logger.info("Dharvis is running", extra={"event": "application_started"})
        await stop_event.wait()
    finally:
        logger.info("Stopping Dharvis")
        if health_server is not None:
            health_server.close()
            await health_server.wait_closed()
        if jobs_started:
            shutdown_job_scheduler(handler.job_scheduler, wait=False)
        if polling and app.updater is not None:
            await app.updater.stop()
        if started:
            await app.stop()
        if initialized:
            await app.shutdown()


def run() -> None:
    """Console-script entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check", action="store_true", help="build dependencies and exit before polling"
    )
    args = parser.parse_args()
    missing = config.validate()
    if missing:
        logger.error(
            "Refusing to start: missing required configuration: %s",
            ", ".join(missing),
        )
        raise SystemExit(2)
    try:
        asyncio.run(main(check_only=args.check))
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    run()
