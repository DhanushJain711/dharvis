"""Turn failures must not encourage replaying an already committed action."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent import Agent, AgentTurnResult, MAX_MODEL_CALLS, UNCERTAIN_CHANGES


class APIConnectionError(Exception):
    pass


def response(*calls, text="", status="completed"):
    return {
        "output": list(calls), "output_text": text, "status": status,
    }


def call(name="add_task", arguments='{"tasks": []}', call_id="call-1"):
    return {
        "type": "function_call", "name": name, "arguments": arguments,
        "call_id": call_id,
    }


def agent_for(responses, handlers=None, history=None):
    client = SimpleNamespace(responses=SimpleNamespace(
        create=AsyncMock(side_effect=responses),
    ))
    return Agent(history, tool_handlers=handlers, client=client)


@pytest.mark.asyncio
async def test_model_failure_before_any_action_can_safely_report_unchanged():
    agent = agent_for([APIConnectionError("offline")])
    text = await agent.run_tool_loop("add laundry", "chat")
    assert "didn’t change anything" in text
    assert UNCERTAIN_CHANGES not in text
    assert isinstance(text, str)
    assert isinstance(text, AgentTurnResult)
    assert text.failed and text.retry_safe and not text.mutation_attempted


@pytest.mark.asyncio
async def test_model_failure_after_committed_mutation_warns_before_retry():
    saved = []

    async def add_task(**arguments):
        saved.append(arguments)
        return [{"id": 12}]

    agent = agent_for(
        [response(call()), APIConnectionError("offline")],
        {"add_task": add_task},
    )
    text = await agent.run_tool_loop("add laundry", "chat")
    assert len(saved) == 1
    assert UNCERTAIN_CHANGES in text
    assert "didn’t change anything" not in text
    assert text.failed and text.mutation_attempted and not text.retry_safe


@pytest.mark.asyncio
async def test_handler_failure_after_write_is_still_a_mutation_attempt():
    saved = []

    async def add_task(**arguments):
        saved.append(arguments)
        raise RuntimeError("reconciliation failed after commit")

    agent = agent_for(
        [response(call()), APIConnectionError("offline")],
        {"add_task": add_task},
    )
    assert UNCERTAIN_CHANGES in await agent.run_tool_loop("add laundry", "chat")
    assert len(saved) == 1


@pytest.mark.asyncio
async def test_history_failure_after_mutation_does_not_erase_commit():
    history = SimpleNamespace(
        resolve_session=AsyncMock(return_value=("session", None)),
        load=AsyncMock(return_value=[]),
        to_openai_input=lambda _: [],
        store=SimpleNamespace(query_facts=AsyncMock(return_value=[])),
    )

    async def append(session, role, *args):
        if role == "tool":
            raise RuntimeError("database is locked")

    history.append = append
    handler = AsyncMock(return_value=[{"id": 12}])
    agent = agent_for([response(call())], {"add_task": handler}, history)
    text = await agent.run_tool_loop("add laundry", "chat")
    handler.assert_awaited_once()
    assert "saved data is busy" in text
    assert UNCERTAIN_CHANGES in text
    assert text.failed and text.mutation_attempted and not text.retry_safe


@pytest.mark.asyncio
async def test_blocked_confirmation_does_not_count_as_a_mutation():
    handler = AsyncMock()
    agent = agent_for(
        [response(call("confirm_event_change", '{"proposal_id":"p1"}')),
         APIConnectionError("offline")],
        {"confirm_event_change": handler},
    )
    text = await agent.run_tool_loop("maybe", "chat")
    handler.assert_not_awaited()
    assert UNCERTAIN_CHANGES not in text
    assert text.retry_safe and not text.mutation_attempted


@pytest.mark.asyncio
@pytest.mark.parametrize("name,arguments,wired", [
    ("add_task", "broken JSON", True),
    ("add_task", "[]", True),
    ("add_task", "{}", False),
    ("query_tasks", "{}", True),
])
async def test_failed_parsing_unwired_and_read_only_calls_do_not_mark_mutation(
    name, arguments, wired,
):
    handler = AsyncMock(return_value=[])
    agent = agent_for(
        [response(call(name, arguments)), APIConnectionError("offline")],
        {name: handler} if wired else {},
    )
    assert UNCERTAIN_CHANGES not in await agent.run_tool_loop("check", "chat")


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [
    response(), response(text="   "),
    response(text="partly done", status="incomplete"),
])
async def test_empty_or_incomplete_reply_after_mutation_has_retry_caution(terminal):
    agent = agent_for(
        [response(call()), terminal], {"add_task": AsyncMock(return_value=[])},
    )
    assert UNCERTAIN_CHANGES in await agent.run_tool_loop("add laundry", "chat")


@pytest.mark.asyncio
async def test_iteration_limit_after_mutation_does_not_invite_repeating_it():
    agent = agent_for(
        [response(call(call_id=f"call-{index}")) for index in range(MAX_MODEL_CALLS)],
        {"add_task": AsyncMock(return_value=[])},
    )
    text = await agent.run_tool_loop("add laundry", "chat")
    assert "tool-call limit" in text
    assert UNCERTAIN_CHANGES in text
    assert text.failed and text.mutation_attempted and not text.retry_safe


@pytest.mark.asyncio
async def test_successful_final_reply_is_not_replaced_with_uncertainty():
    agent = agent_for(
        [response(call()), response(text="added")],
        {"add_task": AsyncMock(return_value=[])},
    )
    text = await agent.run_tool_loop("add laundry", "chat")
    assert text == "added"
    assert not text.failed and not text.retry_safe and text.mutation_attempted


@pytest.mark.asyncio
async def test_retry_metadata_is_per_result_not_shared_across_turns():
    agent = agent_for([
        APIConnectionError("offline"), response(text="you're free after class"),
    ])
    first = await agent.run_tool_loop("what's on today?", "chat-1")
    second = await agent.run_tool_loop("what's on today?", "chat-2")
    assert first.retry_safe and first.failed
    assert not second.retry_safe and not second.failed
    assert not first.mutation_attempted and not second.mutation_attempted
