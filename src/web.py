"""ASGI ingress for Telegram webhooks.

The module factory is intentionally side-effect free: importing it builds the
Telegram handler graph, while FastAPI's lifespan owns all network and
background-process startup/shutdown.
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, Response
from telegram import Update

from .config import config
from .jobs import start_scheduler, stop_scheduler
from .telegram_handler import build_application, initialize_application_runtime

LOGGER = logging.getLogger(__name__)
# httpx INFO records include request URLs, including Telegram bot credentials.
logging.getLogger("httpx").setLevel(logging.WARNING)

_configuration_errors = config.validate()
if _configuration_errors:
    raise RuntimeError(
        "Invalid Telegram webhook configuration: " + ", ".join(_configuration_errors)
    )

# This exact export is the PTB application used by the webhook worker.
application = build_application()


def _webhook_url() -> str:
    return (
        f"{config.PUBLIC_BASE_URL.rstrip('/')}/"
        f"{config.TELEGRAM_WEBHOOK_PATH.lstrip('/')}"
    )


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Start Telegram's update worker and jobs only for a serving ASGI app."""
    initialized = started = scheduler_started = False
    try:
        await initialize_application_runtime(application)
        await application.initialize()
        initialized = True
        await application.start()
        started = True
        await application.bot.set_webhook(
            url=_webhook_url(),
            secret_token=config.TELEGRAM_WEBHOOK_SECRET,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
        start_scheduler(application)
        scheduler_started = True
        LOGGER.info("Telegram webhook application started")
        yield
    finally:
        if scheduler_started:
            stop_scheduler()
        if started:
            await application.stop()
        if initialized:
            await application.shutdown()


app = FastAPI(lifespan=lifespan)


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    """A lightweight platform liveness probe."""
    return {"ok": True}


@app.post("/{path:path}")
async def telegram_webhook(path: str, request: Request) -> Response:
    """Authenticate and enqueue Telegram updates without doing work inline."""
    if path != config.TELEGRAM_WEBHOOK_PATH:
        raise HTTPException(status_code=404, detail="not found")
    supplied_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied_secret, config.TELEGRAM_WEBHOOK_SECRET):
        LOGGER.warning("Rejected Telegram webhook with invalid secret")
        raise HTTPException(status_code=403, detail="forbidden")
    try:
        payload = await request.json()
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid JSON") from exc
    try:
        update = Update.de_json(payload, application.bot)
    except Exception as exc:
        raise HTTPException(status_code=400, detail="invalid Telegram update") from exc
    await application.update_queue.put(update)
    return Response(status_code=200)
