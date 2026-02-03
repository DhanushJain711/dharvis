"""Main entry point for Dharvis - the Claude-powered agenda bot."""

import asyncio
import logging
import signal
import sys

from config import config
from database import Database
from claude_agent import ClaudeAgent
from calendar_service import CalendarService
from telegram_handler import TelegramHandler

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Initialize and run the Dharvis bot."""
    # Validate configuration
    missing = config.validate()
    if missing:
        logger.error(f"Missing required configuration: {', '.join(missing)}")
        logger.error("Please set these in your .env file")
        sys.exit(1)

    logger.info("Initializing Dharvis...")

    # Initialize database
    db = Database()
    await db.init_db()
    logger.info("Database initialized")

    # Initialize Claude agent
    agent = ClaudeAgent()
    logger.info("Claude agent initialized")

    # Initialize Google Calendar (optional - may fail gracefully)
    calendar = CalendarService()
    if calendar.is_available():
        logger.info("Google Calendar connected")
    else:
        logger.warning(
            "Google Calendar not available - run setup_gcal_auth.py to configure"
        )
        calendar = None

    # Initialize Telegram handler
    handler = TelegramHandler(
        database=db,
        claude_agent=agent,
        calendar_service=calendar,
    )

    # Create application
    app = handler.create_application()

    # Set up signal handlers for graceful shutdown
    stop_event = asyncio.Event()

    def signal_handler(sig, frame):
        logger.info("Received shutdown signal")
        stop_event.set()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start bot
    logger.info("Starting Dharvis bot...")
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)

    logger.info("Dharvis is running! Press Ctrl+C to stop.")

    # Wait for stop signal
    await stop_event.wait()

    # Graceful shutdown
    logger.info("Shutting down...")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()
    logger.info("Dharvis stopped.")


def run() -> None:
    """Entry point for running the bot."""
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Interrupted by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        sys.exit(1)


if __name__ == "__main__":
    run()
