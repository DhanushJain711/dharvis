"""Package entrypoint for the runnable Telegram placeholder."""

from __future__ import annotations

import argparse
import asyncio
import logging
import signal
import sys

from .agent import Agent
from .config import config
from .history import History
from .store import Store
from .telegram_handler import TelegramHandler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)


async def build_application() -> tuple[TelegramHandler, object]:
    """Initialize persistence and build, but do not start, the Telegram app."""
    store = Store()
    await store.initialize()
    agent = Agent(History(store))
    handler = TelegramHandler(agent, store=store)
    return handler, handler.create_application()


async def main(*, check_only: bool = False) -> None:
    """Connect polling, serve placeholder replies, and shut down cleanly."""
    missing = config.validate()
    if missing:
        logger.error(
            "Refusing to start: missing required configuration: %s",
            ", ".join(missing),
        )
        return
    handler, app = await build_application()
    del handler
    if check_only:
        logger.info("Configuration, schema, and Telegram application are valid")
        return

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signame in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signame, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            pass

    initialized = started = polling = False
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
        logger.info("Dharvis Telegram placeholder is running")
        await stop_event.wait()
    finally:
        logger.info("Stopping Dharvis")
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
