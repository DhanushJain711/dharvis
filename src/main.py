"""Production entrypoint for the integrated Dharvis Telegram assistant."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from .config import config
from .jobs import (
    _prepare_scheduler,
    assert_jobs_ready,
    start_scheduler,
    stop_scheduler,
)
from .logging_config import configure_logging
from .telegram_handler import (
    TelegramHandler,
    build_application as build_telegram_application,
    initialize_application_runtime,
)

configure_logging()
# httpx includes full request URLs at INFO level; Telegram bot-token URLs must
# never reach process logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


async def build_application() -> tuple[TelegramHandler, object]:
    """Compatibility async wrapper around the synchronous application factory."""
    application = build_telegram_application()
    await initialize_application_runtime(application)
    return application.bot_data["telegram_handler"], application


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
        _prepare_scheduler(app)
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
        start_scheduler(app)
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
            stop_scheduler()
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
    if not args.check and config.RUN_MODE == "webhook":
        logger.error(
            "Webhook mode must be launched by Uvicorn: uvicorn src.web:app"
        )
        raise SystemExit(1)
    try:
        asyncio.run(main(check_only=args.check))
    except KeyboardInterrupt:
        logger.info("Interrupted")


if __name__ == "__main__":
    run()
