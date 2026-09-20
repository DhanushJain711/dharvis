#!/usr/bin/env python3
"""Run the prompt/tool-sequence regression set without real side effects."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

from src import timeutil
from src.agent import Agent
from src.config import config
from src.history import History
from src.store import Store
from src.tools import TOOLS_BY_NAME

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "agent_cases.json"
# Freeze only evaluation time, including the production prompt's time context.
# Keep absolute dates in agent_cases.json aligned with this reference instant.
EVAL_NOW = datetime(2026, 9, 20, 14, tzinfo=UTC)
SAFE_EXTRA_TOOLS = {
    "resolve_date", "query_schedule", "query_tasks", "find_free_blocks",
    "query_facts", "query_goals", "query_reminders", "explain_schedule",
}


@contextmanager
def evaluation_time():
    """Use the same aware clock for model context and relative-date tools."""
    with patch("src.timeutil.now_local", return_value=timeutil.to_local(EVAL_NOW)):
        yield


class RecordingTools:
    def __init__(self, case_id: str = "") -> None:
        self.case_id = case_id
        self.calls: list[str] = []
        self.call_details: list[dict[str, Any]] = []
        self.next_id = 100

    def handlers(self) -> dict[str, Any]:
        handlers: dict[str, Any] = {}
        for name in TOOLS_BY_NAME:
            async def handler(_name: str = name, **arguments: Any) -> Any:
                self.calls.append(_name)
                detail: dict[str, Any] = {"name": _name, "arguments": arguments}
                self.call_details.append(detail)
                self.next_id += 1
                try:
                    result = self.result(_name, arguments)
                except Exception as exc:
                    detail["error"] = {
                        "type": type(exc).__name__, "message": str(exc),
                    }
                    raise
                detail["result"] = result
                return result
            handlers[name] = handler
        return handlers

    def schedule_times(self, arguments: dict[str, Any]) -> dict[str, str]:
        """Return an owned fixture interval inside the requested aware range."""
        start = timeutil.to_utc(datetime.fromisoformat(arguments["start"].replace("Z", "+00:00")))
        end = timeutil.to_utc(datetime.fromisoformat(arguments["end"].replace("Z", "+00:00")))
        if end <= start:
            raise ValueError("schedule range end must follow start")
        tomorrow = timeutil.to_utc(timeutil.resolve_relative("tomorrow at 2pm", ref=EVAL_NOW))
        # Prefer the appointment described in the multi-turn fixtures. Other
        # ranges receive an in-range occurrence instead of an unrelated date.
        preferred = tomorrow if start <= tomorrow < end else timeutil.to_utc(
            timeutil.to_local(start).replace(hour=14, minute=0, second=0, microsecond=0)
        )
        event_start = preferred if start <= preferred < end else start
        event_end = min(event_start + timedelta(hours=1), end)
        return {
            "start": event_start.isoformat().replace("+00:00", "Z"),
            "end": event_end.isoformat().replace("+00:00", "Z"),
        }

    def result(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "resolve_date":
            local = timeutil.resolve_relative(str(arguments.get("phrase", "")), ref=EVAL_NOW)
            return {
                "phrase": arguments.get("phrase"),
                "local": local.isoformat(),
                "utc": timeutil.to_utc(local).isoformat().replace("+00:00", "Z"),
                "timezone": config.USER_TIMEZONE,
            }
        if name == "query_tasks":
            if self.case_id.startswith("progress_") or self.case_id == "completion_retry":
                return [{
                    "id": 12, "title": "math pset", "status": (
                        "completed" if self.case_id == "completion_retry" else "pending"
                    ),
                    "estimated_minutes": 90,
                    "progress_minutes": {
                        "progress_additional": 60,
                        "progress_remaining": 30,
                    }.get(self.case_id, 0),
                    "actual_minutes": 90 if self.case_id == "completion_retry" else None,
                }]
            return [{"id": 12, "title": "pset report", "status": "pending", "estimated_minutes": 90}]
        if name == "query_schedule":
            times = self.schedule_times(arguments)
            if self.case_id == "color_match_reference":
                return [{
                    "source": "gcal", "source_id": "cs311", "title": "CS311 lecture",
                    **times,
                    "metadata": {"color_id": "6", "calendar_color_id": "9"},
                }]
            if self.case_id == "color_inherited_reference":
                return [{
                    "source": "gcal", "source_id": "canvas", "title": "CS311 lecture",
                    **times,
                    "metadata": {"color_id": None, "calendar_color_id": "9"},
                }]
            if self.case_id == "color_conflicting_references":
                return [
                    {
                        "source": "gcal", "source_id": "cs311-a", "title": "CS311 lecture",
                        **times,
                        "metadata": {"color_id": "6", "calendar_color_id": "9"},
                    },
                    {
                        "source": "gcal", "source_id": "cs311-b", "title": "CS311 review",
                        **times,
                        "metadata": {"color_id": "3", "calendar_color_id": "9"},
                    },
                ]
            if self.case_id == "color_external_refusal":
                return [{
                    "source": "gcal", "source_id": "canvas", "title": "Canvas lecture",
                    **times,
                    "metadata": {"color_id": "6", "calendar_access_role": "reader"},
                }]
            return [{"source": "event", "source_id": "21", "title": "dentist", **times}]
        if name == "find_free_blocks":
            return [{
                "start": arguments.get("start"), "end": arguments.get("end"),
                "before": "dinner",
            }]
        if name == "query_facts":
            if self.case_id == "ambiguous_fact":
                return [
                    {"id": 7, "content": "prefers gym workouts after 9am", "active": True},
                    {"id": 8, "content": "prefers gym workouts with a partner", "active": True},
                ]
            return [{"id": 7, "content": "prefers workouts after 9am", "active": True}]
        if name == "query_goals":
            return [{"id": 5, "title": "gym", "target_amount": 3, "progress": {"amount_remaining": 1}}]
        if name == "explain_schedule":
            return [{"task_id": 12, "reasoning": "the Friday deadline made this the only 90 minute gap"}]
        if name == "log_task_progress":
            return {
                "id": arguments.get("task_id"), "title": "math pset",
                "status": "pending", "progress_minutes": arguments.get("total_minutes"),
                "estimated_minutes": arguments.get("remaining_minutes") or 90,
            }
        if name.startswith("add_"):
            return [{"id": self.next_id, "created": True}]
        return {"ok": True, "id": arguments.get("task_id", arguments.get("event_id"))}


def matches(case: dict[str, Any], observed: list[str]) -> bool:
    """Match required calls while permitting additional read-only verification."""
    expected = list(case["expected"])
    safe_extras = SAFE_EXTRA_TOOLS | set(case.get("optional_tools", []))
    if case.get("unordered"):
        remaining = list(observed)
        for name in expected:
            if name not in remaining:
                return False
            remaining.remove(name)
        return all(name in safe_extras for name in remaining)

    position = 0
    extras: list[str] = []
    for name in observed:
        if position < len(expected) and name == expected[position]:
            position += 1
        else:
            extras.append(name)
    return position == len(expected) and all(name in safe_extras for name in extras)


def matches_arguments(case: dict[str, Any], calls: list[dict[str, Any]]) -> bool:
    """Check critical state values, not just that a plausible tool was called."""
    for expected in case.get("expected_arguments", []):
        matching = [call for call in calls if call["name"] == expected["name"]]
        if not matching and expected.get("optional"):
            continue
        if not matching:
            return False
        for call in matching:
            if any(
                key not in call["arguments"] or call["arguments"][key] != value
                for key, value in expected["arguments"].items()
            ):
                return False
    return True


def passes_case(
    case: dict[str, Any], calls: list[dict[str, Any]], response: str,
    error: str | None = None,
) -> bool:
    """Score successful actions or an explicitly permitted clarification.

    Attempted tools do not prove success. A failed date parse may legitimately
    lead to a specific time question, but an outage or a claimed write cannot.
    """
    if error is not None or getattr(response, "failed", False) or not response.strip():
        return False
    observed = [call["name"] for call in calls]
    succeeded = [call["name"] for call in calls if "error" not in call]
    if case.get("allow_time_clarification") and "?" in response and re.search(
        r"\b(?:what|which)\s+time\b|\bwhen\b", response, re.IGNORECASE,
    ):
        failures = [call for call in calls if "error" in call]
        expected_parse_failure = failures and all(
            call["name"] == "resolve_date"
            and call["arguments"].get("phrase", "").strip().lower() == "later today"
            and call["error"]["type"] == "ValueError"
            and "Could not resolve relative date phrase" in call["error"]["message"]
            for call in failures
        )
        if expected_parse_failure and all(name in SAFE_EXTRA_TOOLS for name in observed):
            return True
    if case.get("requires_clarification") and "?" not in response:
        return False
    return (
        matches(case, observed) and matches(case, succeeded)
        and matches_arguments(case, calls)
    )


async def run_case(case: dict[str, Any], root: Path) -> dict[str, Any]:
    store = Store(root / f"{case['id']}.sqlite")
    await store.initialize()
    history = History(store)
    recorder = RecordingTools(case["id"])
    agent = Agent(history, tool_handlers=recorder.handlers())
    conversation = f"eval-{case['id']}"
    session, _ = await history.resolve_session(conversation)
    for role, content in case.get("prior", []):
        await history.append(session, role, content)
    try:
        with evaluation_time():
            response = await agent.run_tool_loop(case["message"], conversation)
        error = None
    except Exception as exc:  # The report must retain failures rather than stop.
        response, error = "", f"{type(exc).__name__}: {exc}"
    now = datetime.now(UTC)
    usage_rows = await store.usage_summary(now - timedelta(days=1), now + timedelta(days=1))
    usage = next((row for row in usage_rows if row["kind"] == "agent"), {})
    return {
        "id": case["id"], "category": case["category"],
        "message": case["message"], "expected": case["expected"],
        "observed": recorder.calls,
        "passed": passes_case(case, recorder.call_details, response, error),
        "turn_failed": bool(getattr(response, "failed", False)),
        "reference_time": EVAL_NOW.isoformat(),
        "call_details": recorder.call_details, "response": response, "error": error,
        "usage": usage,
    }


async def main_async(report_path: Path) -> int:
    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY is required for the live agent eval", flush=True)
        return 2
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    results: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="dharvis-eval-") as directory:
        root = Path(directory)
        for index, case in enumerate(cases, 1):
            result = await run_case(case, root)
            results.append(result)
            mark = "PASS" if result["passed"] else "FAIL"
            print(f"[{index:02d}/{len(cases)}] {mark} {case['id']}: {result['observed']}", flush=True)
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for result in results:
        grouped[result["category"]].append(result)
    summary = {
        category: {
            "passed": sum(item["passed"] for item in items),
            "total": len(items),
            "rate": round(sum(item["passed"] for item in items) / len(items), 4),
        }
        for category, items in grouped.items()
    }
    usage_fields = (
        "input_tokens", "cached_tokens", "cache_write_tokens", "output_tokens",
        "total_tokens", "estimated_cost_usd", "calls",
    )
    usage = {
        field: sum((item["usage"].get(field, 0) or 0) for item in results)
        for field in usage_fields
    }
    usage["cache_hit_rate"] = round(
        usage["cached_tokens"] / usage["input_tokens"], 4
    ) if usage["input_tokens"] else 0.0
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({
            "reference_time": EVAL_NOW.isoformat(), "timezone": config.USER_TIMEZONE,
            "summary": summary, "usage": usage, "results": results,
        }, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
    print(json.dumps({"usage": usage}, indent=2))
    return 0 if all(item["passed"] for item in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report", type=Path, default=ROOT / "evals" / "latest_agent_report.json"
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args.report)))


if __name__ == "__main__":
    main()
