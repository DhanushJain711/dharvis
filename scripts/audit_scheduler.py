#!/usr/bin/env python3
"""Generate and audit twenty real scheduler rationales plus their /why chains."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from src import timeutil
from src.config import config
from src.facts_engine import FactsEngine
from src.freebusy import FreeBlock
from src.integration import build_tool_handlers
from src.scheduler_engine import SchedulerEngine
from src.store import Store

ROOT = Path(__file__).resolve().parents[1]
GENERIC = re.compile(
    r"\b(good|great|best|ideal|suitable|available|convenient) (time|slot|fit|block)\b|"
    r"\b(fits well|works well|makes sense|selected slot)\b",
    re.IGNORECASE,
)


class NoCalendar:
    _last_query_complete = True

    async def list_events(self, start, end):
        return []


async def main_async(report: Path) -> int:
    if not config.OPENAI_API_KEY:
        print("OPENAI_API_KEY is required for the live rationale audit", flush=True)
        return 2
    with tempfile.TemporaryDirectory(prefix="dharvis-rationale-") as directory:
        store = Store(Path(directory) / "audit.sqlite")
        await store.initialize()
        calendar = NoCalendar()
        engine = SchedulerEngine(store, calendar)
        facts = await store.add_fact({
            "content": "deep work usually happens before lunch",
            "category": "energy", "confidence": 0.9, "source": "explicit",
        })
        goal = await store.add_goal({
            "title": "study quota", "target_amount": 8, "target_unit": "hours",
            "period": "week", "category": "school",
        })
        handlers = await build_tool_handlers(store, calendar, engine, FactsEngine(store))
        today = timeutil.now_local().date()
        results = []
        for index in range(20):
            day = today + timedelta(days=1 + index % 7)
            hour = (9, 11, 14, 16, 19)[index % 5]
            local_start = datetime.combine(day, time(hour), tzinfo=timeutil.now_local().tzinfo)
            start = timeutil.to_utc(local_start)
            duration = (30, 45, 60, 90, 120)[index % 5]
            block = FreeBlock(
                start, start + timedelta(minutes=duration + 30),
                "class" if index % 2 else "quiet hours end",
                "meeting" if index % 3 else "deadline window",
            )
            [task] = await store.add_tasks([{
                "title": f"audit task {index + 1}",
                "deadline": block.end + timedelta(hours=2 + index % 4),
                "estimated_minutes": duration,
                "category": "school",
                "energy": "deep_focus" if hour < 12 else "light",
                "priority": ("low", "medium", "high")[index % 3],
                "goal_id": goal["id"] if index % 4 == 0 else None,
            }])
            goals = await store.query_goals(active=True)
            placements = await engine._plan_assignments(
                [task], [("only_block", block)], [facts], goals
            )
            if len(placements) != 1:
                results.append({"task_id": task["id"], "passed": False, "reason": "no assignment"})
                continue
            placement = placements[0]
            await store.record_decision(
                task["id"], "scheduled", placement.start, placement.end, None,
                "daily_plan", placement.reasoning, placement.facts_used,
            )
            chain = await handlers["explain_schedule"](task_id=task["id"])
            generic = bool(GENERIC.search(placement.reasoning))
            coherent = bool(chain and chain[-1]["reasoning"] == placement.reasoning)
            passed = not generic and coherent
            results.append({
                "task_id": task["id"], "day": day.isoformat(),
                "start": placement.start.isoformat(), "reasoning": placement.reasoning,
                "facts_used": placement.facts_used, "why_chain": chain,
                "generic": generic, "coherent": coherent, "passed": passed,
            })
            print(f"{index + 1:02d}. {placement.reasoning}", flush=True)
        summary = {
            "passed": sum(item["passed"] for item in results),
            "total": len(results),
        }
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(json.dumps({"summary": summary, "results": results}, indent=2), encoding="utf-8")
        print(json.dumps(summary, indent=2))
        return 0 if summary["passed"] == summary["total"] else 1


def main() -> None:
    raise SystemExit(asyncio.run(main_async(ROOT / "evals" / "latest_scheduler_audit.json")))


if __name__ == "__main__":
    main()
