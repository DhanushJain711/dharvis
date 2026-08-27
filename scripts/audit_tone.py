#!/usr/bin/env python3
"""Read twenty real agent replies and flag assistant-like tone regressions."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from scripts.eval_agent import RecordingTools
from src.agent import Agent
from src.config import config
from src.history import History
from src.store import Store

ROOT = Path(__file__).resolve().parents[1]
BANNED = (
    "certainly!", "i've gone ahead and", "let me know if you need anything else",
    "reasoning:", "as an ai", "i'd be happy to",
)
CAUSAL = re.compile(r"—|\bbecause\b|\bsince\b|\bonly\b|\bdeadline\b|\byou never\b", re.IGNORECASE)

CASES = [
    ("add laundry", []),
    ("add laundry, call mom, and renew my license", []),
    ("dentist tomorrow at 2 for an hour", []),
    ("finished task 12 in 35 mins", [["assistant", "task 12 is the reading"]]),
    ("delete event 21", [["assistant", "event 21 is the dentist"]]),
    ("what's due this week?", []),
    ("am I free Friday afternoon?", []),
    ("put task 12 at 3 tomorrow — it's the only 90 minute gap before Friday", []),
    ("move task 12 to 10 tomorrow because I never use the 6am slot", []),
    ("why is task 12 after dinner?", []),
    ("set a goal of 3 gym sessions a week", []),
    ("logged one gym session", [["assistant", "goal 5 is the gym goal"]]),
    ("actually make task 12 due Saturday", []),
    ("move it to 4", [["assistant", "event 21 is the dentist tomorrow at 2"]]),
    ("add a fact that I do deep work before lunch", []),
    ("show my goals and pending tasks", []),
    ("cancel the dentist appointment", []),
    ("schedule task 12 in the open block from 3 to 5 tomorrow", []),
    ("I skipped the 6am gym again", []),
    ("what changed today?", []),
]


def violations(text: str, scheduling: bool) -> list[str]:
    lower = text.lower()
    found = [phrase for phrase in BANNED if phrase in lower]
    bullet_lines = [line for line in text.splitlines() if re.match(r"\s*[-*•]", line)]
    if 0 < len(bullet_lines) < 3:
        found.append("short bullet list")
    if scheduling and not CAUSAL.search(text):
        found.append("schedule rationale is not a natural causal aside")
    return found


async def main_async(report: Path) -> int:
    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY is required for the live tone audit", flush=True)
        return 2
    results = []
    with tempfile.TemporaryDirectory(prefix="dharvis-tone-") as directory:
        for index, (message, prior) in enumerate(CASES, 1):
            store = Store(Path(directory) / f"tone-{index}.sqlite")
            await store.initialize()
            history = History(store)
            tools = RecordingTools()
            agent = Agent(history, tool_handlers=tools.handlers())
            conversation = f"tone-{index}"
            session, _ = await history.resolve_session(conversation)
            for role, content in prior:
                await history.append(session, role, content)
            output = await agent.run_tool_loop(message, conversation)
            scheduling = "schedule_task" in tools.calls
            issues = violations(output, scheduling)
            results.append({
                "message": message, "output": output, "tools": tools.calls,
                "violations": issues, "passed": not issues,
            })
            print(f"\n{index:02d} USER: {message}\n   BOT: {output}", flush=True)
    summary = {"passed": sum(item["passed"] for item in results), "total": len(results)}
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0 if summary["passed"] == summary["total"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async(ROOT / "evals" / "latest_tone_audit.json")))


if __name__ == "__main__":
    main()
