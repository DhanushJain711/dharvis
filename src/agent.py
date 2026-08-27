"""Stateful OpenAI Responses agent with concurrent function-tool execution."""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from .costs import estimated_cost as _shared_estimated_cost
from .costs import usage_numbers as _shared_usage_numbers
from .config import config
from .history import History
from .timeutil import format_time_context
from .tools import TOOLS, TOOLS_BY_NAME

logger = logging.getLogger(__name__)

ToolHandler = Callable[..., Any | Awaitable[Any]]

AGENT_MODEL = "gpt-5.6-terra"
SUMMARY_MODEL = "gpt-5.6-luna"
MAX_MODEL_CALLS = 8
HISTORY_LIMIT = 20
PROMPT_CACHE_KEY = "dharvis-agent-core-v1"


def _read_system_prompt() -> str:
    path = Path(__file__).with_name("prompts") / "system.md"
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        # Keep imports and startup safe while packaging or partial deployments
        # are being repaired; a real deployment should always ship system.md.
        return "You are Dharvis, a personal scheduling assistant for one user."


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude_none=True)
    raise TypeError(f"cannot serialize response item {type(value).__name__}")


def _field(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _json_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return str(value)


def _result_summary(value: Any, *, limit: int = 240) -> str:
    text = " ".join(_json_text(value).split())
    return text if len(text) <= limit else f"{text[: limit - 1]}…"


def _response_tools() -> list[dict[str, Any]]:
    """Flatten canonical Chat-style schemas only at the Responses boundary."""
    flattened: list[dict[str, Any]] = []
    for tool in TOOLS:
        function = tool["function"]
        flattened.append(
            {
                "type": "function",
                "name": function["name"],
                "description": function["description"],
                "parameters": function["parameters"],
                "strict": function["strict"],
            }
        )
    return flattened


def _usage_numbers(response: Any) -> dict[str, int]:
    return _shared_usage_numbers(response)


def _estimated_cost(model: str, usage: Mapping[str, int]) -> float | None:
    return _shared_estimated_cost(model, usage)


def _terminal_failure_text(response: Any, *, partial_text: str = "") -> str:
    """Describe an empty terminal response without implying an action succeeded."""
    for item in _field(response, "output", []) or []:
        if _field(item, "type") != "message":
            continue
        for content in _field(item, "content", []) or []:
            if _field(content, "type") == "refusal":
                refusal = str(_field(content, "refusal", "") or "").strip()
                if refusal:
                    return refusal

    refusal = str(_field(response, "refusal", "") or "").strip()
    if refusal:
        return refusal
    if _field(response, "error"):
        return "I couldn't finish that response because the model returned an error."
    if _field(response, "status") == "incomplete":
        details = _field(response, "incomplete_details", {}) or {}
        reason = str(_field(details, "reason", "") or "").strip()
        suffix = f" ({reason.replace('_', ' ')})" if reason else ""
        message = f"I couldn't finish that response before the model stopped{suffix}."
        partial = _result_summary(partial_text) if partial_text.strip() else ""
        return f"{message} Partial response: {partial}" if partial else message
    status = str(_field(response, "status", "") or "").strip()
    if status and status != "completed":
        return f"I couldn't finish that response (model status: {status})."
    return "I couldn't generate a reply for that turn."


class Agent:
    """Run a stateful, bounded Responses API tool loop.

    Tool implementations are injected by name. This module owns orchestration,
    not data, calendar, or scheduling business logic.
    """

    def __init__(
        self,
        history: History | None = None,
        *,
        tool_handlers: Mapping[str, ToolHandler] | None = None,
        client: Any | None = None,
        model: str | None = None,
        summary_model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.history = history
        self.tools = TOOLS
        self.tool_handlers = dict(tool_handlers or {})
        self._client = client
        self.model = model or config.AGENT_MODEL_ID or AGENT_MODEL
        self.summary_model = summary_model or config.SUMMARY_MODEL_ID or SUMMARY_MODEL
        requested_effort = reasoning_effort or os.getenv(
            "OPENAI_REASONING_EFFORT", config.OPENAI_REASONING_EFFORT
        )
        self.reasoning_effort = (
            requested_effort
            if requested_effort
            in {"none", "low", "medium", "high", "xhigh", "max"}
            else "medium"
        )
        self._conversation_locks: dict[str, asyncio.Lock] = {}

    @property
    def client(self) -> Any:
        """Construct the network client only when the first turn needs it."""
        if self._client is None:
            from openai import AsyncOpenAI

            self._client = AsyncOpenAI()
        return self._client

    def build_system_prompt(self) -> str:
        """Build a runnable prompt with an empty facts block and current time."""
        return self._assemble_instructions([])

    def _assemble_instructions(self, facts: Sequence[Mapping[str, Any]]) -> str:
        facts_block = self._facts_block(facts)
        return f"{_read_system_prompt()}\n\n{facts_block}\n\n{format_time_context()}"

    @staticmethod
    def _facts_block(facts: Sequence[Mapping[str, Any]]) -> str:
        sorted_facts = sorted(
            facts,
            key=lambda fact: (
                str(fact.get("category") or ""),
                int(fact.get("id") or 0),
                str(fact.get("content") or ""),
            ),
        )
        if sorted_facts:
            fact_lines = [
                "- [fact {id}] ({category}, confidence {confidence}) {content}".format(
                    id=fact.get("id", "?"),
                    category=fact.get("category", "uncategorized"),
                    confidence=fact.get("confidence", "unknown"),
                    content=fact.get("content", ""),
                )
                for fact in sorted_facts
            ]
            facts_block = "Known facts about the user:\n" + "\n".join(fact_lines)
        else:
            facts_block = "Known facts about the user:\n- none yet"
        return facts_block

    def _system_input(self, facts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
        """Put the explicit cache boundary before all dynamic prompt content."""
        return {
            "role": "system",
            "content": [
                {
                    "type": "input_text",
                    "text": _read_system_prompt(),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {
                    "type": "input_text",
                    "text": self._facts_block(facts),
                    "prompt_cache_breakpoint": {"mode": "explicit"},
                },
                {"type": "input_text", "text": format_time_context()},
            ],
        }

    async def _load_facts(self) -> list[dict[str, Any]]:
        if self.history is None:
            return []
        facts = await self.history.store.query_facts(active=True)
        return [fact for fact in facts if isinstance(fact, dict)]

    async def respond(self, message: str, session_id: str) -> str:
        """Delegate a user turn to the full tool loop."""
        return await self.run_tool_loop(message, session_id)

    async def execute_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        """Dispatch one call to an injected sync or async handler."""
        if name not in TOOLS_BY_NAME:
            raise ValueError(f"unknown tool: {name}")
        handler = self.tool_handlers.get(name)
        if handler is None:
            raise RuntimeError(f"tool is not wired: {name}")
        async_call = inspect.iscoroutinefunction(handler) or inspect.iscoroutinefunction(
            getattr(handler, "__call__", None)
        )
        if async_call:
            result = handler(**arguments)
        else:
            result = await asyncio.to_thread(handler, **arguments)
        if inspect.isawaitable(result):
            return await result
        return result

    async def _execute_call(self, call: Any) -> tuple[str, str]:
        name = str(_field(call, "name", ""))
        call_id = str(_field(call, "call_id", ""))
        raw_arguments = _field(call, "arguments", "{}")
        started = time.perf_counter()
        ok = False
        logged_args: Any = raw_arguments
        try:
            if not call_id:
                raise ValueError("tool call is missing call_id")
            if not name:
                raise ValueError("tool call is missing name")
            if isinstance(raw_arguments, str):
                arguments = json.loads(raw_arguments)
            elif isinstance(raw_arguments, dict):
                arguments = raw_arguments
            else:
                raise TypeError("tool arguments must be a JSON object")
            if not isinstance(arguments, dict):
                raise TypeError("tool arguments must decode to a JSON object")
            logged_args = arguments
            result = await self.execute_tool(name, arguments)
            output = _json_text(result)
            ok = True
        except Exception as exc:  # Tool failures are model-visible recovery input.
            output = f"Tool error ({type(exc).__name__}): {exc}"
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        logger.info(
            json.dumps(
                {
                    "event": "agent_tool_call",
                    "name": name,
                    "args": logged_args,
                    "duration_ms": duration_ms,
                    "ok": ok,
                    "result_summary": _result_summary(output),
                },
                ensure_ascii=False,
                default=str,
                separators=(",", ":"),
            )
        )
        return call_id, output

    async def _summarize_session(
        self, messages: list[dict[str, Any]]
    ) -> tuple[str, dict[str, int], float | None]:
        transcript = "\n".join(
            f"{item.get('role', 'unknown')}: {item.get('content', '')}"
            for item in messages
            if item.get("content")
        )
        response = await self.client.responses.create(
            model=self.summary_model,
            instructions=(
                "Summarize the previous personal-assistant session in exactly one "
                "short plain-text line. Preserve commitments, unresolved questions, "
                "and user preferences. Do not add a label or bullet."
            ),
            input=transcript or "No textual messages were recorded.",
            store=False,
            prompt_cache_key=f"{PROMPT_CACHE_KEY}-summary",
            text={"verbosity": "low"},
        )
        summary = " ".join(str(_field(response, "output_text", "")).split())
        if not summary:
            summary = "The previous session had no durable conversational context."
        usage = _usage_numbers(response)
        return summary, usage, _estimated_cost(self.summary_model, usage)

    async def _record_usage(
        self,
        component: str,
        model: str,
        usage: Mapping[str, int],
        cost: float | None,
        session_id: str,
    ) -> None:
        recorder = getattr(getattr(self.history, "store", None), "record_usage", None)
        if not callable(recorder):
            return
        try:
            await recorder(component, model, dict(usage), cost, session_id)
        except Exception:
            logger.exception("usage_persistence_failed")

    @staticmethod
    def _friendly_failure(exc: Exception) -> str:
        module = type(exc).__module__.lower()
        name = type(exc).__name__.lower()
        message = str(exc).lower()
        if "openai" in module or any(
            token in name for token in ("ratelimit", "apiconnection", "apitimeout")
        ):
            return "I can’t reach OpenAI right now, so I didn’t change anything. Try again in a minute."
        if "locked" in message or "sqlite" in module:
            return "My saved data is busy right now, so I didn’t change anything. Try that again in a moment."
        if "calendar" in module or "google" in module:
            return "I can’t reach the calendar right now, so I left it unchanged."
        return "Something broke before I could finish that, so I didn’t claim it was done."

    @staticmethod
    def _add_usage(total: dict[str, int], addition: Mapping[str, int]) -> None:
        for key in total:
            total[key] += int(addition.get(key, 0))

    def _log_turn(
        self,
        *,
        conversation_id: str,
        iterations: int,
        tool_calls: int,
        started: float,
        usage: Mapping[str, int],
        estimated_cost: float | None,
    ) -> None:
        logger.info(
            json.dumps(
                {
                    "event": "agent_turn_usage",
                    "conversation_id": conversation_id,
                    "iterations": iterations,
                    "tool_calls": tool_calls,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                    "input_tokens": usage["input_tokens"],
                    "cached_tokens": usage["cached_tokens"],
                    "cache_write_tokens": usage["cache_write_tokens"],
                    "output_tokens": usage["output_tokens"],
                    "reasoning_tokens": usage["reasoning_tokens"],
                    "total_tokens": usage["total_tokens"],
                    "estimated_cost_usd": (
                        round(estimated_cost, 8) if estimated_cost is not None else None
                    ),
                },
                separators=(",", ":"),
            )
        )

    async def run_tool_loop(self, message: str, session_id: str) -> str:
        """Run at most eight model calls and persist the complete turn."""
        if not isinstance(message, str) or not message.strip():
            raise ValueError("message must contain text")
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a stable conversation id")

        lock = self._conversation_locks.setdefault(session_id, asyncio.Lock())
        async with lock:
            turn_started = time.perf_counter()
            usage_total = {
                "input_tokens": 0,
                "cached_tokens": 0,
                "cache_write_tokens": 0,
                "output_tokens": 0,
                "reasoning_tokens": 0,
                "total_tokens": 0,
            }
            cost_total: float | None = 0.0
            iterations = 0
            tool_call_count = 0
            try:
                physical_session = session_id
                history_messages: list[dict[str, Any]] = []
                if self.history is not None:
                    physical_session, expired_session = await self.history.resolve_session(
                        session_id
                    )
                    if expired_session is not None:
                        previous = await self.history.load(expired_session, HISTORY_LIMIT)
                        summary, summary_usage, summary_cost = await self._summarize_session(
                            previous
                        )
                        await self._record_usage(
                            "session_summary", self.summary_model, summary_usage,
                            summary_cost, physical_session,
                        )
                        self._add_usage(usage_total, summary_usage)
                        if cost_total is not None:
                            cost_total = (
                                cost_total + summary_cost
                                if summary_cost is not None
                                else None
                            )
                        await self.history.append(
                            physical_session,
                            "assistant",
                            f"Previous session: {summary}",
                        )
                    history_messages = await self.history.load(
                        physical_session, HISTORY_LIMIT
                    )
                    await self.history.append(
                        physical_session, "user", message
                    )

                input_items = (
                    self.history.to_openai_input(history_messages)
                    if self.history is not None
                    else []
                )
                facts = await self._load_facts()
                input_items.insert(0, self._system_input(facts))
                input_items.append({"role": "user", "content": message})

                for iterations in range(1, MAX_MODEL_CALLS + 1):
                    response = await self.client.responses.create(
                        model=self.model,
                        input=input_items,
                        tools=_response_tools(),
                        tool_choice="auto",
                        parallel_tool_calls=True,
                        store=False,
                        include=["reasoning.encrypted_content"],
                        prompt_cache_key=PROMPT_CACHE_KEY,
                        prompt_cache_options={"mode": "explicit"},
                        reasoning={"effort": self.reasoning_effort},
                        text={"verbosity": "low"},
                    )
                    response_usage = _usage_numbers(response)
                    self._add_usage(usage_total, response_usage)
                    response_cost = _estimated_cost(self.model, response_usage)
                    await self._record_usage(
                        "agent_loop", self.model, response_usage, response_cost,
                        physical_session,
                    )
                    if cost_total is not None:
                        cost_total = (
                            cost_total + response_cost
                            if response_cost is not None
                            else None
                        )

                    serialized_output = [
                        _as_dict(item) for item in (_field(response, "output", []) or [])
                    ]
                    output_text = str(_field(response, "output_text", "") or "")
                    calls = [
                        item
                        for item in (_field(response, "output", []) or [])
                        if _field(item, "type") == "function_call"
                    ]

                    # Persist the assistant's call metadata before any handler can
                    # create an external side effect.
                    response_status = _field(response, "status")
                    noncompleted_terminal = (
                        not calls
                        and response_status is not None
                        and response_status != "completed"
                    )
                    terminal_text = (
                        _terminal_failure_text(response, partial_text=output_text)
                        if noncompleted_terminal
                        else output_text
                        if calls or output_text
                        else _terminal_failure_text(response)
                    )
                    if self.history is not None:
                        if noncompleted_terminal:
                            if serialized_output:
                                await self.history.append(
                                    physical_session,
                                    "assistant",
                                    output_text,
                                    serialized_output,
                                )
                            await self.history.append(
                                physical_session, "assistant", terminal_text
                            )
                        else:
                            await self.history.append(
                                physical_session,
                                "assistant",
                                terminal_text,
                                serialized_output,
                            )
                    # Responses reasoning items must be fed back along with calls.
                    input_items.extend(serialized_output)

                    if not calls:
                        return terminal_text

                    tool_call_count += len(calls)
                    results = await asyncio.gather(
                        *(self._execute_call(call) for call in calls)
                    )
                    for call_id, output in results:
                        output_item = {
                            "type": "function_call_output",
                            "call_id": call_id,
                            "output": output,
                        }
                        input_items.append(output_item)
                        if self.history is not None:
                            await self.history.append(
                                physical_session,
                                "tool",
                                output,
                                [output_item],
                            )

                final_text = (
                    "I hit the tool-call limit before I could finish that cleanly."
                )
                if self.history is not None:
                    await self.history.append(
                        physical_session, "assistant", final_text
                    )
                return final_text
            except Exception as exc:
                logger.exception(
                    "agent_turn_failed",
                    extra={"failure_type": type(exc).__name__, "conversation_id": session_id},
                )
                return self._friendly_failure(exc)
            finally:
                self._log_turn(
                    conversation_id=session_id,
                    iterations=iterations,
                    tool_calls=tool_call_count,
                    started=turn_started,
                    usage=usage_total,
                    estimated_cost=cost_total,
                )


async def create_agent(
    history: History | None = None,
    *,
    tool_handlers: Mapping[str, ToolHandler] | None = None,
    client: Any | None = None,
) -> Agent:
    """Create an agent without opening a network connection."""
    return Agent(history, tool_handlers=tool_handlers, client=client)
