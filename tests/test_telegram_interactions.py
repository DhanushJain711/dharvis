"""Exercise Telegram callbacks and settings with durable application state."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from telegram.error import BadRequest, NetworkError

from src import jobs, telegram_handler, timeutil
from src.agent import Agent
from src.config import config
from src.history import History
from src.store import Store
from src.telegram_handler import TelegramHandler


@pytest.fixture
def transport_config(monkeypatch, tmp_path):
    configured = replace(
        config, ALLOWED_USER_ID=123, TELEGRAM_BOT_TOKEN="123:test",
        DATABASE_PATH=tmp_path / "bot.sqlite", USER_TIMEZONE="America/Chicago",
        QUIET_HOURS_START="23:00", QUIET_HOURS_END="07:00",
    )
    monkeypatch.setattr(telegram_handler, "config", configured)
    monkeypatch.setattr(jobs, "config", configured)
    monkeypatch.setattr(jobs, "_runtime", None)
    monkeypatch.setattr(jobs, "_notification_times", {})
    return configured


def incoming(*, text="done with the report", update_id=42, user_id=123):
    chat = SimpleNamespace(id=user_id, type="private", send_action=AsyncMock())
    message = SimpleNamespace(
        text=text, chat=chat, message_id=25, reply_text=AsyncMock(),
    )
    return SimpleNamespace(
        update_id=update_id, message=message, effective_message=message,
        effective_chat=chat, effective_user=SimpleNamespace(id=user_id),
    )


async def checklist(handler, items):
    handler.app = SimpleNamespace(bot=SimpleNamespace(
        send_message=AsyncMock(return_value=SimpleNamespace(message_id=25)),
    ))
    await handler.send_checklist(
        items, f"daily-debrief:{timeutil.now_local().date().isoformat()}"
    )
    return next(iter(handler._checklists))


def callback(update, key, action):
    update.callback_query = SimpleNamespace(
        data=f"cl:{key}:{action}", message=update.message,
        answer=AsyncMock(), edit_message_text=AsyncMock(),
    )
    return update.callback_query


async def test_repeated_checklist_toggle_is_success_when_telegram_reports_unchanged(
    transport_config,
):
    handler = TelegramHandler(SimpleNamespace())
    key = await checklist(handler, [{"id": 1, "title": "report"}])
    update = incoming()
    query = callback(update, key, "0.1")
    await handler.callback_query_handler(update, SimpleNamespace())
    query.edit_message_text.side_effect = BadRequest("Message is not modified")

    await handler.callback_query_handler(update, SimpleNamespace())

    assert handler._checklists[key]["items"][0]["checked"] is True
    update.message.reply_text.assert_not_awaited()
    reloaded = TelegramHandler(SimpleNamespace())
    assert reloaded._checklists[key]["items"][0]["checked"] is True


async def test_other_telegram_edit_errors_are_not_swallowed(transport_config):
    handler = TelegramHandler(SimpleNamespace())
    key = await checklist(handler, ["report"])
    update = incoming()
    query = callback(update, key, "0.1")
    query.edit_message_text.side_effect = BadRequest("Message can't be edited")

    await handler.callback_query_handler(update, SimpleNamespace())

    assert handler._checklists[key]["items"][0]["checked"] is False
    update.message.reply_text.assert_awaited_once()


async def test_checklist_done_ignores_credit_limit_and_retries_only_display(
    transport_config,
):
    completed = AsyncMock()
    handler = TelegramHandler(SimpleNamespace(handle_checklist_completion=completed))
    handler._credits.consume = AsyncMock(return_value=False)
    key = await checklist(handler, ["report"])
    update = incoming()
    query = callback(update, key, "d")
    query.edit_message_text.side_effect = NetworkError("unavailable")

    await handler.callback_query_handler(update, SimpleNamespace())

    completed.assert_awaited_once()
    handler._credits.consume.assert_not_awaited()
    assert handler._checklists[key]["completion_delivered"] is True
    assert "saved your check-in" in update.message.reply_text.await_args.args[0]
    handler = TelegramHandler(SimpleNamespace(handle_checklist_completion=completed))
    query.edit_message_text.side_effect = BadRequest("Message is not modified")
    await handler.callback_query_handler(update, SimpleNamespace())
    assert key not in handler._checklists
    completed.assert_awaited_once()


async def test_saved_checklist_does_not_report_failure_if_ui_state_write_fails(
    transport_config,
):
    completed = AsyncMock()
    handler = TelegramHandler(SimpleNamespace(handle_checklist_completion=completed))
    key = await checklist(handler, ["report"])
    update = incoming()
    callback(update, key, "d")
    handler._save_checklists = AsyncMock(side_effect=OSError("disk unavailable"))

    await handler.callback_query_handler(update, None)

    completed.assert_awaited_once()
    assert handler._checklists[key]["completion_delivered"] is True
    assert "saved your check-in" in update.message.reply_text.await_args.args[0]


async def test_real_debrief_callback_saves_once_during_google_and_learning_outages(
    transport_config, monkeypatch,
):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    goal = await store.add_goal({
        "title": "job applications", "target_amount": 3, "target_unit": "hours",
        "period": "week", "category": "career",
    })
    task = (await store.add_tasks([{
        "title": "application", "estimated_minutes": 30, "goal_id": goal["id"],
    }]))[0]
    now = timeutil.now_utc()
    await store.apply_schedule_decision(
        task_id=task["id"], action="scheduled", start=now,
        end=now + timedelta(minutes=30), previous_start=None, previous_end=None,
        trigger="daily_plan", reasoning="This is the last gap before the deadline.",
        facts_used=[], gcal_event_id="owned-work-block",
    )
    local_date = timeutil.now_local().date()
    await store.upsert_daily_log(local_date, {
        "planned": [{"task_id": task["id"], "title": task["title"], "checklist_included": True}],
    })
    await store.append_message("user", "the morning block worked well", [], "day")
    handler = TelegramHandler(SimpleNamespace(), store=store)
    facts = SimpleNamespace(extract_from_day=AsyncMock(side_effect=RuntimeError("model unavailable")))
    calendar = SimpleNamespace(delete_work_block=AsyncMock(side_effect=RuntimeError("Google unavailable")))
    runtime = jobs._Runtime(
        scheduler=SimpleNamespace(), store=store,
        engine=SimpleNamespace(calendar=calendar), telegram=handler, facts_engine=facts,
    )
    monkeypatch.setattr(jobs, "_runtime", runtime)
    jobs._register_completion_handler(runtime)
    key = await checklist(handler, [{
        "id": task["id"], "title": task["title"],
        "value": {"task_id": task["id"], "actual_minutes": 30},
    }])
    update = incoming()
    query = callback(update, key, "0.1")
    await handler.callback_query_handler(update, None)
    query.data = f"cl:{key}:d"
    await handler.callback_query_handler(update, None)
    await handler.callback_query_handler(update, None)

    assert (await store.get_task(task["id"]))["status"] == "completed"
    progress = await store.get_goal_progress(goal["id"], timeutil.day_bounds(local_date)[0])
    assert progress["amount_done"] == 0.5
    log = await store.get_daily_log(local_date)
    assert len(log["completed"]) == 1
    assert "[debrief-checklist:" in log["notes"]
    assert len(await store.get_pending_debrief_learning(local_date)) == 1
    assert len(await store.list_pending_calendar_cleanup()) == 1
    facts.extract_from_day.assert_not_awaited()
    assert key not in handler._checklists
    assert "complete" in query.edit_message_text.await_args.kwargs["text"].lower()
    await jobs._scheduled_debrief_learning()
    facts.extract_from_day.assert_awaited_once()
    evidence = facts.extract_from_day.await_args.kwargs
    assert len(evidence["decisions"]) == 1
    assert len(evidence["conversation"]) == 1
    assert key not in handler._checklists
    assert "complete" in query.edit_message_text.await_args.kwargs["text"].lower()
    update.message.reply_text.assert_not_awaited()


async def test_times_command_persists_across_handler_restart_and_reports_zone(
    transport_config,
):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    handler = TelegramHandler(SimpleNamespace(), store=store)
    update = incoming()

    await handler.times_command(update, SimpleNamespace(args=["morning", "08:30"]))
    assert (await store.get_notification_times())["morning"] == "08:30"
    assert "saved" in update.message.reply_text.await_args.args[0]

    restarted = TelegramHandler(SimpleNamespace(), store=Store(transport_config.DATABASE_PATH))
    await restarted.times_command(update, SimpleNamespace(args=[]))
    text = update.message.reply_text.await_args.args[0]
    assert "8:30am" in text
    assert "America/Chicago" in text


@pytest.mark.parametrize("args", [["morning", "8:30"], ["evening", "25:00"], ["noon", "12:00"], ["morning"]])
async def test_times_rejects_malformed_input_without_a_write(transport_config, args):
    store = SimpleNamespace(set_notification_times=AsyncMock())
    handler = TelegramHandler(SimpleNamespace(), store=store)
    update = incoming()

    await handler.times_command(update, SimpleNamespace(args=args))

    assert "24-hour" in update.message.reply_text.await_args.args[0]
    store.set_notification_times.assert_not_awaited()


async def test_times_rejects_quiet_hours_and_unauthorized_user(transport_config):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    original = await store.get_notification_times()
    handler = TelegramHandler(SimpleNamespace(), store=store)
    update = incoming()
    await handler.times_command(update, SimpleNamespace(args=["evening", "23:30"]))
    assert "quiet hours" in update.message.reply_text.await_args.args[0]
    unauthorized = incoming(user_id=456)
    await handler.times_command(unauthorized, SimpleNamespace(args=["morning", "09:00"]))
    unauthorized.message.reply_text.assert_not_awaited()
    assert await store.get_notification_times() == original


async def test_same_update_runs_once_concurrently_and_after_restart(transport_config):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    agent = SimpleNamespace(run_tool_loop=AsyncMock(return_value="done"))
    handler = TelegramHandler(agent, store=store)
    update = incoming()

    await asyncio.gather(*(handler.message_handler(update, None) for _ in range(3)))
    restarted = TelegramHandler(agent, store=Store(transport_config.DATABASE_PATH))
    await restarted.message_handler(update, None)

    agent.run_tool_loop.assert_awaited_once()
    update.message.reply_text.assert_awaited_once_with("done")
    assert handler._update_locks == {}
    # Identical text in a genuinely new update must still be handled.
    await restarted.message_handler(incoming(update_id=43), None)
    assert agent.run_tool_loop.await_count == 2


@pytest.mark.parametrize("failure", ["agent", "send", "empty", "rate_limit"])
async def test_failed_or_deferred_update_is_retryable(transport_config, failure):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    agent = SimpleNamespace(run_tool_loop=AsyncMock(return_value="done"))
    handler = TelegramHandler(agent, store=store)
    update = incoming()
    if failure == "agent":
        agent.run_tool_loop.side_effect = RuntimeError("service unavailable")
    elif failure == "send":
        update.message.reply_text.side_effect = NetworkError("transport unavailable")
    elif failure == "empty":
        agent.run_tool_loop.return_value = None
    else:
        handler._credits.consume = AsyncMock(return_value=False)

    await handler.message_handler(update, None)
    assert await store.has_processed_update(42) is False
    assert handler._update_locks == {}

    agent.run_tool_loop.side_effect = None
    agent.run_tool_loop.return_value = "done"
    update.message.reply_text.side_effect = None
    handler._credits.consume = AsyncMock(return_value=True)
    await handler.message_handler(update, None)
    assert await store.has_processed_update(42) is True


async def test_receipt_failure_does_not_contradict_delivered_reply(transport_config):
    store = SimpleNamespace(
        has_processed_update=AsyncMock(return_value=False),
        mark_update_processed=AsyncMock(side_effect=RuntimeError("database locked")),
    )
    handler = TelegramHandler(
        SimpleNamespace(run_tool_loop=AsyncMock(return_value="added")), store=store
    )
    update = incoming()

    await handler.message_handler(update, None)

    update.message.reply_text.assert_awaited_once_with("added")
    assert handler._update_locks == {}


@pytest.mark.parametrize("mutate_first", [False, True])
async def test_real_agent_failure_receipts_distinguish_safe_retry_from_saved_changes(
    transport_config, mutate_first,
):
    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    call = {
        "type": "function_call", "call_id": "create-task",
        "name": "add_task", "arguments": json.dumps({"tasks": [{"title": "laundry"}]}),
    }
    tool_response = SimpleNamespace(status="completed", output=[call], output_text="", usage=None)
    final_response = SimpleNamespace(status="completed", output=[], output_text="added", usage=None)
    responses = AsyncMock(side_effect=(
        [tool_response, RuntimeError("model unavailable")]
        if mutate_first else [RuntimeError("model unavailable"), tool_response, final_response]
    ))
    agent = Agent(
        History(store), tool_handlers={"add_task": store.add_tasks},
        client=SimpleNamespace(responses=SimpleNamespace(create=responses)),
    )
    handler = TelegramHandler(agent, store=store)
    update = incoming(text="add laundry")

    await handler.message_handler(update, None)

    assert await store.has_processed_update(update.update_id) is mutate_first
    assert len(await store.query_tasks()) == int(mutate_first)
    # Replay on a fresh handler uses the persisted receipt after an uncertain
    # mutation, or retries the actual Agent's internally caught pre-call error.
    restarted = TelegramHandler(agent, store=Store(transport_config.DATABASE_PATH))
    await restarted.message_handler(update, None)

    assert responses.await_count == (2 if mutate_first else 3)
    assert len(await store.query_tasks()) == 1
    assert await store.has_processed_update(update.update_id) is True
    assert update.message.reply_text.await_count == (1 if mutate_first else 2)


async def test_runtime_loads_saved_times_before_completing_initialization(
    transport_config, monkeypatch,
):
    import src.integration

    store = Store(transport_config.DATABASE_PATH)
    await store.initialize()
    await store.set_notification_times(morning="09:15", evening="20:45")
    bindings = AsyncMock(return_value={})
    monkeypatch.setattr(src.integration, "build_tool_handlers", bindings)
    application = SimpleNamespace(bot_data={
        "store": store, "telegram_handler": TelegramHandler(SimpleNamespace(), store=store),
        "calendar": object(), "scheduler_engine": object(), "facts_engine": object(),
    })

    await telegram_handler.initialize_application_runtime(application)
    await telegram_handler.initialize_application_runtime(application)

    assert jobs._notification_times == {"morning": "09:15", "evening": "20:45"}
    assert application.bot_data["runtime_initialized"] is True
    bindings.assert_awaited_once()


def test_times_command_is_registered(transport_config):
    application = TelegramHandler(SimpleNamespace()).create_application()
    commands = {
        name for handler in application.handlers[0]
        for name in getattr(handler, "commands", ())
    }
    assert "times" in commands
