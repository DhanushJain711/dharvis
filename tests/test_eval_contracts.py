from __future__ import annotations

import json
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.freebusy import FreeBlock
from src.scheduler_engine import SchedulerEngine, SchedulingPlanError
from src.tools import TOOLS_BY_NAME


def test_agent_eval_has_45_valid_messages_and_all_categories():
    cases = json.loads(
        (Path(__file__).parents[1] / "evals" / "agent_cases.json").read_text()
    )
    assert len(cases) == 45
    counts = Counter(case["category"] for case in cases)
    assert set(counts) == {
        "multi_item", "relative_dates", "ambiguous_references",
        "corrections", "multi_turn_context", "cross_domain_queries",
    }
    assert all(case["message"].strip() for case in cases)
    assert all(set(case["expected"]) <= set(TOOLS_BY_NAME) for case in cases)


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
