#!/usr/bin/env python3
"""Run the prompt/tool-sequence regression set without real side effects."""

from __future__ import annotations

import argparse
import asyncio
import json
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from src.agent import Agent
from src.config import config
from src.history import History
from src.store import Store
from src.tools import TOOLS_BY_NAME

ROOT = Path(__file__).resolve().parents[1]
CASES_PATH = ROOT / "evals" / "agent_cases.json"


class RecordingTools:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.next_id = 100

    def handlers(self) -> dict[str, Any]:
        handlers: dict[str, Any] = {}
        for name in TOOLS_BY_NAME:
            async def handler(_name: str = name, **arguments: Any) -> Any:
                self.calls.append(_name)
                self.next_id += 1
                return self.result(_name, arguments)
            handlers[name] = handler
        return handlers

    def result(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "resolve_date":
            return {
                "phrase": arguments.get("phrase"),
                "local": "2026-08-28T09:00:00-05:00",
                "utc": "2026-08-28T14:00:00Z",
                "timezone": config.USER_TIMEZONE,
            }
        if name == "query_tasks":
            return [{"id": 12, "title": "pset report", "status": "pending", "estimated_minutes": 90}]
        if name == "query_schedule":
            return [{"source": "event", "source_id": "21", "title": "dentist", "start": "2026-08-28T19:00:00Z", "end": "2026-08-28T20:00:00Z"}]
        if name == "find_free_blocks":
            return [{"start": "2026-08-28T20:00:00Z", "end": "2026-08-28T22:00:00Z", "before": "dinner"}]
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
    expected = list(case["expected"])
    if case.get("unordered"):
        return sorted(expected) == sorted(observed)
    if case.get("unordered_tail") and expected:
        return observed[:1] == expected[:1] and sorted(observed[1:]) == sorted(expected[1:])
    return observed == expected


async def run_case(case: dict[str, Any], root: Path) -> dict[str, Any]:
    store = Store(root / f"{case['id']}.sqlite")
    await store.initialize()
    history = History(store)
    recorder = RecordingTools()
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
    return {
        "id": case["id"], "category": case["category"],
        "message": case["message"], "expected": case["expected"],
        "observed": recorder.calls, "passed": matches(case, recorder.calls),
        "response": response, "error": error,
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
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps({"summary": summary, "results": results}, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(summary, indent=2))
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
