#!/usr/bin/env python3
"""Run the prompt/tool-sequence regression set without real side effects."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src import timeutil
from src.agent import Agent
from src.config import config
from src.history import History
from src.store import Store
from src.tools import TOOLS_BY_NAME

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "agent_cases.json"
SAFE_EXTRA_TOOLS = {
    "resolve_date", "query_schedule", "query_tasks", "find_free_blocks",
    "query_facts", "query_goals", "explain_schedule",
}


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
                self.call_details.append({"name": _name, "arguments": arguments})
                self.next_id += 1
                return self.result(_name, arguments)
            handlers[name] = handler
        return handlers

    def result(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "resolve_date":
            local = timeutil.resolve_relative(str(arguments.get("phrase", "")))
            return {
                "phrase": arguments.get("phrase"),
                "local": local.isoformat(),
                "utc": timeutil.to_utc(local).isoformat().replace("+00:00", "Z"),
                "timezone": config.USER_TIMEZONE,
            }
        if name == "query_tasks":
            return [{"id": 12, "title": "pset report", "status": "pending", "estimated_minutes": 90}]
        if name == "query_schedule":
            if self.case_id == "color_match_reference":
                return [{
                    "source": "gcal", "source_id": "cs311", "title": "CS311 lecture",
                    "start": "2026-09-03T20:30:00Z", "end": "2026-09-03T22:00:00Z",
                    "metadata": {"color_id": "6", "calendar_color_id": "9"},
                }]
            if self.case_id == "color_inherited_reference":
                return [{
                    "source": "gcal", "source_id": "canvas", "title": "CS311 lecture",
                    "start": "2026-09-03T20:30:00Z", "end": "2026-09-03T22:00:00Z",
                    "metadata": {"color_id": None, "calendar_color_id": "9"},
                }]
            if self.case_id == "color_conflicting_references":
                return [
                    {
                        "source": "gcal", "source_id": "cs311-a", "title": "CS311 lecture",
                        "start": "2026-09-03T20:30:00Z", "end": "2026-09-03T22:00:00Z",
                        "metadata": {"color_id": "6", "calendar_color_id": "9"},
                    },
                    {
                        "source": "gcal", "source_id": "cs311-b", "title": "CS311 review",
                        "start": "2026-09-04T20:30:00Z", "end": "2026-09-04T22:00:00Z",
                        "metadata": {"color_id": "3", "calendar_color_id": "9"},
                    },
                ]
            if self.case_id == "color_external_refusal":
                return [{
                    "source": "gcal", "source_id": "canvas", "title": "Canvas lecture",
                    "start": "2026-09-03T20:30:00Z", "end": "2026-09-03T22:00:00Z",
                    "metadata": {"color_id": "6", "calendar_access_role": "reader"},
                }]
            return [{"source": "event", "source_id": "21", "title": "dentist", "start": "2026-08-28T19:00:00Z", "end": "2026-08-28T20:00:00Z"}]
        if name == "find_free_blocks":
            return [{
                "start": arguments.get("start"), "end": arguments.get("end"),
                "before": "dinner",
            }]
        if name == "query_facts":
            return [{"id": 7, "content": "prefers workouts after 9am", "active": True}]
        if name == "query_goals":
            return [{"id": 5, "title": "gym", "target_amount": 3, "progress": {"amount_remaining": 1}}]
        if name == "explain_schedule":
            return [{"task_id": 12, "reasoning": "the Friday deadline made this the only 90 minute gap"}]
        if name.startswith("add_"):
            return [{"id": self.next_id, "created": True}]
        return {"ok": True, "id": arguments.get("task_id", arguments.get("event_id"))}


def matches(case: dict[str, Any], observed: list[str]) -> bool:
    """Match required calls while permitting additional read-only verification."""
    expected = list(case["expected"])
    if case.get("unordered"):
        remaining = list(observed)
        for name in expected:
            if name not in remaining:
                return False
            remaining.remove(name)
        return all(name in SAFE_EXTRA_TOOLS for name in remaining)

    position = 0
    extras: list[str] = []
    for name in observed:
        if position < len(expected) and name == expected[position]:
            position += 1
        else:
            extras.append(name)
    return position == len(expected) and all(name in SAFE_EXTRA_TOOLS for name in extras)


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
        "observed": recorder.calls, "passed": matches(case, recorder.calls),
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
        json.dumps({"summary": summary, "usage": usage, "results": results}, indent=2),
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
