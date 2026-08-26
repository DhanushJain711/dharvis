"""Runnable Telegram transport with placeholder assistant behavior."""

from __future__ import annotations

from typing import Any

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from .agent import Agent
from .config import config

PLACEHOLDER_REPLY = "Dharvis is online. Agentic planning is being connected now."


class TelegramHandler:
    """Authorize Telegram updates and forward text to the stateful agent."""

    def __init__(
        self,
        agent: Agent | Any | None = None,
        *,
        store: Any | None = None,
        database: Any | None = None,
        claude_agent: Any | None = None,
        calendar_service: Any | None = None,
    ) -> None:
        # Keyword aliases preserve imports during the parallel migration.
        self.agent = agent or claude_agent or Agent()
        self.store = store or database
        self.calendar = calendar_service
        self.app: Application | None = None

    def is_authorized(self, user_id: int) -> bool:
        """Return whether a Telegram user may access this personal bot."""
        return config.ALLOWED_USER_ID is not None and user_id == config.ALLOWED_USER_ID

    def _is_authorized(self, user_id: int) -> bool:
        """Compatibility alias for the legacy handler."""
        return self.is_authorized(user_id)

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Acknowledge that the placeholder transport is online."""
        del context
        if update.effective_user and update.message and self.is_authorized(update.effective_user.id):
            await update.message.reply_text(PLACEHOLDER_REPLY)

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Return the same placeholder until agent-core help is available."""
        await self.start_command(update, context)

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Echo a placeholder reply for every authorized text message."""
        del context
        if not update.effective_user or not update.message:
            return
        if not self.is_authorized(update.effective_user.id):
            return
        text = update.message.text
        if not text:
            return
        session_id = str(update.effective_chat.id) if update.effective_chat else str(update.effective_user.id)
        if isinstance(self.agent, Agent):
            reply = await self.agent.respond(text, session_id)
        else:
            reply = PLACEHOLDER_REPLY
        await update.message.reply_text(reply)

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Transport-level error hook; logging is configured by the entrypoint."""
        del update
        if context.error:
            import logging

            logging.getLogger(__name__).error(
                "Telegram update failed", exc_info=context.error
            )

    def create_application(self, token: str | None = None) -> Application:
        """Build the Telegram application without connecting to the network."""
        bot_token = token or config.TELEGRAM_BOT_TOKEN
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to create the Telegram app")
        app = Application.builder().token(bot_token).build()
        app.add_handler(CommandHandler("start", self.start_command))
        app.add_handler(CommandHandler("help", self.help_command))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.message_handler))
        app.add_error_handler(self.error_handler)
        self.app = app
        return app


def create_telegram_handler(agent: Agent, store: Any | None = None) -> TelegramHandler:
    """Create the Telegram transport facade."""
    return TelegramHandler(agent, store=store)
