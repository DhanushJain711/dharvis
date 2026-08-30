from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from src.agent import MAX_MODEL_CALLS, Agent
from src.calendar_service import CalendarError
from src.facts_engine import FactsEngine
from src.freebusy import query_schedule
from src.history import History
from src.integration import build_tool_handlers
from src.scheduler_engine import SchedulerEngine
from src.store import Store
from src.telegram_handler import TelegramHandler
from src.tools import TOOLS_BY_NAME


class FakeCalendar:
    _last_query_complete = True

    def __init__(self) -> None:
        self.events: list[dict] = []
        self.deleted: list[str] = []

    async def list_events(self, start, end, *, force_refresh=False):
        return list(self.events)

    async def create_event(self, event, reasoning=None, *, category=None, kind="fixed-event"):
        created = dict(event)
        created["start_time"] = created.pop("start", created.get("start_time", None))
        created["end_time"] = created.pop("end", created.get("end_time", None))
        created["gcal_event_id"] = f"g-{len(self.events) + 1}"
        self.events.append(created)
        return created

    async def update_event(self, event_id, changes, *, category=None, kind=None):
        return {"gcal_event_id": event_id, **changes}

    async def delete_event(self, event_id):
        self.deleted.append(event_id)

    async def create_work_block(self, task_id, title, start, end, reasoning):
        return f"work-{task_id}"

    async def update_work_block(self, *args):
        return None

    async def delete_work_block(self, event_id):
        self.deleted.append(event_id)


class FakeResponses:
    def __init__(self, responses):
        self.items = list(responses)
        self.calls: list[dict] = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.items.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def response(output, text="", *, usage=None):
    return SimpleNamespace(
        status="completed",
        output=output,
        output_text=text,
        usage=usage or {
            "input_tokens": 100,
            "output_tokens": 10,
            "total_tokens": 110,
            "input_tokens_details": {"cached_tokens": 60, "cache_write_tokens": 0},
        },
    )


class SpyScheduler:
    def __init__(self) -> None:
        self.conflicts: list[tuple] = []
        self.reschedules: list[tuple] = []

    async def detect_conflicts(self, start, end):
        self.conflicts.append((start, end))
        return []

    async def reschedule(self, reason, affected_range, *, trigger="conflict"):
        self.reschedules.append((reason, affected_range, trigger))
        return []

    async def explain_schedule(self, task_id):
        return []

    async def schedule_task(self, *args, **kwargs):
        raise AssertionError("not used by this test")


@pytest.mark.asyncio
async def test_full_message_agent_tool_store_response_loop(tmp_path):
    store = Store(tmp_path / "runtime.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    scheduler = SchedulerEngine(store, calendar, client=object())
    handlers = await build_tool_handlers(store, calendar, scheduler, FactsEngine(store))
    call = {
        "type": "function_call",
        "call_id": "call-1",
        "name": "add_task",
        "arguments": json.dumps({
            "tasks": [{
                "title": "laundry", "description": None, "deadline": None,
                "estimated_minutes": 30, "category": "personal",
                "energy": "light", "priority": "medium", "goal_id": None,
            }]
        }),
    }
    client = SimpleNamespace(responses=FakeResponses([
        response([call]), response([], "added"),
    ]))
    agent = Agent(History(store), tool_handlers=handlers, client=client)

    assert await agent.run_tool_loop("add laundry", "chat-1") == "added"
    tasks = await store.query_tasks()
    assert [task["title"] for task in tasks] == ["laundry"]
    assert set(handlers) == set(TOOLS_BY_NAME)
    assert len(client.responses.calls) == 2
    first = client.responses.calls[0]
    assert first["prompt_cache_options"] == {"mode": "explicit"}
    assert first["input"][0]["role"] == "system"
    assert first["input"][0]["content"][0]["prompt_cache_breakpoint"]
    usage = await store.usage_summary(
        datetime.now(UTC) - timedelta(minutes=1),
        datetime.now(UTC) + timedelta(minutes=1),
    )
    assert usage[0]["calls"] == 2
    assert usage[0]["cached_tokens"] == 120


@pytest.mark.asyncio
async def test_full_event_loop_writes_calendar_and_store(tmp_path):
    store = Store(tmp_path / "event-runtime.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    scheduler = SchedulerEngine(store, calendar, client=object())
    handlers = await build_tool_handlers(store, calendar, scheduler, FactsEngine(store))
    call = {
        "type": "function_call", "call_id": "event-1", "name": "add_event",
        "arguments": json.dumps({"events": [{
            "title": "dentist", "description": None,
            "start": "2026-08-28T19:00:00Z", "end": "2026-08-28T20:00:00Z",
            "location": None, "category": "personal",
        }]}),
    }
    client = SimpleNamespace(responses=FakeResponses([
        response([call]), response([], "dentist is on for 2"),
    ]))
    agent = Agent(History(store), tool_handlers=handlers, client=client)
    assert await agent.run_tool_loop("dentist tomorrow at 2", "chat-2") == "dentist is on for 2"
    events = await store.query_events(
        datetime(2026, 8, 28, 18, tzinfo=UTC),
        datetime(2026, 8, 28, 21, tzinfo=UTC),
    )
    assert events[0]["gcal_event_id"] == "g-1"
    assert calendar.events[0]["title"] == "dentist"
    merged = await query_schedule(
        store, calendar,
        datetime(2026, 8, 28, 18, tzinfo=UTC),
        datetime(2026, 8, 28, 21, tzinfo=UTC),
    )
    assert [(block.source, block.title) for block in merged] == [("event", "dentist")]


@pytest.mark.asyncio
async def test_complete_task_removes_calendar_block_and_clears_placement(tmp_path):
    store = Store(tmp_path / "complete.sqlite")
    await store.initialize()
    [task] = await store.add_tasks([{
        "title": "pset", "deadline": datetime.now(UTC) + timedelta(days=1),
        "estimated_minutes": 60,
    }])
    start = datetime.now(UTC) + timedelta(hours=1)
    await store.apply_schedule_decision(
        task["id"], "scheduled", start, start + timedelta(hours=1), None, None,
        "user_request", "the deadline makes this the only one hour gap", [], "work-1",
    )
    calendar = FakeCalendar()
    scheduler = SchedulerEngine(store, calendar, client=object())
    handlers = await build_tool_handlers(store, calendar, scheduler, FactsEngine(store))
    result = await handlers["complete_task"](task_id=task["id"], actual_minutes=55)
    assert result["status"] == "completed"
    assert result["scheduled_start"] is None and result["gcal_event_id"] is None
    assert calendar.deleted == ["work-1"]


@pytest.mark.asyncio
async def test_event_and_deadline_changes_trigger_scheduler(tmp_path):
    store = Store(tmp_path / "triggers.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    scheduler = SpyScheduler()
    handlers = await build_tool_handlers(store, calendar, scheduler, FactsEngine(store))
    await handlers["add_event"](events=[{
        "title": "meeting", "description": None,
        "start": "2026-08-28T19:00:00Z", "end": "2026-08-28T20:00:00Z",
        "location": None, "category": "work",
    }])
    assert len(scheduler.conflicts) == 1

    [task] = await store.add_tasks([{
        "title": "report", "deadline": datetime(2026, 8, 29, tzinfo=UTC),
        "estimated_minutes": 60,
    }])
    start = datetime(2026, 8, 28, 21, tzinfo=UTC)
    await store.apply_schedule_decision(
        task["id"], "scheduled", start, start + timedelta(hours=1), None, None,
        "daily_plan", "the deadline made this the only one hour gap", [], "work-task",
    )
    await handlers["update_task"](
        task_id=task["id"], deadline="2026-08-28T23:00:00Z",
        clear_fields=[], title=None, description=None, estimated_minutes=None,
        category=None, energy=None, priority=None, status=None, goal_id=None,
    )
    assert scheduler.reschedules[0][2] == "deadline_shift"


@pytest.mark.asyncio
async def test_conflicting_event_requires_later_confirmation_and_rechecks(tmp_path):
    store = Store(tmp_path / "proposal.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    scheduler = SpyScheduler()
    handlers = await build_tool_handlers(store, calendar, scheduler, FactsEngine(store))
    start = datetime(2026, 8, 28, 19, tzinfo=UTC)
    await handlers["add_event"](events=[{
        "title": "first", "description": None, "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(), "location": None,
        "category": "work",
    }])
    proposal = await handlers["add_event"](events=[{
        "title": "overlap", "description": None,
        "start": (start + timedelta(minutes=30)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(), "location": None,
        "category": "work",
    }])
    assert proposal["confirmation_required"] is True
    assert len(calendar.events) == 1
    confirmed = await handlers["confirm_event_change"](proposal_id=proposal["proposal_id"])
    assert confirmed["events"][0]["title"] == "overlap"
    assert len(calendar.events) == 2


@pytest.mark.asyncio
async def test_confirmation_reproposes_when_a_new_conflict_appears(tmp_path):
    store = Store(tmp_path / "changed-proposal.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    handlers = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    start = datetime(2026, 8, 28, 19, tzinfo=UTC)
    await handlers["add_event"](events=[{
        "title": "first", "description": None, "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(), "location": None,
        "category": "work",
    }])
    proposal = await handlers["add_event"](events=[{
        "title": "overlap", "description": None,
        "start": (start + timedelta(minutes=30)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(), "location": None,
        "category": "work",
    }])
    calendar.events.append({
        "gcal_event_id": "external", "title": "new", "start_time": start + timedelta(minutes=75),
        "end_time": start + timedelta(minutes=90),
    })
    changed = await handlers["confirm_event_change"](proposal_id=proposal["proposal_id"])
    assert changed["confirmation_required"] is True
    assert len(calendar.events) == 2


@pytest.mark.asyncio
async def test_concurrent_confirmation_claims_only_one_proposal(tmp_path):
    store = Store(tmp_path / "claimed-proposal.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    handlers = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    start = datetime(2026, 8, 28, 19, tzinfo=UTC)
    await handlers["add_event"](events=[{
        "title": "first", "description": None, "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(), "location": None, "category": "work",
    }])
    proposal = await handlers["add_event"](events=[{
        "title": "overlap", "description": None, "start": (start + timedelta(minutes=30)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(), "location": None, "category": "work",
    }])
    one, two = await asyncio.gather(
        handlers["confirm_event_change"](proposal_id=proposal["proposal_id"]),
        handlers["confirm_event_change"](proposal_id=proposal["proposal_id"]),
    )
    assert sum("events" in result for result in (one, two)) == 1
    assert sum(result.get("reason") == "proposal_unavailable" for result in (one, two)) == 1


@pytest.mark.asyncio
async def test_transient_confirmation_failure_releases_claim_for_retry(tmp_path):
    class FlakyCalendar(FakeCalendar):
        def __init__(self):
            super().__init__()
            self.fail_once = True

        async def create_event(self, *args, **kwargs):
            if self.fail_once:
                self.fail_once = False
                raise CalendarError("temporary outage")
            return await super().create_event(*args, **kwargs)

    store = Store(tmp_path / "retry-proposal.sqlite")
    await store.initialize()
    calendar = FlakyCalendar()
    handlers = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    start = datetime(2026, 8, 28, 19, tzinfo=UTC)
    calendar.fail_once = False
    await handlers["add_event"](events=[{
        "title": "first", "description": None, "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(), "location": None, "category": "work",
    }])
    calendar.fail_once = True
    proposal = await handlers["add_event"](events=[{
        "title": "overlap", "description": None, "start": (start + timedelta(minutes=30)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(), "location": None, "category": "work",
    }])
    with pytest.raises(Exception):
        await handlers["confirm_event_change"](proposal_id=proposal["proposal_id"])
    assert (await store.get_event_change_proposal(proposal["proposal_id"]))["claimed_at"] is None
    succeeded = await handlers["confirm_event_change"](proposal_id=proposal["proposal_id"])
    assert succeeded["events"][0]["title"] == "overlap"


@pytest.mark.asyncio
async def test_confirm_finalizes_when_post_commit_reconciliation_fails(tmp_path):
    class FailingScheduler(SpyScheduler):
        async def detect_conflicts(self, start, end):
            raise RuntimeError("reconciliation unavailable")

    store = Store(tmp_path / "reconcile-proposal.sqlite")
    await store.initialize()
    calendar = FakeCalendar()
    # First create the blocking event without invoking the failing reconciler.
    stable = await build_tool_handlers(store, calendar, SpyScheduler(), FactsEngine(store))
    start = datetime(2026, 8, 28, 19, tzinfo=UTC)
    await stable["add_event"](events=[{
        "title": "first", "description": None, "start": start.isoformat(),
        "end": (start + timedelta(hours=1)).isoformat(), "location": None, "category": "work",
    }])
    handlers = await build_tool_handlers(store, calendar, FailingScheduler(), FactsEngine(store))
    proposal = await handlers["add_event"](events=[{
        "title": "overlap", "description": None, "start": (start + timedelta(minutes=30)).isoformat(),
        "end": (start + timedelta(hours=2)).isoformat(), "location": None, "category": "work",
    }])
    applied = await handlers["confirm_event_change"](proposal_id=proposal["proposal_id"])
    assert applied["events"][0]["title"] == "overlap"
    assert applied["schedule_changes"] == []
    assert applied["reconciliation_pending"] is True
    saved = await store.get_event_change_proposal(proposal["proposal_id"])
    assert saved is not None and saved["consumed_at"] is not None
    assert len(await store.query_events(start, start + timedelta(hours=3))) == 2


@pytest.mark.asyncio
async def test_missing_task_estimate_uses_default_and_explicit_estimate_wins(tmp_path):
    store = Store(tmp_path / "estimate.sqlite")
    await store.initialize()
    handlers = await build_tool_handlers(
        store, FakeCalendar(), SpyScheduler(), FactsEngine(store)
    )
    [defaulted] = await handlers["add_task"](tasks=[{
        "title": "outline", "description": None, "deadline": None,
        "estimated_minutes": None, "category": "school", "energy": "deep_focus",
        "priority": "medium", "goal_id": None, "series_key": None,
    }])
    [explicit] = await handlers["add_task"](tasks=[{
        "title": "outline", "description": None, "deadline": None,
        "estimated_minutes": 47, "category": "school", "energy": "deep_focus",
        "priority": "medium", "goal_id": None, "series_key": None,
    }])
    assert defaulted["estimated_minutes"] > 0
    assert defaulted["estimate_source"] == "default"
    assert explicit["estimated_minutes"] == 47
    assert explicit["estimate_source"] == "user"


@pytest.mark.asyncio
async def test_malformed_tool_args_are_returned_to_model(tmp_path):
    store = Store(tmp_path / "malformed.sqlite")
    await store.initialize()
    bad = {"type": "function_call", "call_id": "bad", "name": "add_task", "arguments": "{"}
    client = SimpleNamespace(responses=FakeResponses([
        response([bad]), response([], "that call was malformed, so I left everything alone"),
    ]))
    agent = Agent(History(store), tool_handlers={}, client=client)
    text = await agent.run_tool_loop("add it", "chat")
    assert "malformed" in text
    second_input = client.responses.calls[1]["input"]
    assert any("Tool error" in str(item.get("output")) for item in second_input if isinstance(item, dict))


@pytest.mark.asyncio
async def test_event_confirmation_tool_requires_affirmative_later_message():
    called = 0

    async def confirm_event_change(proposal_id):
        nonlocal called
        called += 1
        return {"applied": True}

    call = {
        "type": "function_call", "call_id": "confirm", "name": "confirm_event_change",
        "arguments": json.dumps({"proposal_id": "proposal"}),
    }
    client = SimpleNamespace(responses=FakeResponses([
        response([call]), response([], "I need an explicit yes"),
    ]))
    agent = Agent(tool_handlers={"confirm_event_change": confirm_event_change}, client=client)
    assert await agent.run_tool_loop("maybe do that", "chat") == "I need an explicit yes"
    assert called == 0


@pytest.mark.asyncio
async def test_google_failure_is_model_visible_not_raised(tmp_path):
    store = Store(tmp_path / "google.sqlite")
    await store.initialize()
    call = {"type": "function_call", "call_id": "g", "name": "add_event", "arguments": "{}"}

    async def offline(**kwargs):
        raise CalendarError("calendar unavailable")

    client = SimpleNamespace(responses=FakeResponses([
        response([call]), response([], "the calendar is offline, so I left it unchanged"),
    ]))
    agent = Agent(History(store), tool_handlers={"add_event": offline}, client=client)
    assert "left it unchanged" in await agent.run_tool_loop("add dinner", "chat")


@pytest.mark.asyncio
async def test_openai_and_locked_database_fail_plainly(tmp_path):
    ApiConnectionError = type("APIConnectionError", (Exception,), {"__module__": "openai"})
    agent = Agent(client=SimpleNamespace(responses=FakeResponses([ApiConnectionError("down")])))
    assert "can’t reach OpenAI" in await agent.run_tool_loop("hello", "chat")

    class LockedHistory:
        store = None

        async def resolve_session(self, session_id):
            raise sqlite3.OperationalError("database is locked")

    locked = Agent(LockedHistory(), client=SimpleNamespace(responses=FakeResponses([])))
    assert "saved data is busy" in await locked.run_tool_loop("hello", "chat")


@pytest.mark.asyncio
async def test_iteration_cap_returns_plain_message():
    calls = [
        response([{
            "type": "function_call", "call_id": f"c-{index}",
            "name": "query_tasks",
            "arguments": '{"status":null,"category":null,"due_before":null,"due_after":null}',
        }])
        for index in range(MAX_MODEL_CALLS)
    ]

    async def query_tasks(**kwargs):
        return []

    agent = Agent(
        tool_handlers={"query_tasks": query_tasks},
        client=SimpleNamespace(responses=FakeResponses(calls)),
    )
    assert "tool-call limit" in await agent.run_tool_loop("loop", "chat")


def test_why_history_is_a_natural_chain_not_json():
    text = TelegramHandler._format_why_history([
        {
            "action": "scheduled", "start": "2026-08-28T20:00:00Z",
            "reasoning": "it was the only 90 minute gap before Friday",
        },
        {
            "action": "moved", "start": "2026-08-28T22:00:00Z",
            "reasoning": "your meeting took the earlier block but this still clears Friday",
        },
    ])
    assert text.startswith("originally scheduled")
    assert "; now moved" in text
    assert "reasoning" not in text.lower() and "{" not in text
