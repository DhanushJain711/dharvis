"""Thin, single-user Telegram transport for the scheduling agent."""

from __future__ import annotations

import asyncio
import base64
import copy
from contextlib import suppress
from datetime import datetime, timedelta, timezone
import hashlib
import io
import json
import logging
import math
import os
from pathlib import Path
import re
import secrets
import tempfile
import time
from typing import Any

from openai import AsyncOpenAI
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ChatAction, ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from .agent import Agent
from .config import config

LOGGER = logging.getLogger(__name__)

PLACEHOLDER_REPLY = "i’m up — send me a task, event, or what you’re trying to plan"
TELEGRAM_TEXT_LIMIT = 4096
_CHECKLIST_CALLBACK = "cl"
_WHY_CALLBACK = "why"
_RATE_LIMIT_MESSAGE = "I’m handling too many requests right now. Please try again shortly."
_GENERIC_ERROR_MESSAGE = "I couldn’t process that. Please try again."
_EMPTY_RESPONSE_MESSAGE = "I don’t have a response to send yet."
_TRANSCRIPTION_MODEL = "gpt-4o-mini-transcribe"

_MAX_INPUT_TEXT = 32_000
_MAX_CAPTION = 4_096
_MAX_VOICE_BYTES = 25 * 1024 * 1024
_MAX_PHOTO_BYTES = 20 * 1024 * 1024
_MAX_PDF_BYTES = 20 * 1024 * 1024
_MAX_CHECKLIST_ITEMS = 20
_MAX_CHECKLIST_LABEL = 120
_MAX_CALLBACK_PREFIX = 64
_MAX_ITEM_VALUE_JSON = 512
_MAX_ACTIVE_CHECKLISTS = 20
_MAX_STATE_FILE_BYTES = 1024 * 1024
_CHECKLIST_TTL_SECONDS = 3 * 24 * 60 * 60


class _TokenBucket:
    """Small monotonic, concurrency-safe limiter for credit-spending calls."""

    def __init__(self, capacity: float = 8, refill_per_second: float = 0.2) -> None:
        self.capacity = capacity
        self.refill_per_second = refill_per_second
        self._tokens = capacity
        self._updated_at = time.monotonic()
        self._lock = asyncio.Lock()

    async def consume(self, amount: float = 1) -> bool:
        async with self._lock:
            now = time.monotonic()
            elapsed = max(0.0, now - self._updated_at)
            self._tokens = min(
                self.capacity, self._tokens + elapsed * self.refill_per_second
            )
            self._updated_at = now
            if self._tokens < amount:
                return False
            self._tokens -= amount
            return True


class TelegramHandler:
    """Authorize updates and translate Telegram payloads to agent calls."""

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
        self._openai: AsyncOpenAI | None = None
        self._checklist_lock = asyncio.Lock()
        self._credits = _TokenBucket()
        self._checklist_path = Path(
            f"{config.DATABASE_PATH}.telegram-checklists.json"
        )
        self._checklists = self._load_checklists()

    def is_authorized(self, user_id: int) -> bool:
        """Return whether a Telegram user may access this personal bot."""
        return config.ALLOWED_USER_ID is not None and user_id == config.ALLOWED_USER_ID

    def _is_authorized(self, user_id: int) -> bool:
        """Compatibility alias for the legacy handler."""
        return self.is_authorized(user_id)

    def _authorize_update(self, update: Update, kind: str) -> bool:
        user = update.effective_user
        chat = update.effective_chat
        allowed_id = config.ALLOWED_USER_ID
        authorized = (
            user is not None
            and chat is not None
            and allowed_id is not None
            and user.id == allowed_id
            and chat.id == allowed_id
            and chat.type == ChatType.PRIVATE
        )
        if authorized:
            return True
        LOGGER.warning(
            "Rejected Telegram %s user_id=%s chat_id=%s chat_type=%s",
            kind,
            user.id if user is not None else None,
            chat.id if chat is not None else None,
            chat.type if chat is not None else None,
        )
        return False

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Acknowledge that the personal bot is online."""
        del context
        if not self._authorize_update(update, "command"):
            return
        message = getattr(update, "message", None)
        if message is not None:
            await message.reply_text(PLACEHOLDER_REPLY)

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Describe the compact command surface."""
        del context
        if not self._authorize_update(update, "command"):
            return
        message = getattr(update, "message", None)
        if message is not None:
            await message.reply_text(
                "text me naturally — I can manage tasks, events, goals, and scheduling. "
                "Use /cost for today and month-to-date model usage."
            )

    async def cost_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show persisted interactive/background cost and cache counters."""
        del context
        if not self._authorize_update(update, "command"):
            return
        message = getattr(update, "message", None)
        if message is None or self.store is None:
            return
        from . import timeutil

        now = timeutil.now_utc()
        local_today = timeutil.now_local().date()
        today_start, today_end = timeutil.day_bounds(local_today)
        month_start = timeutil.day_bounds(local_today.replace(day=1))[0]
        today, month = await asyncio.gather(
            self.store.usage_summary(today_start, today_end),
            self.store.usage_summary(month_start, now + timedelta(microseconds=1)),
        )

        def render(label: str, rows: list[dict[str, Any]]) -> str:
            by_kind = {row["kind"]: row for row in rows}
            parts: list[str] = []
            for kind in ("agent", "background"):
                row = by_kind.get(kind, {})
                input_tokens = int(row.get("input_tokens") or 0)
                cached = int(row.get("cached_tokens") or 0)
                rate = cached / input_tokens * 100 if input_tokens else 0.0
                cost = float(row.get("estimated_cost_usd") or 0.0)
                parts.append(f"{kind} ${cost:.4f}, cache {rate:.0f}%")
            return f"{label}: " + "; ".join(parts)

        await message.reply_text(f"{render('today', today)}\n{render('month', month)}")

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Turn a supported Telegram message into an agent-loop input."""
        del context
        if not self._authorize_update(update, "message"):
            return
        message = getattr(update, "message", None)
        if message is None:
            LOGGER.warning("Ignored authorized non-new-message Telegram update")
            return
        try:
            if getattr(message, "voice", None) is not None:
                await self._handle_voice(update, message)
                return
            if getattr(message, "photo", None):
                await self._handle_photo(update, message)
                return
            document = getattr(message, "document", None)
            if document is not None and document.mime_type == "application/pdf":
                await self._handle_pdf(update, message)
                return
            text = getattr(message, "text", None)
            if text and not text.startswith("/"):
                if len(text) > _MAX_INPUT_TEXT:
                    await message.reply_text("That message is too long for me to process.")
                    return
                await self._run_agent_for_update(update, message, text)
        except Exception as exc:
            self._log_failure("message", exc)
            await self._reply_generic(message)

    async def _handle_voice(self, update: Update, message: Any) -> None:
        voice = message.voice
        if self._file_too_large(voice, _MAX_VOICE_BYTES):
            await message.reply_text("That voice note is too large for me to process.")
            return
        # Reserve transcription and agent calls before downloading any bytes.
        if not await self._credits.consume(2):
            await message.reply_text(_RATE_LIMIT_MESSAGE)
            return

        stop_typing, typing_task = self._start_typing(update.effective_chat)
        try:
            telegram_file = await voice.get_file()
            audio = bytes(await telegram_file.download_as_bytearray())
            if len(audio) > _MAX_VOICE_BYTES:
                await message.reply_text("That voice note is too large for me to process.")
                return
            transcript = await self._openai_client().audio.transcriptions.create(
                model=_TRANSCRIPTION_MODEL,
                file=("voice.ogg", audio, voice.mime_type or "audio/ogg"),
            )
            text = transcript.text.strip()
            if not text:
                await message.reply_text("I couldn’t hear any speech in that voice note.")
                return
            reply = await self._invoke_agent(text[:_MAX_INPUT_TEXT], self._session_id(update))
            await self._send_reply(message, reply)
        finally:
            await self._stop_typing(stop_typing, typing_task)

    async def _handle_photo(self, update: Update, message: Any) -> None:
        photo = message.photo[-1]
        if self._file_too_large(photo, _MAX_PHOTO_BYTES):
            await message.reply_text("That image is too large for me to process.")
            return
        if not await self._credits.consume():
            await message.reply_text(_RATE_LIMIT_MESSAGE)
            return

        stop_typing, typing_task = self._start_typing(update.effective_chat)
        try:
            telegram_file = await photo.get_file()
            image = bytes(await telegram_file.download_as_bytearray())
            if len(image) > _MAX_PHOTO_BYTES:
                await message.reply_text("That image is too large for me to process.")
                return
            transient_content = [
                {
                    "type": "input_text",
                    "text": self._bounded_caption(
                        getattr(message, "caption", None),
                        "The user sent this image.",
                    ),
                },
                {
                    "type": "input_image",
                    "image_url": "data:image/jpeg;base64,"
                    + base64.b64encode(image).decode("ascii"),
                    "detail": "auto",
                },
            ]
            reply = await self._invoke_multimodal(
                transient_content, self._session_id(update)
            )
            await self._send_reply(message, reply)
        finally:
            # The base64 payload remains local to this call and is never stored by
            # the Telegram layer or added to Telegram persistence.
            await self._stop_typing(stop_typing, typing_task)

    async def _handle_pdf(self, update: Update, message: Any) -> None:
        document = message.document
        if self._file_too_large(document, _MAX_PDF_BYTES):
            await message.reply_text("That PDF is too large for me to process.")
            return
        if not await self._credits.consume():
            await message.reply_text(_RATE_LIMIT_MESSAGE)
            return

        stop_typing, typing_task = self._start_typing(update.effective_chat)
        try:
            telegram_file = await document.get_file()
            pdf = bytes(await telegram_file.download_as_bytearray())
            if len(pdf) > _MAX_PDF_BYTES:
                await message.reply_text("That PDF is too large for me to process.")
                return
            filename = (document.file_name or "document.pdf")[:128]
            transient_content = [
                {
                    "type": "input_text",
                    "text": self._bounded_caption(
                        getattr(message, "caption", None),
                        "The user sent this PDF.",
                    ),
                },
                {
                    "type": "input_file",
                    "filename": filename,
                    "file_data": "data:application/pdf;base64,"
                    + base64.b64encode(pdf).decode("ascii"),
                },
            ]
            reply = await self._invoke_multimodal(
                transient_content, self._session_id(update)
            )
            await self._send_reply(message, reply)
        finally:
            await self._stop_typing(stop_typing, typing_task)

    async def _run_agent_for_update(
        self,
        update: Update,
        message: Any,
        agent_input: str,
    ) -> None:
        if not await self._credits.consume():
            await message.reply_text(_RATE_LIMIT_MESSAGE)
            return
        stop_typing, typing_task = self._start_typing(update.effective_chat)
        try:
            reply = await self._invoke_agent(agent_input, self._session_id(update))
            await self._send_reply(message, reply)
        finally:
            await self._stop_typing(stop_typing, typing_task)

    async def _invoke_agent(self, agent_input: str, session_id: str) -> str:
        """Invoke the normal text boundary."""
        loop = getattr(self.agent, "run_tool_loop", None)
        if callable(loop):
            result = await loop(agent_input, session_id)
        else:
            responder = getattr(self.agent, "respond", None)
            if not callable(responder):
                raise TypeError("Agent must provide run_tool_loop or respond")
            result = await responder(agent_input, session_id)
        return self._agent_result_text(result)

    async def _invoke_multimodal(
        self, transient_content: list[dict[str, Any]], session_id: str
    ) -> str:
        """Invoke an explicitly transient multimodal agent boundary.

        The preferred core API is ``run_multimodal_tool_loop(content, session_id)``.
        Implementations own submission to Responses and must not append data URLs to
        conversational history. ``run_tool_loop`` is used only for a duck-typed agent
        that explicitly opts in, or a compatibility fake with its own implementation.
        """
        for method_name in ("run_multimodal_tool_loop", "respond_multimodal"):
            method = getattr(self.agent, method_name, None)
            if callable(method):
                return self._agent_result_text(await method(transient_content, session_id))

        loop = getattr(self.agent, "run_tool_loop", None)
        explicitly_accepts = bool(
            getattr(self.agent, "accepts_transient_multimodal", False)
        )
        if callable(loop) and explicitly_accepts:
            return self._agent_result_text(await loop(transient_content, session_id))
        raise TypeError("Agent does not expose a transient multimodal boundary")

    @staticmethod
    def _agent_result_text(result: Any) -> str:
        if isinstance(result, str):
            return result
        return json.dumps(result, ensure_ascii=False, default=str)

    def _start_typing(self, chat: Any) -> tuple[asyncio.Event, asyncio.Task[None]]:
        stop = asyncio.Event()
        return stop, asyncio.create_task(self._typing_loop(chat, stop))

    @staticmethod
    async def _stop_typing(stop: asyncio.Event, task: asyncio.Task[None]) -> None:
        stop.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _typing_loop(self, chat: Any, stop: asyncio.Event) -> None:
        if chat is None:
            return
        while not stop.is_set():
            try:
                await chat.send_action(ChatAction.TYPING)
            except Exception:
                LOGGER.debug("Unable to send Telegram typing indicator")
                return
            try:
                await asyncio.wait_for(stop.wait(), timeout=4.5)
            except TimeoutError:
                continue

    @staticmethod
    def _session_id(update: Update) -> str:
        if update.effective_chat is not None:
            return str(update.effective_chat.id)
        if update.effective_user is not None:
            return str(update.effective_user.id)
        return "telegram"

    def _openai_client(self) -> AsyncOpenAI:
        if self._openai is None:
            self._openai = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)
        return self._openai

    async def send_checklist(self, items: list[Any], callback_prefix: str) -> Any:
        """Send a bounded, restart-safe checklist to the configured private chat."""
        app, chat_id = self._outbound_target()
        prefix = str(callback_prefix).strip()
        if not prefix or len(prefix.encode("utf-8")) > _MAX_CALLBACK_PREFIX:
            raise ValueError("callback_prefix must contain 1-64 UTF-8 bytes")
        if not 1 <= len(items) <= _MAX_CHECKLIST_ITEMS:
            raise ValueError(
                f"A checklist requires 1-{_MAX_CHECKLIST_ITEMS} items"
            )

        normalized = [self._normalize_checklist_item(item, i) for i, item in enumerate(items)]
        prefix_hash = hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:10]
        checklist_id = secrets.token_urlsafe(8)
        key = f"{prefix_hash}:{checklist_id}"
        now = datetime.now(timezone.utc)
        state = {
            "callback_prefix": prefix,
            "checklist_id": checklist_id,
            "items": normalized,
            "chat_id": chat_id,
            "message_id": None,
            "created_at": self._format_utc(now),
            "completion_delivered": False,
        }

        async with self._checklist_lock:
            current = self._clean_checklists(self._checklists, now)
            if len(current) >= _MAX_ACTIVE_CHECKLISTS:
                raise RuntimeError(
                    "Too many active checklists; complete or expire one before creating another"
                )
            provisional = copy.deepcopy(current)
            provisional[key] = state
            await self._save_checklists(provisional)
            self._checklists = provisional
            try:
                sent = await app.bot.send_message(
                    chat_id=chat_id,
                    text=self._checklist_text(state),
                    reply_markup=self._checklist_markup(key, state),
                )
            except Exception:
                cleaned = copy.deepcopy(self._checklists)
                cleaned.pop(key, None)
                try:
                    await self._save_checklists(cleaned)
                except Exception as cleanup_exc:
                    self._log_failure("checklist provisional cleanup", cleanup_exc)
                else:
                    self._checklists = cleaned
                raise

            committed = copy.deepcopy(self._checklists)
            committed[key]["message_id"] = sent.message_id
            try:
                await self._save_checklists(committed)
            except Exception:
                with suppress(Exception):
                    await app.bot.edit_message_reply_markup(
                        chat_id=chat_id, message_id=sent.message_id, reply_markup=None
                    )
                cleaned = copy.deepcopy(self._checklists)
                cleaned.pop(key, None)
                try:
                    await self._save_checklists(cleaned)
                except Exception as cleanup_exc:
                    self._log_failure("checklist commit cleanup", cleanup_exc)
                else:
                    self._checklists = cleaned
                raise
            self._checklists = committed
        return sent

    async def send_with_why(self, text: str, task_id: int) -> Any:
        """Send a schedule update with one-tap access to its decision history."""
        app, chat_id = self._outbound_target()
        normalized_task_id = int(task_id)
        if normalized_task_id < 1:
            raise ValueError("task_id must be positive")
        callback_data = f"{_WHY_CALLBACK}:{normalized_task_id}"
        if len(callback_data.encode("utf-8")) > 64:
            raise ValueError("task_id is too large for Telegram callback data")
        markup = InlineKeyboardMarkup(
            [[InlineKeyboardButton("Why?", callback_data=callback_data)]]
        )
        segments = self._split_output(text)
        last_message = None
        for index, (kind, content) in enumerate(segments):
            is_last = index == len(segments) - 1
            if kind == "text":
                last_message = await app.bot.send_message(
                    chat_id=chat_id,
                    text=content,
                    reply_markup=markup if is_last else None,
                )
            else:
                last_message = await app.bot.send_document(
                    chat_id=chat_id,
                    document=InputFile(
                        io.BytesIO(content.encode("utf-8")), "response.txt"
                    ),
                )
        if segments[-1][0] != "text":
            last_message = await app.bot.send_message(
                chat_id=chat_id,
                text="Why was this scheduled?",
                reply_markup=markup,
            )
        return last_message

    async def callback_query_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle checklist toggles/completion and schedule explanations."""
        del context
        if not self._authorize_update(update, "callback"):
            query = update.callback_query
            if query is not None:
                await query.answer("This bot is private.", show_alert=True)
            return
        query = update.callback_query
        message = update.effective_message
        if query is None:
            return
        try:
            data = query.data or ""
            if data.startswith(f"{_CHECKLIST_CALLBACK}:"):
                await self._handle_checklist_callback(query, data)
                return
            if data.startswith(f"{_WHY_CALLBACK}:"):
                await self._handle_why_callback(update, query, data)
                return
            await query.answer("This action is no longer available.", show_alert=True)
            LOGGER.warning("Ignored unknown Telegram callback")
        except Exception as exc:
            self._log_failure("callback", exc)
            await self._reply_generic(message)

    async def _handle_checklist_callback(self, query: Any, data: str) -> None:
        try:
            _, prefix_hash, checklist_id, action = data.split(":", 3)
        except ValueError:
            await query.answer("This checklist action is invalid.", show_alert=True)
            return
        key = f"{prefix_hash}:{checklist_id}"

        async with self._checklist_lock:
            state = self._checklists.get(key)
            if state is None:
                await query.answer("This checklist has expired.", show_alert=True)
                return
            if self._checklist_is_expired(state, datetime.now(timezone.utc)):
                remaining = copy.deepcopy(self._checklists)
                remaining.pop(key, None)
                await self._save_checklists(remaining)
                self._checklists = remaining
                await query.answer("This checklist has expired.", show_alert=True)
                with suppress(Exception):
                    await query.edit_message_reply_markup(reply_markup=None)
                return
            state = await self._bind_or_validate_callback(query, key, state)
            if state is None:
                LOGGER.warning("Rejected checklist callback with mismatched origin")
                await query.answer("This checklist action is invalid.", show_alert=True)
                return
            if action == "d":
                await query.answer()
                await self._complete_checklist(query, key, state)
                return
            try:
                item_text, target_text = action.split(".", 1)
                item_index = int(item_text)
                if target_text not in {"0", "1"}:
                    raise ValueError
                target = target_text == "1"
                if not 0 <= item_index < len(state["items"]):
                    raise IndexError
                item = state["items"][item_index]
            except (ValueError, IndexError, KeyError, TypeError):
                await query.answer("This checklist item is invalid.", show_alert=True)
                return
            await query.answer()
            proposed_state = copy.deepcopy(state)
            proposed_state["items"][item_index]["checked"] = target
            if bool(item["checked"]) == target:
                await query.edit_message_text(
                    text=self._checklist_text(proposed_state),
                    reply_markup=self._checklist_markup(key, proposed_state),
                )
                return
            proposed = copy.deepcopy(self._checklists)
            proposed[key] = proposed_state
            await self._save_checklists(proposed)
            try:
                await query.edit_message_text(
                    text=self._checklist_text(proposed_state),
                    reply_markup=self._checklist_markup(key, proposed_state),
                )
            except Exception:
                try:
                    await self._save_checklists(self._checklists)
                except Exception as rollback_exc:
                    self._log_failure("checklist toggle rollback", rollback_exc)
                raise
            self._checklists = proposed

    async def _complete_checklist(
        self, query: Any, key: str, state: dict[str, Any]
    ) -> None:
        delivered_state = copy.deepcopy(state)
        if not delivered_state["completion_delivered"]:
            if not await self._credits.consume():
                if query.message is not None:
                    await query.message.reply_text(_RATE_LIMIT_MESSAGE)
                return
            event = {
                "type": "telegram_checklist_completed",
                "callback_prefix": state["callback_prefix"],
                "checklist_id": state["checklist_id"],
                "items": [
                    {
                        "id": item["id"],
                        "value": item["value"],
                        "checked": bool(item["checked"]),
                    }
                    for item in state["items"]
                ],
            }
            chat = getattr(query.message, "chat", None)
            stop_typing, typing_task = self._start_typing(chat)
            try:
                await self._notify_checklist_completion(event, str(state["chat_id"]))
            finally:
                await self._stop_typing(stop_typing, typing_task)
            delivered_state["completion_delivered"] = True
            delivered = copy.deepcopy(self._checklists)
            delivered[key] = delivered_state
            await self._save_checklists(delivered)
            self._checklists = delivered

        await query.edit_message_text(
            text=self._checklist_text(delivered_state, completed=True),
            reply_markup=None,
        )
        remaining = copy.deepcopy(self._checklists)
        remaining.pop(key, None)
        await self._save_checklists(remaining)
        self._checklists = remaining

    async def _notify_checklist_completion(
        self, event: dict[str, Any], session_id: str
    ) -> None:
        """Surface UI state through an idempotent agent callback.

        The agent implementation must deduplicate retries by ``checklist_id``. A
        process can fail after delivery but before the local delivery flag commits.
        """
        for method_name in ("handle_checklist_completion", "on_checklist_completed"):
            method = getattr(self.agent, method_name, None)
            if callable(method):
                await method(event, session_id)
                return
        raise TypeError("Agent does not expose an idempotent checklist callback")

    async def _handle_why_callback(
        self, update: Update, query: Any, data: str
    ) -> None:
        try:
            task_id = int(data.split(":", 1)[1])
        except (ValueError, IndexError):
            await query.answer(
                "That schedule explanation is no longer available.", show_alert=True
            )
            return
        if task_id < 1:
            await query.answer(
                "That schedule explanation is no longer available.", show_alert=True
            )
            return
        await query.answer()
        if not await self._credits.consume():
            if query.message is not None:
                await query.message.reply_text(_RATE_LIMIT_MESSAGE)
            return

        stop_typing, typing_task = self._start_typing(update.effective_chat)
        try:
            history = await self._explain_schedule(task_id)
            if history in (None, [], {}):
                explanation = "No schedule decision history was found for this task."
            elif isinstance(history, str):
                explanation = history
            elif isinstance(history, list):
                explanation = self._format_why_history(history)
            else:
                explanation = str(history)
            if query.message is not None:
                await self._send_reply(query.message, explanation)
        finally:
            await self._stop_typing(stop_typing, typing_task)

    async def _explain_schedule(self, task_id: int) -> Any:
        execute_tool = getattr(self.agent, "execute_tool", None)
        if callable(execute_tool):
            return await execute_tool("explain_schedule", {"task_id": task_id})
        explain = getattr(self.agent, "explain_schedule", None)
        if callable(explain):
            return await explain(task_id)
        raise TypeError("Agent cannot execute explain_schedule")

    @staticmethod
    def _format_why_history(history: list[Any]) -> str:
        """Render the immutable rationale chain without database-shaped output."""
        from . import timeutil

        clauses: list[str] = []
        for index, raw in enumerate(history):
            if not isinstance(raw, dict):
                continue
            action = str(raw.get("action") or "scheduled")
            reason = " ".join(str(raw.get("reasoning") or "").split()).rstrip(" .")
            start = raw.get("start")
            if isinstance(start, str):
                with suppress(ValueError):
                    start = datetime.fromisoformat(start.replace("Z", "+00:00"))
            when = ""
            if isinstance(start, datetime) and start.tzinfo is not None:
                local = timeutil.to_local(start)
                when = local.strftime("%a at %-I:%M%p").replace(":00", "").lower()
            lead = "originally" if index == 0 else "then"
            if index == len(history) - 1 and index > 0:
                lead = "now"
            if action == "unscheduled":
                clauses.append(f"{lead} left it unscheduled — {reason}")
            else:
                clauses.append(f"{lead} {action} it {when} — {reason}".replace("  ", " "))
        return "; ".join(clauses) or "No schedule decision history was found for this task."

    @classmethod
    def _normalize_checklist_item(cls, item: Any, index: int) -> dict[str, Any]:
        if isinstance(item, dict):
            label_source = (
                item.get("title") or item.get("name") or item.get("text") or item
            )
            identifier = item.get("id", item.get("task_id", index))
            value = item.get("value", item)
        else:
            label_source = item
            identifier = index
            value = item
        label = re.sub(r"\s+", " ", str(label_source)).strip()
        label = (label or "Untitled item")[:_MAX_CHECKLIST_LABEL]
        return {
            "id": cls._bounded_json_value(identifier),
            "value": cls._bounded_json_value(value),
            "text": label,
            "checked": False,
        }

    @staticmethod
    def _bounded_json_value(value: Any) -> Any:
        if isinstance(value, float) and not math.isfinite(value):
            value = str(value)
        try:
            encoded = json.dumps(
                value,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError):
            value = str(value)
            encoded = json.dumps(value, ensure_ascii=False)
        if len(encoded.encode("utf-8")) <= _MAX_ITEM_VALUE_JSON:
            return value
        raw = encoded.encode("utf-8")[: _MAX_ITEM_VALUE_JSON - 3]
        return raw.decode("utf-8", errors="ignore") + "…"

    @staticmethod
    def _checklist_text(state: dict[str, Any], completed: bool = False) -> str:
        heading = "Evening debrief complete" if completed else "Evening debrief"
        lines = [heading, ""]
        for item in state["items"]:
            marker = "☑" if item["checked"] else "☐"
            lines.append(f"{marker} {item['text']}")
        return "\n".join(lines)

    @staticmethod
    def _checklist_markup(key: str, state: dict[str, Any]) -> InlineKeyboardMarkup:
        rows = []
        for index, item in enumerate(state["items"]):
            marker = "✅" if item["checked"] else "⬜"
            target = "0" if item["checked"] else "1"
            data = f"{_CHECKLIST_CALLBACK}:{key}:{index}.{target}"
            if len(data.encode("utf-8")) > 64:
                raise ValueError("Checklist callback data exceeds Telegram's limit")
            rows.append(
                [
                    InlineKeyboardButton(
                        f"{marker} {item['text'][:60]}", callback_data=data
                    )
                ]
            )
        done_data = f"{_CHECKLIST_CALLBACK}:{key}:d"
        rows.append([InlineKeyboardButton("Done", callback_data=done_data)])
        return InlineKeyboardMarkup(rows)

    async def _bind_or_validate_callback(
        self, query: Any, key: str, state: dict[str, Any]
    ) -> dict[str, Any] | None:
        """Bind a crash-retained provisional record or validate its exact origin."""
        message = query.message
        chat = getattr(message, "chat", None)
        message_id = getattr(message, "message_id", None)
        valid_origin = (
            message is not None
            and chat is not None
            and chat.id == state["chat_id"] == config.ALLOWED_USER_ID
            and chat.type == ChatType.PRIVATE
            and type(message_id) is int
            and message_id > 0
        )
        if not valid_origin:
            return None
        if state["message_id"] is not None:
            return state if message_id == state["message_id"] else None

        bound_state = copy.deepcopy(state)
        bound_state["message_id"] = message_id
        bound = copy.deepcopy(self._checklists)
        bound[key] = bound_state
        await self._save_checklists(bound)
        self._checklists = bound
        return bound_state

    def _load_checklists(self) -> dict[str, dict[str, Any]]:
        if config.ALLOWED_USER_ID is None or not self._checklist_path.exists():
            return {}
        try:
            if self._checklist_path.stat().st_size > _MAX_STATE_FILE_BYTES:
                raise ValueError("checklist state exceeds size limit")
            os.chmod(self._checklist_path, 0o600)
            raw = json.loads(self._checklist_path.read_text(encoding="utf-8"))
            records = raw.get("checklists", {}) if isinstance(raw, dict) else {}
            cleaned = self._clean_checklists(records, datetime.now(timezone.utc))
            if len(cleaned) > _MAX_ACTIVE_CHECKLISTS:
                raise ValueError("checklist state exceeds record limit")
            if cleaned != records:
                self._atomic_write_checklists(self._checklist_path, cleaned)
            return cleaned
        except (OSError, ValueError, TypeError) as exc:
            self._log_failure("checklist state load", exc)
            return {}

    def _clean_checklists(
        self, records: Any, now: datetime
    ) -> dict[str, dict[str, Any]]:
        if not isinstance(records, dict):
            return {}
        cleaned: dict[str, dict[str, Any]] = {}
        for key, raw in records.items():
            state = self._validated_checklist(key, raw, now)
            if state is not None:
                cleaned[key] = state
        return cleaned

    @staticmethod
    def _validated_checklist(
        key: Any, raw: Any, now: datetime
    ) -> dict[str, Any] | None:
        try:
            if not isinstance(key, str) or not re.fullmatch(
                r"[0-9a-f]{10}:[A-Za-z0-9_-]{8,16}", key
            ):
                return None
            if not isinstance(raw, dict):
                return None
            created_at = TelegramHandler._parse_utc(raw["created_at"])
            if created_at is None:
                return None
            message_id = raw.get("message_id")
            if TelegramHandler._checklist_is_expired(raw, now, created_at):
                return None
            chat_id = raw["chat_id"]
            if type(chat_id) is not int or chat_id != config.ALLOWED_USER_ID:
                return None
            if message_id is not None and (
                type(message_id) is not int or message_id < 1
            ):
                return None
            prefix = raw["callback_prefix"]
            checklist_id = raw["checklist_id"]
            if (
                not isinstance(prefix, str)
                or not 1 <= len(prefix.encode("utf-8")) <= _MAX_CALLBACK_PREFIX
                or not isinstance(checklist_id, str)
                or not key.endswith(f":{checklist_id}")
                or not key.startswith(
                    hashlib.sha256(prefix.encode("utf-8")).hexdigest()[:10] + ":"
                )
            ):
                return None
            items = raw["items"]
            if not isinstance(items, list) or not 1 <= len(items) <= _MAX_CHECKLIST_ITEMS:
                return None
            if not isinstance(raw.get("completion_delivered", False), bool):
                return None
            validated_items = []
            for item in items:
                if not isinstance(item, dict):
                    return None
                text = item["text"]
                if not isinstance(text, str) or not 1 <= len(text) <= _MAX_CHECKLIST_LABEL:
                    return None
                if not isinstance(item.get("checked", False), bool):
                    return None
                identifier = TelegramHandler._bounded_json_value(item.get("id"))
                value = TelegramHandler._bounded_json_value(item.get("value"))
                validated_items.append(
                    {
                        "id": identifier,
                        "value": value,
                        "text": text,
                        "checked": item.get("checked", False),
                    }
                )
            return {
                "callback_prefix": prefix,
                "checklist_id": checklist_id,
                "items": validated_items,
                "chat_id": chat_id,
                "message_id": message_id,
                "created_at": TelegramHandler._format_utc(created_at),
                "completion_delivered": raw.get("completion_delivered", False),
            }
        except (KeyError, TypeError, ValueError, UnicodeError):
            return None

    @staticmethod
    def _format_utc(value: datetime) -> str:
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _parse_utc(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.endswith("Z"):
            return None
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
            return None
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _checklist_is_expired(
        state: dict[str, Any],
        now: datetime,
        created_at: datetime | None = None,
    ) -> bool:
        created = created_at or TelegramHandler._parse_utc(state.get("created_at"))
        if created is None or created > now + timedelta(minutes=1):
            return True
        return now - created > timedelta(seconds=_CHECKLIST_TTL_SECONDS)

    async def _save_checklists(self, records: dict[str, dict[str, Any]]) -> None:
        snapshot = copy.deepcopy(records)
        await asyncio.to_thread(
            self._atomic_write_checklists, self._checklist_path, snapshot
        )

    @staticmethod
    def _atomic_write_checklists(
        path: Path, records: dict[str, dict[str, Any]]
    ) -> None:
        if len(records) > _MAX_ACTIVE_CHECKLISTS:
            raise ValueError("checklist state exceeds record limit")
        payload = json.dumps(
            {"version": 1, "checklists": records},
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(payload) > _MAX_STATE_FILE_BYTES:
            raise ValueError("serialized checklist state exceeds size limit")
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{path.name}.", dir=path.parent
        )
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as state_file:
                descriptor = -1
                state_file.write(payload)
                state_file.flush()
                os.fsync(state_file.fileno())
            os.replace(temp_name, path)
            os.chmod(path, 0o600)
        except Exception:
            if descriptor >= 0:
                os.close(descriptor)
            with suppress(OSError):
                os.unlink(temp_name)
            raise

    def _outbound_target(self) -> tuple[Application, int]:
        if self.app is None:
            raise RuntimeError("create_application must be called before sending messages")
        if config.ALLOWED_USER_ID is None:
            raise RuntimeError(
                "ALLOWED_USER_ID is required for outbound Telegram messages"
            )
        return self.app, config.ALLOWED_USER_ID

    async def _send_reply(self, message: Any, text: str) -> None:
        for kind, content in self._split_output(text):
            if kind == "text":
                await message.reply_text(content)
            else:
                await message.reply_document(
                    document=InputFile(
                        io.BytesIO(content.encode("utf-8")), "response.txt"
                    )
                )

    @classmethod
    def _split_output(cls, text: str) -> list[tuple[str, str]]:
        """Split at paragraphs, then safe sentences; oversize sentences become files."""
        normalized = str(text or "").strip()
        if not normalized:
            return [("text", _EMPTY_RESPONSE_MESSAGE)]
        if len(normalized) <= TELEGRAM_TEXT_LIMIT:
            return [("text", normalized)]

        units: list[tuple[str, str]] = []
        for paragraph in re.split(r"\n\s*\n", normalized):
            if len(paragraph) <= TELEGRAM_TEXT_LIMIT:
                units.append(("text", paragraph))
            else:
                # Natural-language sentence boundaries are not reliably inferable
                # from punctuation alone. Preserve the paragraph verbatim as a
                # document rather than risk splitting an abbreviation or sentence.
                units.append(("document", paragraph))

        packed: list[tuple[str, str]] = []
        pending = ""
        for kind, content in units:
            if kind == "document":
                if pending:
                    packed.append(("text", pending))
                    pending = ""
                packed.append((kind, content))
                continue
            candidate = content if not pending else f"{pending}\n\n{content}"
            if len(candidate) <= TELEGRAM_TEXT_LIMIT:
                pending = candidate
            else:
                if pending:
                    packed.append(("text", pending))
                pending = content
        if pending:
            packed.append(("text", pending))
        return packed

    @staticmethod
    def _file_too_large(media: Any, limit: int) -> bool:
        size = getattr(media, "file_size", None)
        return isinstance(size, int) and size > limit

    @staticmethod
    def _bounded_caption(caption: Any, fallback: str) -> str:
        value = str(caption).strip() if caption else fallback
        return value[:_MAX_CAPTION]

    @staticmethod
    async def _reply_generic(message: Any) -> None:
        if message is not None:
            with suppress(Exception):
                await message.reply_text(_GENERIC_ERROR_MESSAGE)

    @staticmethod
    def _log_failure(operation: str, exc: Exception) -> None:
        # Never log update text, data URLs, callback values, or exception messages.
        LOGGER.error("Telegram %s failed (%s)", operation, type(exc).__name__)

    async def error_handler(
        self, update: object, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Last-resort transport error hook without logging user content."""
        error = context.error
        if error:
            LOGGER.error("Telegram update failed (%s)", type(error).__name__)
        if isinstance(update, Update) and self._authorize_update(update, "error"):
            await self._reply_generic(update.effective_message)

    def create_application(self, token: str | None = None) -> Application:
        """Build the Telegram application and retain safe handler-owned state."""
        bot_token = token or config.TELEGRAM_BOT_TOKEN
        if not bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN is required to create the Telegram app")
        # Reload here so a long-lived constructed handler sees externally retained
        # state immediately before callbacks begin.
        self._checklists = self._load_checklists()
        app = Application.builder().token(bot_token).build()
        app.add_handler(
            CommandHandler(
                "start", self.start_command, filters=filters.UpdateType.MESSAGE
            )
        )
        app.add_handler(
            CommandHandler(
                "help", self.help_command, filters=filters.UpdateType.MESSAGE
            )
        )
        app.add_handler(
            CommandHandler(
                "cost", self.cost_command, filters=filters.UpdateType.MESSAGE
            )
        )
        app.add_handler(CallbackQueryHandler(self.callback_query_handler))
        app.add_handler(
            MessageHandler(
                filters.UpdateType.MESSAGE
                & filters.ALL
                & ~filters.StatusUpdate.ALL,
                self.message_handler,
            )
        )
        app.add_error_handler(self.error_handler)
        self.app = app
        return app


def create_telegram_handler(agent: Agent, store: Any | None = None) -> TelegramHandler:
    """Create the Telegram transport facade."""
    return TelegramHandler(agent, store=store)


def build_application() -> Application:
    """Compose a handler-complete Telegram application without starting it.

    Network and SQLite initialization deliberately stay asynchronous.  Both the
    polling entrypoint and the ASGI lifespan call
    :func:`initialize_application_runtime` before accepting Telegram updates.
    Keeping this factory synchronous makes it safe for ASGI module import while
    still exposing one canonical application composition point.
    """
    from .calendar_service import CalendarService
    from .facts_engine import FactsEngine
    from .history import History
    from .scheduler_engine import SchedulerEngine
    from .store import Store

    store = Store()
    calendar = CalendarService()
    scheduler_engine = SchedulerEngine(store, calendar)
    facts_engine = FactsEngine(store)
    agent = Agent(History(store))
    agent.facts_engine = facts_engine
    telegram = TelegramHandler(agent, store=store, calendar_service=calendar)
    application = telegram.create_application()
    application.bot_data.update(
        {
            "store": store,
            "calendar": calendar,
            "scheduler": scheduler_engine,
            "scheduler_engine": scheduler_engine,
            "facts": facts_engine,
            "facts_engine": facts_engine,
            "telegram": telegram,
            "telegram_handler": telegram,
            "runtime_initialized": False,
            "runtime_initialization_lock": asyncio.Lock(),
        }
    )
    return application


async def initialize_application_runtime(application: Application) -> None:
    """Initialize SQLite and tool bindings once before update processing."""
    runtime = application.bot_data
    lock = runtime.get("runtime_initialization_lock")
    if lock is None:
        lock = runtime["runtime_initialization_lock"] = asyncio.Lock()
    async with lock:
        if runtime.get("runtime_initialized"):
            return
        store = runtime["store"]
        await store.initialize()
        from .integration import build_tool_handlers

        agent = runtime["telegram_handler"].agent
        agent.tool_handlers = await build_tool_handlers(
            store,
            runtime["calendar"],
            runtime["scheduler_engine"],
            runtime["facts_engine"],
        )
        runtime["runtime_initialized"] = True
