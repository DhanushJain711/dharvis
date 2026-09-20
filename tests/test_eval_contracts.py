from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.freebusy import FreeBlock
from src.scheduler_engine import SchedulerEngine, SchedulingPlanError
from src.tools import TOOLS_BY_NAME


def agent_case(case_id):
    cases = json.loads(
        (Path(__file__).parents[1] / "evals" / "agent_cases.json").read_text()
    )
    return next(case for case in cases if case["id"] == case_id)


def test_agent_eval_has_56_valid_messages_and_all_categories():
    cases = json.loads(
        (Path(__file__).parents[1] / "evals" / "agent_cases.json").read_text()
    )
    assert len(cases) == 56
    counts = Counter(case["category"] for case in cases)
    assert set(counts) == {
        "multi_item", "relative_dates", "ambiguous_references",
        "corrections", "multi_turn_context", "cross_domain_queries",
        "event_colors", "task_consistency",
    }
    assert all(case["message"].strip() for case in cases)
    assert all(set(case["expected"]) <= set(TOOLS_BY_NAME) for case in cases)


@pytest.mark.parametrize("start,end", [
    ("2026-09-21T00:00:00-05:00", "2026-09-22T00:00:00-05:00"),
    ("2026-09-25T18:00:00Z", "2026-09-25T18:30:00Z"),
    ("2026-11-01T00:00:00-05:00", "2026-11-02T00:00:00-06:00"),
])
def test_agent_eval_schedule_fixture_is_owned_and_inside_requested_range(start, end):
    from scripts.eval_agent import RecordingTools

    tools = RecordingTools("context_move_event")
    arguments = {"start": start, "end": end}
    [event] = tools.result("query_schedule", arguments)
    assert event == tools.result("query_schedule", arguments)[0]
    assert event["source"] == "event" and event["source_id"] == "21"
    assert (
        datetime.fromisoformat(start) <= datetime.fromisoformat(event["start"])
        < datetime.fromisoformat(event["end"]) <= datetime.fromisoformat(end)
    )


@pytest.mark.parametrize("start,end", [
    ("2026-09-21T00:00:00", "2026-09-22T00:00:00Z"),
    ("2026-09-22T00:00:00Z", "2026-09-21T00:00:00Z"),
])
def test_agent_eval_schedule_fixture_rejects_invalid_ranges(start, end):
    from scripts.eval_agent import RecordingTools

    with pytest.raises(ValueError):
        RecordingTools().result("query_schedule", {"start": start, "end": end})


def test_agent_eval_prompt_dates_resolver_and_context_fixture_agree(monkeypatch):
    from scripts.eval_agent import EVAL_NOW, RecordingTools, evaluation_time
    from src import timeutil
    from src.agent import Agent
    monkeypatch.setattr(timeutil, "config", SimpleNamespace(USER_TIMEZONE="America/Chicago"))
    original_clock = timeutil.now_local
    with evaluation_time():
        assert timeutil.now_local() == EVAL_NOW
        prompt = Agent(client=object()).build_system_prompt()
        assert "9:00 AM on Sunday, September 20, 2026" in prompt
        resolved = RecordingTools().result("resolve_date", {"phrase": "tomorrow at 2pm"})
        assert resolved["utc"] == "2026-09-21T19:00:00Z"
        [event] = RecordingTools().result("query_schedule", {
            "start": "2026-09-21T00:00:00-05:00", "end": "2026-09-22T00:00:00-05:00",
        })
        assert event["start"] == resolved["utc"]
        context = agent_case("context_schedule")["prior"][0][1]
        assert "2026-09-21T20:00:00Z" in context
    assert timeutil.now_local is original_clock


def test_agent_eval_ambiguous_fact_has_multiple_matches_and_requires_a_question():
    from scripts.eval_agent import RecordingTools, passes_case

    case = agent_case("ambiguous_fact")
    facts = RecordingTools(case["id"]).result("query_facts", {})
    assert {fact["id"] for fact in facts} == {7, 8}
    assert all("gym" in fact["content"] for fact in facts)
    calls = [{"name": "query_facts", "arguments": {}, "result": facts}]
    assert passes_case(case, calls, "Which gym preference: after 9am, or with a partner?")
    assert not passes_case(case, calls, "I found your gym preferences.")
    assert not passes_case(case, calls + [{
        "name": "update_fact", "arguments": {"fact_id": 7, "active": False},
    }], "I've removed that preference. Which one did you mean?")


def test_quick_timed_call_eval_expects_a_reminder():
    from scripts.eval_agent import matches

    case = agent_case("relative_monday_evening")
    assert matches(case, ["resolve_date", "add_reminder"])
    assert not matches(case, ["resolve_date", "add_task"])


@pytest.mark.asyncio
async def test_agent_eval_records_parse_failure_and_accepts_specific_time_question():
    from scripts.eval_agent import RecordingTools, passes_case
    from src.agent import AgentTurnResult

    case = agent_case("relative_later_today")
    tools = RecordingTools(case["id"])
    with pytest.raises(ValueError, match="Could not resolve relative date phrase"):
        await tools.handlers()["resolve_date"](phrase="later today")
    calls = tools.call_details
    assert calls[0]["error"]["type"] == "ValueError"
    assert passes_case(case, calls, "What time later today should I remind you?")
    assert not passes_case(case, calls, "Couldn't resolve that.")
    assert not passes_case(case, calls, "Done — reminder added.")
    assert not passes_case(case, calls, AgentTurnResult("What time?", failed=True))
    assert not passes_case(case, calls + [{
        "name": "add_reminder", "arguments": {"reminders": []}, "result": [],
    }], "What time should I remind you? I've added it for now.")
    assert not passes_case(case, [{**calls[0], "error": {
        "type": "ConnectionError", "message": "network unavailable",
    }}], "What time should I remind you?")


def test_agent_eval_resolved_creation_must_include_successful_tool_calls():
    from scripts.eval_agent import passes_case

    case = agent_case("relative_later_today")
    calls = [
        {"name": "resolve_date", "arguments": {"phrase": "today at 5pm"},
         "result": {"utc": "2026-09-20T22:00:00Z"}},
        {"name": "add_reminder", "arguments": {"reminders": [
            {"message": "stretch", "remind_at": "2026-09-20T22:00:00Z"},
        ]}, "result": [{"id": 101}]},
    ]
    assert passes_case(case, calls, "I'll remind you at 5.")
    for index in range(len(calls)):
        failed_calls = [dict(call) for call in calls]
        failed_calls[index]["error"] = {"type": "RuntimeError", "message": "tool failed"}
        assert not passes_case(case, failed_calls, "I'll remind you at 5.")


@pytest.mark.asyncio
async def test_agent_eval_run_case_rejects_a_typed_failed_turn(monkeypatch, tmp_path):
    from scripts import eval_agent
    from src.agent import AgentTurnResult

    class FailedAgent:
        def __init__(self, history, tool_handlers):
            pass

        async def run_tool_loop(self, message, conversation):
            return AgentTurnResult("I couldn't finish that response.", failed=True)

    monkeypatch.setattr(eval_agent, "Agent", FailedAgent)
    result = await eval_agent.run_case(agent_case("ambiguous_it"), tmp_path)
    assert result["observed"] == []
    assert result["turn_failed"] and not result["passed"]
    assert result["reference_time"] == eval_agent.EVAL_NOW.isoformat()


def test_progress_evals_reject_wrong_task_and_completion_instead_of_progress():
    from scripts.eval_agent import matches, matches_arguments

    cases = json.loads(
        (Path(__file__).parents[1] / "evals" / "agent_cases.json").read_text()
    )
    case = next(case for case in cases if case["id"] == "progress_additional")
    assert matches(case, ["query_tasks", "log_task_progress"])
    assert not matches(case, ["log_task_progress", "add_task"])
    assert not matches(case, ["complete_task"])
    call = {"name": "log_task_progress", "arguments": {
        "task_id": 12, "total_minutes": 90, "remaining_minutes": None,
    }}
    assert matches_arguments(case, [call])
    assert not matches_arguments(case, [{**call, "arguments": {
        **call["arguments"], "task_id": 13,
    }}])
    assert not matches_arguments(case, [{**call, "arguments": {
        **call["arguments"], "total_minutes": 30,
    }}])


@pytest.mark.parametrize("text", [
    "your strongest behavior signal was planning behavior",
    "the fixed events set the shape of the day",
    "behavior pattern emerging from weekly checkins",
    "dentist is at 2026-09-20T19:00:00Z",
    "class starts at 19:00 UTC",
])
def test_tone_audit_rejects_reported_robotic_phrases_and_raw_times(text):
    from scripts.audit_tone import violations

    assert violations(text, False)


def test_tone_audit_accepts_a_concrete_local_time_and_natural_reason():
    from scripts.audit_tone import violations

    assert violations("put the pset at 3:30pm — that's your gap before class", True) == []


def test_tone_audit_does_not_count_bold_heading_as_a_bullet():
    from scripts.audit_tone import violations

    response = "**Monday:** calendar looks clear.\n\n- **Due:** pset report — ~90 min\n- **No scheduled events**"
    assert violations(response, False) == ["short bullet list"]
    assert violations("**Monday:** calendar looks clear.", False) == []
    assert violations("**Monday:**\n- dentist\n* pset\n• laundry", False) == []


def test_tone_audit_rejects_a_typed_failed_turn_even_with_clean_wording():
    from scripts.audit_tone import violations
    from src.agent import AgentTurnResult

    assert violations(AgentTurnResult("Couldn't finish that.", failed=True), False) == [
        "agent turn failed",
    ]


def test_scheduler_rejects_generic_rationale_even_when_slot_fits():
    engine = SchedulerEngine(None, None, client=object())
    start = datetime(2026, 8, 28, 14, tzinfo=UTC)
    task = {
        "id": 1, "title": "pset", "deadline": start + timedelta(hours=3),
        "estimated_minutes": 60, "energy": "deep_focus", "priority": "high",
        "category": "school", "goal_id": None,
    }
    block = FreeBlock(start, start + timedelta(hours=2), None, "class")
    raw = {"assignments": [{
        "task_id": 1, "block_id": "block", "reasoning": "this is a good slot",
        "facts_used": [],
    }]}
    with pytest.raises(SchedulingPlanError, match="true constraint"):
        engine._pack_and_validate(raw, [task], [("block", block)], [], [])


def test_scheduler_accepts_hyphenated_duration_constraint():
    engine = SchedulerEngine(None, None, client=object())
    start = datetime(2026, 8, 28, 14, tzinfo=UTC)
    task = {
        "id": 1, "title": "pset", "deadline": start + timedelta(hours=3),
        "estimated_minutes": 30, "energy": "deep_focus", "priority": "high",
        "category": "school", "goal_id": None,
    }
    block = FreeBlock(start, start + timedelta(hours=1), None, None)
    raw = {"assignments": [{
        "task_id": 1, "block_id": "block",
        "reasoning": "the 30-minute task fits inside the 60-minute block",
        "facts_used": [],
    }]}

    placements = engine._pack_and_validate(raw, [task], [("block", block)], [], [])

    assert len(placements) == 1


def test_few_shot_assistant_lines_have_no_corporate_phrases_or_reasoning_label():
    prompt = (Path(__file__).parents[1] / "src" / "prompts" / "system.md").read_text()
    assistant_lines = [
        line.removeprefix("Assistant:").strip()
        for line in prompt.splitlines() if line.startswith("Assistant:")
    ]
    assert 8 <= len(assistant_lines) <= 10
    joined = "\n".join(assistant_lines).lower()
    for phrase in (
        "certainly", "i've gone ahead", "let me know if", "reasoning:",
    ):
        assert phrase not in joined
    schedule_examples = [line for line in assistant_lines if line.startswith(("put ", "moved "))]
    assert len(schedule_examples) >= 3
    assert all("—" in line for line in schedule_examples)


def test_system_prompt_distinguishes_event_and_calendar_colors() -> None:
    prompt = (Path(__file__).parents[1] / "src" / "prompts" / "system.md").read_text()
    assert "metadata.color_id" in prompt
    assert "never copy `metadata.calendar_color_id`" in prompt
    assert "external events themselves are read-only" in prompt
    assert "6 tangerine/orange" in prompt
    assert "clear `color_id`" in prompt
