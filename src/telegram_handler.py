"""Telegram bot handlers for Dharvis."""

import logging
from typing import Any

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from src.claude_agent import ActionType, AgentResponse, ClaudeAgent
from src.config import config
from src.database import Database
from src.calendar_service import CalendarService
from src.utils import (
    format_datetime_for_display,
    format_events_list,
    format_tasks_list,
    get_current_time,
    get_day_range,
    get_week_range,
)

logger = logging.getLogger(__name__)


class TelegramHandler:
    """Handles Telegram bot interactions."""

    def __init__(
        self,
        database: Database,
        claude_agent: ClaudeAgent,
        calendar_service: CalendarService | None = None,
    ):
        """Initialize the handler.

        Args:
            database: Database instance for persistence.
            claude_agent: Claude agent for NLP processing.
            calendar_service: Optional Google Calendar service.
        """
        self.db = database
        self.agent = claude_agent
        self.calendar = calendar_service
        self.app: Application | None = None

    def _is_authorized(self, user_id: int) -> bool:
        """Check if a user is authorized to use the bot.

        Args:
            user_id: Telegram user ID.

        Returns:
            True if authorized (or no restriction set).
        """
        if config.ALLOWED_USER_ID is None:
            return True
        return user_id == config.ALLOWED_USER_ID

    async def start_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /start command."""
        if not update.effective_user or not update.message:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text(
                "Sorry, this bot is configured for private use only."
            )
            return

        welcome_message = """Hey! I'm Dharvis, your personal task and calendar assistant.

You can text me naturally to:
- Add tasks: "finish essay by friday"
- Add events: "coffee with Jake tomorrow 3pm"
- Check schedule: "what do I have today"
- Mark complete: "done with the essay"
- Delete/modify: "cancel the meeting" or "move it to 4pm"

Commands:
/today - Today's briefing
/week - Week overview
/tasks - Pending tasks
/help - This message"""

        await update.message.reply_text(welcome_message)

    async def help_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /help command."""
        if not update.message:
            return

        if not self._is_authorized(update.effective_user.id):
            return

        help_message = """Dharvis - Task & Calendar Assistant

Natural language examples:
- "add task: finish homework by Friday"
- "meeting with advisor tomorrow 2pm"
- "what's due this week"
- "mark math pset as done"
- "cancel the dinner on Saturday"
- "move the meeting to 3pm"

Commands:
/today - Today's schedule and tasks
/week - Week overview
/tasks - All pending tasks
/help - Show this help"""

        await update.message.reply_text(help_message)

    async def today_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /today command - show today's briefing."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            return

        await self._send_daily_briefing(update)

    async def week_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /week command - show week overview."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            return

        await self._send_week_overview(update)

    async def tasks_command(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle /tasks command - list pending tasks."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            return

        tasks = await self.db.get_pending_tasks()

        if not tasks:
            await update.message.reply_text("No pending tasks!")
            return

        formatted = format_tasks_list(tasks)
        await update.message.reply_text(f"Pending tasks:\n\n{formatted}")

    async def message_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Handle natural language messages."""
        if not update.message or not update.effective_user:
            return

        if not self._is_authorized(update.effective_user.id):
            await update.message.reply_text(
                "Sorry, this bot is configured for private use only."
            )
            return

        user_message = update.message.text
        if not user_message:
            return

        # Gather context - fetch upcoming week for full context
        tasks = await self.db.get_pending_tasks()
        start, end = get_week_range()
        events = await self.db.get_events_between(start, end)

        gcal_events = []
        if self.calendar and self.calendar.is_available():
            gcal_events = await self.calendar.get_upcoming_events(days=7)

        # Process with Claude
        response = self.agent.process_message(
            user_message, tasks=tasks, events=events, gcal_events=gcal_events
        )

        # Execute action
        result_message = await self._execute_action(response)

        # Store conversation
        await self.db.add_conversation(user_message, result_message)

        # Send response
        await update.message.reply_text(result_message)

    async def _execute_action(self, response: AgentResponse) -> str:
        """Execute the action from Claude's response.

        Args:
            response: Parsed agent response.

        Returns:
            Message to send to user.
        """
        params = response.params

        try:
            if response.action == ActionType.ADD_TASK:
                task_id = await self.db.add_task(
                    title=params.get("title", "Untitled task"),
                    deadline=params.get("deadline"),
                    priority=params.get("priority", "medium"),
                    description=params.get("description"),
                )
                logger.info(f"Added task {task_id}: {params.get('title')}")

            elif response.action == ActionType.ADD_EVENT:
                event_id = await self.db.add_event(
                    title=params.get("title", "Untitled event"),
                    start_time=params.get("start_time"),
                    end_time=params.get("end_time"),
                    location=params.get("location"),
                    description=params.get("description"),
                )
                logger.info(f"Added event {event_id}: {params.get('title')}")

            elif response.action == ActionType.COMPLETE_TASK:
                success = await self.db.complete_task(
                    task_id=params.get("task_id"),
                    title=params.get("task_title"),
                )
                if not success:
                    return "Couldn't find that task to mark complete. Can you be more specific?"

            elif response.action == ActionType.DELETE_TASK:
                success = await self.db.delete_task(
                    task_id=params.get("id"),
                    title=params.get("title"),
                )
                if not success:
                    return "Couldn't find that task to delete. Can you be more specific?"

            elif response.action == ActionType.DELETE_EVENT:
                success = await self.db.delete_event(
                    event_id=params.get("id"),
                    title=params.get("title"),
                )
                if not success:
                    return "Couldn't find that event to delete. Can you be more specific?"

            elif response.action == ActionType.MODIFY_TASK:
                task_id = params.get("task_id")
                if not task_id and params.get("task_title"):
                    task = await self.db.fuzzy_match_task(params["task_title"])
                    if task:
                        task_id = task["id"]

                if task_id:
                    update_fields = {}
                    if params.get("new_title"):
                        update_fields["title"] = params["new_title"]
                    if params.get("new_deadline"):
                        update_fields["deadline"] = params["new_deadline"]
                    if params.get("new_priority"):
                        update_fields["priority"] = params["new_priority"]

                    if update_fields:
                        success = await self.db.update_task(task_id, **update_fields)
                        if not success:
                            return "Couldn't update that task. Can you try again?"
                else:
                    return "Couldn't find that task to modify. Can you be more specific?"

            elif response.action == ActionType.MODIFY_EVENT:
                event_id = params.get("event_id")
                if not event_id and params.get("event_title"):
                    event = await self.db.fuzzy_match_event(params["event_title"])
                    if event:
                        event_id = event["id"]

                if event_id:
                    update_fields = {}
                    if params.get("new_title"):
                        update_fields["title"] = params["new_title"]
                    if params.get("new_start_time"):
                        update_fields["start_time"] = params["new_start_time"]
                    if params.get("new_end_time"):
                        update_fields["end_time"] = params["new_end_time"]
                    if params.get("new_location"):
                        update_fields["location"] = params["new_location"]

                    if update_fields:
                        success = await self.db.update_event(event_id, **update_fields)
                        if not success:
                            return "Couldn't update that event. Can you try again?"
                else:
                    return "Couldn't find that event to modify. Can you be more specific?"

            # QUERY action doesn't need database operations

        except Exception as e:
            logger.error(f"Error executing action {response.action}: {e}")
            return "Something went wrong processing that. Try again?"

        return response.message

    async def _send_daily_briefing(self, update: Update) -> None:
        """Send today's briefing to the user."""
        now = get_current_time()
        today_str = now.strftime("%A %b %-d")

        # Get today's events
        start, end = get_day_range()
        bot_events = await self.db.get_events_between(start, end)

        gcal_events = []
        if self.calendar and self.calendar.is_available():
            gcal_events = await self.calendar.get_today_events()

        # Get tasks due today
        tasks_due = await self.db.get_tasks_due_by(end)

        # Get upcoming tasks (next few days)
        week_start, week_end = get_week_range()
        all_tasks = await self.db.get_pending_tasks()

        # Build briefing
        lines = [f"Today ({today_str}):"]
        lines.append("")

        # Events section
        all_events = gcal_events + bot_events
        if all_events:
            for event in sorted(all_events, key=lambda e: e.get("start_time", "")):
                time_str = format_datetime_for_display(event.get("start_time"))
                location = f" at {event['location']}" if event.get("location") else ""
                source = " [gcal]" if event.get("source") == "gcal" else ""
                lines.append(f"📅 {event['title']} - {time_str}{location}{source}")
        else:
            lines.append("No events scheduled today.")

        lines.append("")

        # Tasks due today
        if tasks_due:
            lines.append("📋 Due today:")
            for task in tasks_due:
                deadline_str = format_datetime_for_display(task.get("deadline"))
                lines.append(f"  - {task['title']} (by {deadline_str})")
        else:
            lines.append("No tasks due today.")

        # Upcoming warnings
        upcoming = [t for t in all_tasks if t not in tasks_due][:3]
        if upcoming:
            lines.append("")
            lines.append("⚠️ Coming up:")
            for task in upcoming:
                deadline_str = format_datetime_for_display(task.get("deadline"))
                lines.append(f"  - {task['title']} ({deadline_str})")

        await update.message.reply_text("\n".join(lines))

    async def _send_week_overview(self, update: Update) -> None:
        """Send week overview to the user."""
        now = get_current_time()
        start, end = get_week_range()

        # Get week's events
        bot_events = await self.db.get_events_between(start, end)

        gcal_events = []
        if self.calendar and self.calendar.is_available():
            gcal_events = await self.calendar.get_events_between(start, end)

        # Get tasks due this week
        tasks = await self.db.get_tasks_due_by(end)

        # Build overview
        lines = [f"This week ({start.strftime('%b %-d')} - {end.strftime('%b %-d')}):"]
        lines.append("")

        # Events
        all_events = gcal_events + bot_events
        if all_events:
            lines.append("Events:")
            for event in sorted(all_events, key=lambda e: e.get("start_time", "")):
                time_str = format_datetime_for_display(event.get("start_time"))
                lines.append(f"  📅 {event['title']} - {time_str}")
        else:
            lines.append("No events this week.")

        lines.append("")

        # Tasks
        if tasks:
            lines.append("Tasks due:")
            for task in tasks:
                deadline_str = format_datetime_for_display(task.get("deadline"))
                lines.append(f"  📋 {task['title']} ({deadline_str})")
        else:
            lines.append("No tasks due this week.")

        await update.message.reply_text("\n".join(lines))

    def create_application(self) -> Application:
        """Create and configure the Telegram application.

        Returns:
            Configured Application instance.
        """
        self.app = (
            Application.builder()
            .token(config.TELEGRAM_BOT_TOKEN)
            .build()
        )

        # Register handlers
        self.app.add_handler(CommandHandler("start", self.start_command))
        self.app.add_handler(CommandHandler("help", self.help_command))
        self.app.add_handler(CommandHandler("today", self.today_command))
        self.app.add_handler(CommandHandler("week", self.week_command))
        self.app.add_handler(CommandHandler("tasks", self.tasks_command))

        # Natural language handler (must be last)
        self.app.add_handler(
            MessageHandler(filters.TEXT & ~filters.COMMAND, self.message_handler)
        )

        return self.app

    async def run_polling(self) -> None:
        """Start the bot with polling mode."""
        if not self.app:
            self.create_application()

        logger.info("Starting Dharvis bot with polling...")
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(drop_pending_updates=True)

        # Keep running until stopped
        try:
            while True:
                import asyncio
                await asyncio.sleep(1)
        except asyncio.CancelledError:
            pass
        finally:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
