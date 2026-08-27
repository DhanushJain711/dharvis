"""Autonomous, explainable task placement over deterministic free-time blocks.

The language model ranks tasks and selects named blocks. It never receives
responsibility for clock arithmetic: :mod:`src.freebusy` computes availability,
and this module packs task durations into returned blocks in Python.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import logging
import re
from time import monotonic
from typing import Any, Literal, Mapping, Sequence

from openai import AsyncOpenAI

from . import timeutil
from .calendar_service import CalendarService
from .config import config
from .costs import estimated_cost, usage_numbers
from .freebusy import (
    CalendarQueryIncompleteError,
    FreeBlock,
    ScheduleBlock,
    compute_free_blocks,
    query_schedule,
)
from .store import Store

Trigger = Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"]
DecisionAction = Literal["scheduled", "moved", "unscheduled", "shortened", "extended"]

logger = logging.getLogger(__name__)
SCHEDULER_MODEL = "gpt-5.6-terra"
MAX_MODEL_ATTEMPTS = 2
_WORD_RE = re.compile(r"[a-z0-9]+")
_GENERIC_REASON_RE = re.compile(
    r"\b(?:good|great|best|ideal|suitable|available|appropriate|convenient)\s+"
    r"(?:time|slot|fit|choice|block)\b|\b(?:fits? well|works? well|makes sense|"
    r"logical choice|selected slot|open slot)\b",
    re.IGNORECASE,
)
_GENERIC_FACT_WORDS = {
    "always", "never", "usually", "often", "sometimes", "prefer", "prefers",
    "habit", "routine", "time", "times", "work", "works", "task", "tasks",
    "schedule", "scheduled", "slot", "block", "window", "gap", "focus",
    "energy", "morning", "afternoon", "evening", "night", "deadline", "due",
    "priority", "goal", "quota", "school", "personal", "fitness", "career",
    "errand", "best", "better",
}

_ASSIGNMENTS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"},
                    "block_id": {"type": "string"},
                    "reasoning": {"type": "string"},
                    "facts_used": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["task_id", "block_id", "reasoning", "facts_used"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


class SchedulingPlanError(RuntimeError):
    """A model response could not be made safe after the allowed retry."""


@dataclass(frozen=True, slots=True)
class ScheduleDecision:
    """A persisted placement plus its contemporaneous rationale."""

    task_id: int
    action: DecisionAction
    start: datetime
    end: datetime
    previous_start: datetime | None
    previous_end: datetime | None
    trigger: Trigger
    reasoning: str
    facts_used: list[int]
    id: int | None = None
    title: str | None = None
    gcal_event_id: str | None = None


@dataclass(frozen=True, slots=True)
class _Placement:
    task: dict[str, Any]
    block_id: str
    start: datetime
    end: datetime
    reasoning: str
    facts_used: list[int]


def _utc(value: Any, name: str) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{name} must be an ISO-8601 datetime") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are not allowed")
    return value.astimezone(UTC)


def _record_value(record: Any, key: str, default: Any = None) -> Any:
    if isinstance(record, Mapping):
        return record.get(key, default)
    return getattr(record, key, default)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def _event_id(record: Mapping[str, Any]) -> str:
    return str(record.get("gcal_event_id", record.get("id", "")) or "").strip()


def _external_block(
    record: Mapping[str, Any], source: Literal["gcal", "event"]
) -> ScheduleBlock:
    """Normalize a fresh external/local record without another calendar read."""
    start = _utc(record.get("start_time", record.get("start")), "event start")
    end = _utc(record.get("end_time", record.get("end")), "event end")
    if end <= start:
        raise ValueError("event end must be later than start")
    return ScheduleBlock(
        start=start,
        end=end,
        title=str(record.get("title") or record.get("summary") or "Untitled Event"),
        source=source,
        source_id=(
            _event_id(record)
            if source == "gcal"
            else str(record.get("id", record.get("gcal_event_id", "")))
        ),
        metadata=dict(record),
    )


def _range(affected_range: Any) -> tuple[datetime, datetime]:
    if isinstance(affected_range, Mapping):
        start = affected_range.get("start", affected_range.get("start_time"))
        end = affected_range.get("end", affected_range.get("end_time"))
    elif isinstance(affected_range, Sequence) and not isinstance(affected_range, (str, bytes)):
        if len(affected_range) != 2:
            raise ValueError("affected_range must contain exactly start and end")
        start, end = affected_range
    else:
        start = getattr(affected_range, "start", None)
        end = getattr(affected_range, "end", None)
    start_utc = _utc(start, "affected_range start")
    end_utc = _utc(end, "affected_range end")
    if end_utc <= start_utc:
        raise ValueError("affected_range end must be later than start")
    return start_utc, end_utc


def _overlaps(start: datetime, end: datetime, other_start: datetime, other_end: datetime) -> bool:
    return start < other_end and other_start < end


def _constraints(busy: list[ScheduleBlock], *, buffer_minutes: int = 0) -> dict[str, Any]:
    return {
        "busy_intervals": busy,
        "waking_hours": ("00:00", "00:00"),
        "quiet_hours": {"start": config.QUIET_HOURS_START, "end": config.QUIET_HOURS_END},
        "timezone": config.USER_TIMEZONE,
        "buffer_minutes": buffer_minutes,
    }


def _task_duration(task: Mapping[str, Any]) -> int:
    value = task.get("estimated_minutes")
    if type(value) is not int or value <= 0:
        raise ValueError(f"Task {task.get('id')} has no positive estimated_minutes")
    return value


def _meaningful_words(value: str) -> set[str]:
    ignored = {
        "about", "after", "again", "before", "because", "could", "from", "have",
        "into", "just", "that", "the", "their", "then", "there", "this", "with",
        "your", "you", "for", "and", "but", "its", "it's", "slot", "task",
    }
    return {word for word in _WORD_RE.findall(value.lower()) if len(word) >= 3 and word not in ignored}


def _planning_floor(now: datetime | None = None) -> datetime:
    """Return a whole-minute floor safely ahead of model/API latency."""
    current = _utc(now, "now") if now is not None else timeutil.now_utc()
    return (current + timedelta(minutes=2)).replace(second=0, microsecond=0)


class SchedulerEngine:
    """Coordinate model ranking, deterministic validation, and atomic logging."""

    def __init__(
        self,
        store: Store,
        calendar: CalendarService,
        *,
        client: Any | None = None,
        openai_client: Any | None = None,
        model: str | None = None,
    ) -> None:
        if client is not None and openai_client is not None:
            raise ValueError("Pass either client or openai_client, not both")
        self.store = store
        self.calendar = calendar
        self._client = client if client is not None else openai_client
        self.model = model or config.SCHEDULER_MODEL_ID or SCHEDULER_MODEL
        self._mutation_lock = asyncio.Lock()

    @property
    def client(self) -> Any:
        """Lazily construct OpenAI so importing the app needs no API key."""
        if self._client is None:
            self._client = AsyncOpenAI(api_key=config.OPENAI_API_KEY or None)
        return self._client

    async def _response_json(self, payload: dict[str, Any], retry_error: str | None) -> dict[str, Any]:
        system = (
            "You place personal tasks into precomputed free blocks. You rank and assign only; "
            "never calculate, invent, split, resize, or move a time block. Multiple assignments "
            "may use one block; their response order is their execution order and Python packs them "
            "from that block's start. You may omit tasks that cannot safely fit. Weigh deadline "
            "urgency, estimated duration versus block capacity, deep-focus energy earlier in the "
            "user's productive day, explicit learned habits, goals behind quota, and the user's "
            "real priority ordering in the facts even when paper priority differs. Every reasoning "
            "field must name the concrete constraint that drove it: a deadline, duration or block "
            "boundary, energy/time-of-day match, cited habit fact, behind goal quota, or priority. "
            "Before emitting an assignment, verify its reason contains at least one literal value "
            "from the supplied data: the exact task or block minutes, deadline, bounded-by label, "
            "energy plus matching time of day, cited fact content, quota remaining, or priority. "
            "When no stronger habit or quota applies, use the exact duration comparison and deadline; "
            "for example, 'the 45-minute task fits the 75-minute block and ends before its Friday deadline.' "
            "Generic claims like 'good slot' or restating the assignment are invalid. facts_used "
            "may contain only IDs from supplied facts and every cited fact must be explicitly tied "
            "to the wording of the reason. Never claim a duration, weekday, time of day, energy, "
            "priority, boundary, or behind-goal status that differs from supplied data. Return only "
            "the strict JSON object."
        )
        if retry_error:
            system += f" The previous response was rejected: {retry_error}. Correct that exact defect."
        started = monotonic()
        response = await self.client.responses.create(
            model=self.model,
            input=[
                {"role": "system", "content": system},
                {"role": "user", "content": json.dumps(payload, default=_json_default, separators=(",", ":"))},
            ],
            text={"format": {
                "type": "json_schema", "name": "schedule_assignments", "strict": True,
                "schema": _ASSIGNMENTS_SCHEMA,
            }},
        )
        duration_ms = round((monotonic() - started) * 1000, 2)
        status = _record_value(response, "status")
        refusal = _record_value(response, "refusal")
        if status not in (None, "completed"):
            raise SchedulingPlanError(f"model response status was {status!r}")
        if refusal:
            raise SchedulingPlanError(f"model refused the scheduling request: {refusal}")
        text = _record_value(response, "output_text")
        if not isinstance(text, str) or not text.strip():
            output = _record_value(response, "output", []) or []
            chunks: list[str] = []
            for item in output:
                for content in _record_value(item, "content", []) or []:
                    if _record_value(content, "type") == "refusal":
                        raise SchedulingPlanError(
                            f"model refused the scheduling request: {_record_value(content, 'refusal', '')}"
                        )
                    candidate = _record_value(content, "text")
                    if isinstance(candidate, str):
                        chunks.append(candidate)
            text = "".join(chunks)
        if not text:
            raise SchedulingPlanError("model returned no JSON text")
        try:
            decoded = json.loads(text)
        except (TypeError, json.JSONDecodeError) as exc:
            raise SchedulingPlanError("model returned malformed JSON") from exc
        if not isinstance(decoded, dict) or set(decoded) != {"assignments"}:
            raise SchedulingPlanError("model output must contain only assignments")
        usage = usage_numbers(response)
        cost = estimated_cost(self.model, usage)
        recorder = getattr(self.store, "record_usage", None)
        if callable(recorder):
            try:
                await recorder("scheduler", self.model, usage, cost, None)
            except Exception:
                logger.exception("scheduler_usage_persistence_failed")
        logger.info(
            "scheduler_model_call",
            extra={"scheduler_event": {
                "model": self.model,
                "duration_ms": duration_ms,
                "usage": usage,
                "estimated_cost_usd": cost,
            }},
        )
        return decoded

    def _reason_names_constraint(
        self,
        reason: str,
        task: Mapping[str, Any],
        block: FreeBlock,
        fact_ids: list[int],
        facts_by_id: Mapping[int, Mapping[str, Any]],
        goals: list[dict[str, Any]],
    ) -> bool:
        clean = " ".join(reason.split()).strip()
        words = _meaningful_words(clean)
        if len(words) < 3 or len(clean) < 18 or _GENERIC_REASON_RE.search(clean):
            return False
        lower = clean.lower()
        if not re.search(
            r"\b(?:is|are|need|needs|leave|leaves|give|gives|fit|fits|match|matches|"
            r"clear|clears|keep|keeps|protect|protects|drive|drives|make|makes|"
            r"prefer|prefers|focus|work|works|behind|only|before|after|because|since|so|"
            r"never|usually|enough|room)\b",
            lower,
        ):
            return False
        placement_hour = timeutil.to_local(block.start).hour

        # Citing a fact is a truth claim, not decoration. Every selected fact
        # must share specific language/number content. Generic habit and
        # scheduling words ("usually", "work", "time") never establish
        # relevance by themselves.
        for fact_id in fact_ids:
            content = str(facts_by_id.get(fact_id, {}).get("content", ""))
            content_lower = content.lower()
            fact_words = _meaningful_words(content) - _GENERIC_FACT_WORDS
            reason_words = words - _GENERIC_FACT_WORDS
            verified_specific_claim = False
            period_matches = {
                "morning": 5 <= placement_hour < 12,
                "afternoon": 12 <= placement_hour < 17,
                "evening": 17 <= placement_hour < 22,
                "night": placement_hour >= 20 or placement_hour < 5,
            }
            same_habit_qualifier = any(
                qualifier in lower and qualifier in content_lower
                for qualifier in ("never", "usually", "always", "prefer", "prefers")
            )
            if (
                not fact_words
                and same_habit_qualifier
                and any(
                    period in lower and period in content_lower and matches
                    for period, matches in period_matches.items()
                )
            ):
                verified_specific_claim = True
            energy = str(task.get("energy") or "")
            if (
                "deep focus" in lower
                and "deep focus" in content_lower
                and energy == "deep_focus"
            ):
                verified_specific_claim = True
            def clock_claims(text: str) -> set[tuple[int, int, str]]:
                return {
                    (int(hour), int(minute or 0), meridiem or "")
                    for hour, minute, meridiem in re.findall(
                        r"\bat\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", text
                    )
                    if 0 <= int(hour) <= 23 and 0 <= int(minute or 0) <= 59
                }

            common_clocks = clock_claims(lower) & clock_claims(content_lower)
            if common_clocks:
                local_start = timeutil.to_local(block.start)
                clock_matches = False
                for claimed_hour, claimed_minute, meridiem in common_clocks:
                    if meridiem:
                        if not 1 <= claimed_hour <= 12:
                            return False
                        absolute_hour = claimed_hour % 12
                        if meridiem == "pm":
                            absolute_hour += 12
                        matches = (
                            local_start.hour == absolute_hour
                            and local_start.minute == claimed_minute
                        )
                    else:
                        matches = (
                            local_start.hour % 12 == claimed_hour % 12
                            and local_start.minute == claimed_minute
                        )
                    clock_matches = clock_matches or matches
                if not clock_matches:
                    return False
                if not fact_words and same_habit_qualifier:
                    verified_specific_claim = True

            # Numeric coincidence alone is not relevance: "gym at 6" cannot
            # justify a pset whose deadline also happens to be 6. Facts with a
            # subject require subject overlap; pure temporal/energy facts use
            # the verified semantic checks above.
            subject_overlap = bool(reason_words & fact_words)
            if (fact_words and not subject_overlap) or (
                not fact_words and not verified_specific_claim
            ):
                return False

        anchors: set[str] = set()
        deadline = task.get("deadline")
        deadline_claimed = any(
            token in lower for token in ("deadline", "due ", "due-", "overdue")
        )
        if deadline_claimed and deadline is None:
            return False
        if deadline is not None:
            local_deadline = timeutil.to_local(_utc(deadline, "deadline"))
            deadline_day = local_deadline.strftime("%A").lower()
            mentioned_days = {
                day.lower() for day in (
                    "Monday", "Tuesday", "Wednesday", "Thursday", "Friday",
                    "Saturday", "Sunday",
                ) if day.lower() in lower
            }
            if deadline_claimed and mentioned_days and deadline_day not in mentioned_days:
                return False
            if deadline_claimed or deadline_day in lower or local_deadline.strftime("%b").lower() in lower:
                anchors.add("deadline")

        task_minutes = _task_duration(task)
        block_minutes = int((block.end - block.start).total_seconds() // 60)
        quantity_minutes: list[int] = []
        for amount, unit in re.findall(
            r"\b(\d+)\s*(?:-\s*)?(minutes?|mins?|m|hours?|hrs?|h)\b", lower
        ):
            quantity_minutes.append(int(amount) * (60 if unit.startswith("h") else 1))
        word_amounts = {
            "one": 1, "two": 2, "three": 3, "four": 4,
            "five": 5, "six": 6, "seven": 7, "eight": 8,
        }
        for word, amount in word_amounts.items():
            if re.search(rf"\b{word}[ -](?:hour|hr)s?\b", lower):
                quantity_minutes.append(amount * 60)
        if quantity_minutes:
            if any(value not in {task_minutes, block_minutes} for value in quantity_minutes):
                return False
            anchors.add("duration")

        hour = placement_hour
        periods = {
            "morning": 5 <= hour < 12,
            "afternoon": 12 <= hour < 17,
            "evening": 17 <= hour < 22,
            "night": hour >= 20 or hour < 5,
        }
        for period, matches in periods.items():
            period_claim = bool(re.search(
                rf"\b(?:in\s+the\s+{period}|{period}\s+(?:block|gap|window|"
                rf"slot|focus|energy|work|session|time|habit|routine))\b",
                lower,
            )) or bool(re.search(
                rf"\b(?:prefer|prefers|only|usually|best)\b[^.;]{{0,40}}\b{period}\b",
                lower,
            ))
            if period_claim:
                if not matches:
                    return False
                anchors.add("time_of_day")

        energy = str(task.get("energy") or "")
        if "deep focus" in lower or re.search(r"\bfocus\b", lower):
            if energy != "deep_focus":
                return False
            anchors.add("energy")
        if "light work" in lower:
            if energy != "light":
                return False
            anchors.add("energy")
        if "errand" in lower:
            if energy != "errand":
                return False
            anchors.add("energy")

        boundary_words = _meaningful_words(" ".join(part for part in (block.after, block.before) if part))
        if words & boundary_words:
            anchors.add("boundary")
        if any(token in lower for token in ("gap", "block", "window")) and any(
            token in lower
            for token in (
                "only", "enough", "long", "short", "uninterrupted", "room",
                "one", "two", "three", "four", "five", "six", "seven", "eight",
            )
        ):
            anchors.add("block_shape")
        if fact_ids:
            anchors.add("fact")

        relevant_goals = [
            goal for goal in goals
            if goal.get("id") == task.get("goal_id") or goal.get("category") == task.get("category")
        ]
        goal_claimed = any(
            token in lower for token in ("goal", "quota", "behind", "session", "weekly", "monthly")
        )
        if goal_claimed:
            if not relevant_goals:
                return False
            if "behind" in lower or "quota" in lower:
                if not any(
                    isinstance(goal.get("progress"), Mapping)
                    and float(goal["progress"].get("amount_remaining", 0) or 0) > 0
                    for goal in relevant_goals
                ):
                    return False
            anchors.add("goal")

        priority = str(task.get("priority") or "")
        claimed_priorities = {
            value for value in ("low", "medium", "high")
            if f"{value} priority" in lower
        }
        if claimed_priorities and priority not in claimed_priorities:
            return False
        if "priority" in lower and priority:
            anchors.add("priority")
        return bool(anchors)

    def _pack_and_validate(
        self,
        raw: dict[str, Any],
        tasks: list[dict[str, Any]],
        blocks: list[tuple[str, FreeBlock]],
        facts: list[dict[str, Any]],
        goals: list[dict[str, Any]],
    ) -> list[_Placement]:
        assignments = raw.get("assignments")
        if not isinstance(assignments, list):
            raise SchedulingPlanError("assignments must be an array")
        tasks_by_id = {task.get("id"): task for task in tasks if type(task.get("id")) is int}
        blocks_by_id = dict(blocks)
        facts_by_id = {fact.get("id"): fact for fact in facts if type(fact.get("id")) is int}
        cursors = {block_id: block.start for block_id, block in blocks}
        seen_tasks: set[int] = set()
        placements: list[_Placement] = []
        for index, assignment in enumerate(assignments):
            if not isinstance(assignment, dict) or set(assignment) != {
                "task_id", "block_id", "reasoning", "facts_used"
            }:
                raise SchedulingPlanError(f"assignment {index} has invalid fields")
            task_id, block_id = assignment["task_id"], assignment["block_id"]
            if type(task_id) is not int or task_id not in tasks_by_id:
                raise SchedulingPlanError(f"assignment {index} references unknown task {task_id!r}")
            if task_id in seen_tasks:
                raise SchedulingPlanError(f"task {task_id} was assigned more than once")
            if not isinstance(block_id, str) or block_id not in blocks_by_id:
                raise SchedulingPlanError(f"assignment {index} references unknown block {block_id!r}")
            reason, fact_ids = assignment["reasoning"], assignment["facts_used"]
            if not isinstance(reason, str) or not reason.strip():
                raise SchedulingPlanError(f"task {task_id} has empty reasoning")
            if not isinstance(fact_ids, list) or any(type(item) is not int for item in fact_ids):
                raise SchedulingPlanError(f"task {task_id} facts_used must contain integer ids")
            if len(fact_ids) != len(set(fact_ids)):
                raise SchedulingPlanError(f"task {task_id} repeats a fact id")
            unknown_facts = set(fact_ids) - set(facts_by_id)
            if unknown_facts:
                raise SchedulingPlanError(f"task {task_id} cites unknown facts {sorted(unknown_facts)}")
            task, block = tasks_by_id[task_id], blocks_by_id[block_id]
            start = cursors[block_id]
            end = start + timedelta(minutes=_task_duration(task))
            if start < block.start or end > block.end:
                raise SchedulingPlanError(f"task {task_id} does not fit inside {block_id}")
            placement_block = FreeBlock(
                start,
                block.end,
                block.after if start == block.start else f"previous task in {block_id}",
                block.before,
            )
            if not self._reason_names_constraint(
                reason, task, placement_block, fact_ids, facts_by_id, goals
            ):
                raise SchedulingPlanError(
                    f"task {task_id} reasoning does not name a true constraint: {reason!r}"
                )
            deadline = task.get("deadline")
            if deadline is not None and end > _utc(deadline, f"task {task_id} deadline"):
                raise SchedulingPlanError(f"task {task_id} would end after its deadline")
            placements.append(_Placement(
                task, block_id, start, end, " ".join(reason.split()), list(fact_ids)
            ))
            cursors[block_id] = end
            seen_tasks.add(task_id)
        ordered = sorted(placements, key=lambda item: (item.start, item.end, item.task["id"]))
        for left, right in zip(ordered, ordered[1:]):
            if left.end > right.start:
                raise SchedulingPlanError(f"tasks {left.task['id']} and {right.task['id']} overlap")
        return placements

    def _payload(
        self,
        tasks: list[dict[str, Any]],
        blocks: list[tuple[str, FreeBlock]],
        facts: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        *,
        context: str | None = None,
    ) -> dict[str, Any]:
        return {
            "instruction": context or "Build a safe plan from the named free blocks.",
            "timezone": config.USER_TIMEZONE,
            "tasks": [{key: task.get(key) for key in (
                "id", "title", "deadline", "estimated_minutes", "category", "energy",
                "priority", "goal_id", "urgency_score",
            )} for task in tasks],
            "free_blocks": [{
                "block_id": block_id,
                "start": block.start,
                "end": block.end,
                "duration_minutes": int((block.end - block.start).total_seconds() // 60),
                "bounded_after_by": block.after,
                "bounded_before_by": block.before,
            } for block_id, block in blocks],
            "facts": [{key: fact.get(key) for key in (
                "id", "content", "category", "confidence", "evidence_count",
            )} for fact in facts],
            "goals": [{key: goal.get(key) for key in (
                "id", "title", "target_amount", "target_unit", "period", "category", "progress",
            )} for goal in goals],
        }

    async def _plan_assignments(
        self,
        tasks: list[dict[str, Any]],
        blocks: list[tuple[str, FreeBlock]],
        facts: list[dict[str, Any]],
        goals: list[dict[str, Any]],
        *,
        context: str | None = None,
    ) -> list[_Placement]:
        if not tasks or not blocks:
            return []
        payload = self._payload(tasks, blocks, facts, goals, context=context)
        error: str | None = None
        for attempt in range(MAX_MODEL_ATTEMPTS):
            try:
                raw = await self._response_json(payload, error)
                return self._pack_and_validate(raw, tasks, blocks, facts, goals)
            except Exception as exc:
                if isinstance(exc, (CalendarQueryIncompleteError, asyncio.CancelledError)):
                    raise
                error = str(exc) or type(exc).__name__
                logger.warning(
                    "scheduler_model_response_rejected",
                    extra={"scheduler_event": {"attempt": attempt + 1, "error": error}},
                )
                if attempt + 1 == MAX_MODEL_ATTEMPTS:
                    raise SchedulingPlanError(error) from exc
        raise AssertionError("unreachable")

    async def choose_slot(
        self, task_id: int, candidates: list[FreeBlock], trigger: Trigger
    ) -> ScheduleDecision:
        """Choose one named deterministic block without writing it."""
        task = await self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} does not exist")
        facts, goals = await asyncio.gather(
            self.store.query_facts(active=True), self.store.query_goals(active=True)
        )
        blocks = [(f"block_{index}", block) for index, block in enumerate(candidates, 1)]
        placements = await self._plan_assignments([task], blocks, facts, goals)
        if not placements:
            raise SchedulingPlanError(f"No safe slot was selected for task {task_id}")
        item = placements[0]
        return ScheduleDecision(
            task_id, "scheduled", item.start, item.end, None, None, trigger,
            item.reasoning, item.facts_used, title=str(task.get("title") or "Untitled Task"),
        )

    async def _actual_free(
        self,
        task_id: int,
        start: datetime,
        end: datetime,
        releasing_intervals: Mapping[int, tuple[datetime, datetime]] | None = None,
    ) -> bool:
        # Use unique, slightly wider read bounds to bypass CalendarService's
        # 60-second cache. Availability checks are safety barriers and must see
        # events added after the planning snapshot.
        cache_buster = timedelta(
            microseconds=1 + int(monotonic() * 1_000_000) % 1_000_000
        )
        occupied = await query_schedule(
            self.store, self.calendar, start - cache_buster, end + cache_buster
        )
        releasing = releasing_intervals or {}
        retained: list[ScheduleBlock] = []
        for block in occupied:
            if block.source == "task" and block.source_id == str(task_id):
                continue
            if block.source == "task":
                try:
                    releasing_task_id = int(block.source_id)
                except (TypeError, ValueError):
                    releasing_task_id = -1
                old_interval = releasing.get(releasing_task_id)
                # Ignore only the exact old interval promised to this cascade.
                # A prior task's newly committed placement has different times
                # and remains busy, preventing a later assignment from piling
                # onto it.
                if old_interval == (block.start, block.end):
                    continue
            retained.append(block)
        occupied = retained
        duration = max(1, int((end - start).total_seconds() // 60))
        free = compute_free_blocks(start, end, duration, _constraints(occupied))
        return any(block.start <= start and end <= block.end for block in free)

    @staticmethod
    def _action(
        previous_start: datetime | None,
        previous_end: datetime | None,
        start: datetime,
        end: datetime,
    ) -> DecisionAction:
        if previous_start is None or previous_end is None:
            return "scheduled"
        if start != previous_start:
            return "moved"
        old_duration, new_duration = previous_end - previous_start, end - start
        if new_duration < old_duration:
            return "shortened"
        if new_duration > old_duration:
            return "extended"
        return "scheduled"

    async def _schedule_task_locked(
        self,
        task_id: int,
        start: datetime,
        end: datetime,
        reasoning: str,
        trigger: Trigger,
        facts_used: list[int],
        releasing_intervals: Mapping[int, tuple[datetime, datetime]] | None = None,
    ) -> ScheduleDecision:
        start, end = _utc(start, "start"), _utc(end, "end")
        if end <= start:
            raise ValueError("end must be later than start")
        if start < timeutil.now_utc():
            raise ValueError("cannot schedule a task in an elapsed time range")
        rationale = " ".join(str(reasoning).split())
        if not rationale:
            raise ValueError("reasoning must contain non-whitespace text")
        task = await self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"Task {task_id} does not exist")
        deadline = task.get("deadline")
        if deadline is not None and end > _utc(deadline, "deadline"):
            raise ValueError("requested placement ends after the task deadline")
        # Availability is intentionally re-read immediately before every
        # external mutation. A model plan is only a proposal; a calendar event
        # may have arrived after the plan was validated.
        if not await self._actual_free(
            task_id, start, end, releasing_intervals
        ):
            raise ValueError("requested placement is not inside a real free block")
        previous_start = _utc(task["scheduled_start"], "previous_start") if task.get("scheduled_start") else None
        previous_end = _utc(task["scheduled_end"], "previous_end") if task.get("scheduled_end") else None
        action = self._action(previous_start, previous_end, start, end)
        title = str(task.get("title") or "Untitled Task")
        old_gcal_id = str(task.get("gcal_event_id") or "").strip() or None
        gcal_id = old_gcal_id
        previous_reason = "restored after a persistence error"
        if old_gcal_id:
            history = await self.store.get_decisions_for_task(task_id)
            if history:
                previous_reason = str(history[0].get("reasoning") or previous_reason)
            await self.calendar.update_work_block(old_gcal_id, title, start, end, rationale)
        else:
            gcal_id = await self.calendar.create_work_block(task_id, title, start, end, rationale)
        try:
            record = await self.store.apply_schedule_decision(
                task_id, action, start, end, previous_start, previous_end, trigger,
                rationale, facts_used, gcal_id,
            )
        except Exception:
            try:
                if old_gcal_id and previous_start is not None and previous_end is not None:
                    await self.calendar.update_work_block(
                        old_gcal_id, title, previous_start, previous_end, previous_reason
                    )
                elif gcal_id:
                    await self.calendar.delete_work_block(gcal_id)
            except Exception:
                logger.exception("calendar_schedule_rollback_failed")
            raise
        decision = self._decision(record, task, gcal_id)
        logger.info(
            "schedule_decision_applied",
            extra={"scheduler_event": {
                "task_id": task_id, "action": action, "decision_id": decision.id,
            }},
        )
        return decision

    async def schedule_task(
        self,
        task_id: int,
        start: datetime,
        end: datetime,
        reasoning: str,
        trigger: Trigger,
        facts_used: list[int] | None = None,
    ) -> ScheduleDecision:
        """Recheck availability, mutate Calendar, then atomically persist rationale."""
        start_utc, end_utc = _utc(start, "start"), _utc(end, "end")
        if end_utc <= start_utc:
            raise ValueError("end must be later than start")
        if start_utc < timeutil.now_utc():
            raise ValueError("cannot schedule a task in an elapsed time range")
        async with self._mutation_lock:
            existing = await self.store.get_task(task_id)
            if existing is None:
                raise KeyError(f"Task {task_id} does not exist")
            if existing.get("scheduled_start") and existing.get("scheduled_end"):
                fixed, corrections = await self._manual_fixed_points(
                    [existing],
                    start_utc,
                    end_utc,
                    respect_user_request_history=trigger != "user_request",
                )
                if task_id in fixed:
                    detail = (
                        "a manual calendar edit was reconciled"
                        if corrections else "the managed calendar block could not be verified"
                    )
                    raise SchedulingPlanError(
                        f"Task {task_id} was not overwritten because {detail}"
                    )
            return await self._schedule_task_locked(
                task_id, start_utc, end_utc, reasoning, trigger, list(facts_used or [])
            )

    @staticmethod
    def _decision(
        record: Mapping[str, Any], task: Mapping[str, Any], gcal_id: str | None
    ) -> ScheduleDecision:
        previous_start, previous_end = record.get("previous_start"), record.get("previous_end")
        return ScheduleDecision(
            task_id=int(record["task_id"]), action=record["action"],
            start=_utc(record["start"], "decision start"), end=_utc(record["end"], "decision end"),
            previous_start=_utc(previous_start, "previous_start") if previous_start else None,
            previous_end=_utc(previous_end, "previous_end") if previous_end else None,
            trigger=record["trigger"], reasoning=str(record["reasoning"]),
            facts_used=list(record.get("facts_used") or []),
            id=int(record["id"]) if record.get("id") is not None else None,
            title=str(task.get("title") or "Untitled Task"), gcal_event_id=gcal_id,
        )

    async def build_daily_plan(self, local_date: date) -> list[ScheduleDecision]:
        """Build and persist a safe reasoned plan for one local calendar day."""
        if not isinstance(local_date, date) or isinstance(local_date, datetime):
            raise TypeError("local_date must be a date")
        async with self._mutation_lock:
            start, end = timeutil.day_bounds(local_date)
            now = timeutil.now_utc()
            floor = _planning_floor(now)
            if end <= floor:
                raise ValueError("cannot build a plan for a fully elapsed day")
            start = max(start, floor)
            occupied, tasks, facts, goals = await asyncio.gather(
                query_schedule(self.store, self.calendar, start, end),
                self.store.get_schedulable_tasks(), self.store.query_facts(active=True),
                self.store.query_goals(active=True),
            )
            tasks = [task for task in tasks if task.get("estimated_minutes") and task.get("deadline")]
            if not tasks:
                return []
            minimum = min(_task_duration(task) for task in tasks)
            free = compute_free_blocks(start, end, minimum, _constraints(occupied, buffer_minutes=15))
            blocks = [(f"block_{index}", block) for index, block in enumerate(free, 1)]
            try:
                placements = await self._plan_assignments(tasks, blocks, facts, goals)
            except SchedulingPlanError:
                logger.exception("daily_plan_aborted_without_writes")
                return []
            decisions: list[ScheduleDecision] = []
            for placement in placements:
                try:
                    decisions.append(await self._schedule_task_locked(
                        int(placement.task["id"]), placement.start, placement.end,
                        placement.reasoning, "daily_plan", placement.facts_used,
                    ))
                except Exception as exc:
                    # Earlier writes are already individually consistent and
                    # audited. Return them rather than hiding a partial plan or
                    # manufacturing unreasoned compensating placements.
                    logger.exception("daily_plan_placement_skipped", extra={
                        "scheduler_event": {
                            "task_id": placement.task["id"],
                            "committed_decision_ids": [item.id for item in decisions],
                            "error": str(exc),
                        }
                    })
            return decisions

    async def plan_day(self, local_date: date) -> list[ScheduleDecision]:
        """Compatibility alias for :meth:`build_daily_plan`."""
        return await self.build_daily_plan(local_date)

    async def _calendar_records(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        records = await self.calendar.list_events(start, end)
        if getattr(self.calendar, "_last_query_complete", True) is False:
            raise CalendarQueryIncompleteError(
                "Google Calendar returned an incomplete event set; reconciliation is unsafe"
            )
        return records

    async def _fresh_calendar_snapshot(
        self,
        tasks: list[dict[str, Any]],
        start: datetime,
        end: datetime,
    ) -> tuple[list[dict[str, Any]], datetime, datetime]:
        """Read a cache-busted bounded horizon covering tasks and the operation."""
        padding = timedelta(days=max(1, config.SCHEDULER_LOOKAHEAD_DAYS))
        known_starts = [
            _utc(task["scheduled_start"], "scheduled_start")
            for task in tasks if task.get("scheduled_start")
        ]
        known_ends = [
            _utc(task["scheduled_end"], "scheduled_end")
            for task in tasks if task.get("scheduled_end")
        ]
        query_start = min([start, *known_starts]) - padding
        # CalendarService caches reads by exact bounds for 60 seconds. A tiny,
        # bounded end variation forces safety checks to observe later edits.
        cache_buster = timedelta(
            microseconds=1 + int(monotonic() * 1_000_000) % 1_000_000
        )
        query_end = max([end, *known_ends]) + padding + cache_buster
        records = await self._calendar_records(query_start, query_end)
        return records, query_start, query_end

    async def _emit_scheduler_signal(self, payload: dict[str, Any]) -> None:
        """Persist a fact-extraction signal in the canonical message channel."""
        try:
            await self.store.append_message(
                "tool",
                json.dumps(payload, default=_json_default, separators=(",", ":")),
                [],
                "scheduler-fact-signals",
            )
        except (AttributeError, NotImplementedError):
            # Lightweight stores used by embeddings/tests may not implement
            # history. The structured log remains available in that case.
            logger.warning("scheduler_signal_store_unavailable", extra={
                "scheduler_event": payload
            })
        except Exception:
            logger.exception("scheduler_signal_persistence_failed", extra={
                "scheduler_event": payload
            })

    async def _manual_fixed_points(
        self,
        tasks: list[dict[str, Any]],
        start: datetime,
        end: datetime,
        *,
        respect_user_request_history: bool = True,
        calendar_snapshot: tuple[list[dict[str, Any]], datetime, datetime] | None = None,
    ) -> tuple[set[int], list[ScheduleDecision]]:
        # CalendarService has no get-by-id contract. Query a bounded horizon
        # around both the known placement and the planning window. If the ID is
        # still absent, its state is unknowable (deleted, moved farther away, or
        # a partial provider view), so fail closed and never overwrite/delete it.
        records, query_start, query_end = (
            calendar_snapshot
            if calendar_snapshot is not None
            else await self._fresh_calendar_snapshot(tasks, start, end)
        )
        google_by_id = {_event_id(record): record for record in records if _event_id(record)}
        fixed: set[int] = set()
        corrections: list[ScheduleDecision] = []
        for task in tasks:
            task_id = int(task["id"])
            history = await self.store.get_decisions_for_task(task_id)
            if (
                respect_user_request_history
                and history
                and history[0].get("trigger") == "user_request"
            ):
                fixed.add(task_id)
            gcal_id = str(task.get("gcal_event_id") or "").strip()
            google = google_by_id.get(gcal_id)
            if (
                not gcal_id
                or google is None
                or not task.get("scheduled_start")
                or not task.get("scheduled_end")
            ):
                fixed.add(task_id)
                diagnostic = {
                    "kind": "calendar_block_unknown",
                    "task_id": task_id,
                    "gcal_event_id": gcal_id or None,
                    "known_start": task.get("scheduled_start"),
                    "known_end": task.get("scheduled_end"),
                    "query_start": query_start,
                    "query_end": query_end,
                    "reason": "managed event id was absent from a complete bounded calendar read",
                }
                logger.warning("managed_calendar_block_unknown", extra={
                    "scheduler_event": diagnostic
                })
                await self._emit_scheduler_signal(diagnostic)
                continue
            google_start = _utc(google.get("start_time", google.get("start")), "Google start")
            google_end = _utc(google.get("end_time", google.get("end")), "Google end")
            old_start = _utc(task["scheduled_start"], "scheduled_start")
            old_end = _utc(task["scheduled_end"], "scheduled_end")
            if google_start == old_start and google_end == old_end:
                continue
            rationale = f"you moved {task.get('title') or 'this task'} by hand, so that time is now fixed"
            previous_reason = "the previous automatic placement was restored"
            if history:
                previous_reason = str(history[0].get("reasoning") or previous_reason)
            try:
                await self.calendar.update_work_block(
                    gcal_id, str(task.get("title") or "Untitled Task"),
                    google_start, google_end, rationale,
                )
                record = await self.store.apply_schedule_decision(
                    task_id, "moved", google_start, google_end, old_start, old_end,
                    "user_request", rationale, [], gcal_id,
                )
            except Exception as exc:
                try:
                    # Preserve the user's chosen time while rolling back our
                    # description mutation; changing it back would discard the
                    # very manual correction we are trying to learn from.
                    await self.calendar.update_work_block(
                        gcal_id,
                        str(task.get("title") or "Untitled Task"),
                        google_start,
                        google_end,
                        previous_reason,
                    )
                except Exception:
                    logger.exception("manual_correction_calendar_rollback_failed")
                fixed.add(task_id)
                logger.exception("manual_correction_reconciliation_failed", extra={
                    "scheduler_event": {"task_id": task_id, "error": str(exc)}
                })
                await self._emit_scheduler_signal({
                    "kind": "manual_correction_reconciliation_failed",
                    "task_id": task_id,
                    "gcal_event_id": gcal_id,
                    "reason": str(exc),
                })
                continue
            task["scheduled_start"], task["scheduled_end"] = google_start, google_end
            fixed.add(task_id)
            correction = self._decision(record, task, gcal_id)
            corrections.append(correction)
            logger.info("manual_schedule_correction", extra={"scheduler_event": {
                "kind": "manual_correction", "task_id": task_id,
                "from": [old_start.isoformat(), old_end.isoformat()],
                "to": [google_start.isoformat(), google_end.isoformat()], "reason": rationale,
            }})
            await self._emit_scheduler_signal({
                "kind": "manual_schedule_correction",
                "task_id": task_id,
                "title": task.get("title"),
                "gcal_event_id": gcal_id,
                "previous_start": old_start,
                "previous_end": old_end,
                "new_start": google_start,
                "new_end": google_end,
                "reason": rationale,
            })
        return fixed, corrections

    @staticmethod
    def _causal_reason(cause: str, placement_reason: str) -> str:
        cause = " ".join(cause.split()).strip().rstrip(" .;,")
        placement_reason = " ".join(placement_reason.split()).strip().rstrip(" .")
        if not cause:
            raise ValueError("reschedule reason must contain non-whitespace text")
        if not placement_reason:
            return f"{cause}, so no safe replacement fit before the deadline"
        return f"{cause}, so {placement_reason[0].lower() + placement_reason[1:]}"

    async def _unschedule_locked(
        self,
        task: dict[str, Any],
        reason: str,
        trigger: Trigger,
    ) -> ScheduleDecision:
        task_id = int(task["id"])
        old_start = _utc(task["scheduled_start"], "scheduled_start")
        old_end = _utc(task["scheduled_end"], "scheduled_end")
        gcal_id = str(task.get("gcal_event_id") or "").strip() or None
        # Delete-after-DB is deliberate. Deleting first cannot be rolled back
        # safely because Google assigns a new event ID, leaving SQLite pointed
        # at a stale ID. If deletion fails, the old event still exists and we
        # can atomically restore the exact old ID and placement in SQLite.
        record = await self.store.apply_schedule_decision(
            task_id, "unscheduled", old_start, old_end, old_start, old_end,
            trigger, reason, [], None,
        )
        if not gcal_id:
            return self._decision(record, task, None)
        try:
            await self.calendar.delete_work_block(gcal_id)
        except Exception as exc:
            compensation_reason = (
                "calendar deletion failed, so the original block and event id were restored"
            )
            restored = await self.store.apply_schedule_decision(
                task_id, "scheduled", old_start, old_end, None, None,
                trigger, compensation_reason, [], gcal_id,
            )
            try:
                # This transient decision is represented by the immediately
                # following compensation and must not be surfaced later as a
                # real unschedule in a morning brief.
                await self.store.mark_decision_surfaced(int(record["id"]))
            except Exception:
                logger.exception("failed_unschedule_suppression_failed")
            logger.exception("calendar_unschedule_compensated", extra={
                "scheduler_event": {
                    "task_id": task_id,
                    "failed_unschedule_decision_id": record.get("id"),
                    "compensation_decision_id": restored.get("id"),
                    "gcal_event_id": gcal_id,
                    "error": str(exc),
                }
            })
            return self._decision(restored, task, gcal_id)
        return self._decision(record, task, None)

    async def _rollback_cascade_moves(
        self,
        committed: list[tuple[ScheduleDecision, dict[str, Any]]],
        failure: str,
        trigger: Trigger,
    ) -> list[ScheduleDecision]:
        """Reverse committed cascade moves after a promised release fails."""
        reversals: list[ScheduleDecision] = []
        for moved, original in reversed(committed):
            task_id = moved.task_id
            current = await self.store.get_task(task_id)
            if (
                current is None
                or not current.get("scheduled_start")
                or not current.get("scheduled_end")
            ):
                logger.critical("cascade_rollback_state_missing", extra={
                    "scheduler_event": {"task_id": task_id, "failure": failure}
                })
                raise SchedulingPlanError(
                    f"critical cascade rollback failure for task {task_id}: state missing"
                )
            original_start = _utc(original["scheduled_start"], "original start")
            original_end = _utc(original["scheduled_end"], "original end")
            current_start = _utc(current["scheduled_start"], "current start")
            current_end = _utc(current["scheduled_end"], "current end")
            gcal_id = str(current.get("gcal_event_id") or "").strip()
            if not gcal_id:
                logger.critical("cascade_rollback_calendar_id_missing", extra={
                    "scheduler_event": {"task_id": task_id, "failure": failure}
                })
                raise SchedulingPlanError(
                    f"critical cascade rollback failure for task {task_id}: calendar id missing"
                )
            title = str(current.get("title") or original.get("title") or "Untitled Task")
            rationale = (
                f"{failure}, so {title} returned to its original block to unwind the failed cascade"
            )
            try:
                await self.calendar.update_work_block(
                    gcal_id, title, original_start, original_end, rationale
                )
                record = await self.store.apply_schedule_decision(
                    task_id,
                    self._action(
                        current_start,
                        current_end,
                        original_start,
                        original_end,
                    ),
                    original_start,
                    original_end,
                    current_start,
                    current_end,
                    trigger,
                    rationale,
                    [],
                    gcal_id,
                )
            except Exception as exc:
                # If SQLite rejected the reversal after Calendar changed, put
                # Calendar back at SQLite's current placement. Either outcome
                # is reported as critical; callers must not claim success.
                try:
                    await self.calendar.update_work_block(
                        gcal_id,
                        title,
                        current_start,
                        current_end,
                        moved.reasoning,
                    )
                except Exception:
                    logger.critical(
                        "cascade_rollback_double_failure",
                        exc_info=True,
                        extra={"scheduler_event": {
                            "task_id": task_id, "failure": failure, "error": str(exc),
                        }},
                    )
                logger.critical(
                    "cascade_rollback_failed",
                    exc_info=True,
                    extra={"scheduler_event": {
                        "task_id": task_id, "failure": failure, "error": str(exc),
                    }},
                )
                raise SchedulingPlanError(
                    f"critical cascade rollback failure for task {task_id}"
                ) from exc
            reversal = self._decision(record, current, gcal_id)
            reversals.append(reversal)
            if moved.id is not None:
                try:
                    # The transient forward move remains in explain_schedule's
                    # audit chain but must not be announced as current state.
                    await self.store.mark_decision_surfaced(moved.id)
                except Exception:
                    logger.exception("cascade_transient_suppression_failed")
            logger.warning("cascade_move_rolled_back", extra={
                "scheduler_event": {
                    "task_id": task_id,
                    "forward_decision_id": moved.id,
                    "reversal_decision_id": reversal.id,
                    "failure": failure,
                }
            })
        return reversals

    async def _reschedule_locked(
        self,
        reason: str,
        affected_range: Any,
        trigger: Trigger,
    ) -> list[ScheduleDecision]:
        start, end = _range(affected_range)
        now = timeutil.now_utc()
        if end <= now:
            raise ValueError("cannot reschedule a fully elapsed range")
        start = max(start, now)
        # The affected interval selects displaced tasks. The enclosing local-day
        # window supplies alternate gaps, so a one-hour conflict does not force
        # an otherwise schedulable task back to pending.
        search_start = max(
            timeutil.day_bounds(timeutil.to_local(start).date())[0],
            _planning_floor(now),
        )
        end_local = timeutil.to_local(end - timedelta(microseconds=1)).date()
        search_end = timeutil.day_bounds(end_local)[1]
        if search_end <= search_start:
            raise ValueError("reschedule search range is fully elapsed")
        reason = " ".join(str(reason).split()).strip()
        if not reason:
            raise ValueError("reason must contain non-whitespace text")
        scheduled = await self.store.query_tasks(status="scheduled")
        overlapping = [task for task in scheduled if (
            task.get("scheduled_start") and task.get("scheduled_end")
            and _overlaps(
                _utc(task["scheduled_start"], "scheduled_start"),
                _utc(task["scheduled_end"], "scheduled_end"), start, end,
            )
        )]
        if not overlapping:
            return []
        fixed, corrections = await self._manual_fixed_points(
            overlapping, search_start, search_end
        )
        movable = [task for task in overlapping if int(task["id"]) not in fixed]
        if not movable:
            return corrections
        releasing_intervals: dict[int, tuple[datetime, datetime]] = {
            int(task["id"]): (
                _utc(task["scheduled_start"], "scheduled_start"),
                _utc(task["scheduled_end"], "scheduled_end"),
            )
            for task in movable
        }
        occupied = await query_schedule(
            self.store, self.calendar, search_start, search_end
        )
        movable_ids = {str(task["id"]) for task in movable}
        occupied = [
            block for block in occupied
            if not (block.source == "task" and block.source_id in movable_ids)
        ]
        # The planner sees all movable old intervals as releasable, while every
        # external/local/fixed block remains busy. Do not call
        # CalendarService.clear_kalendra_range: broad deletion cannot preserve
        # fixed or unknown-ID blocks. Each accepted move is selective instead.
        minimum = min(_task_duration(task) for task in movable)
        free = compute_free_blocks(
            search_start,
            search_end,
            minimum,
            _constraints(occupied, buffer_minutes=15),
        )
        blocks = [(f"block_{index}", block) for index, block in enumerate(free, 1)]
        facts, goals = await asyncio.gather(
            self.store.query_facts(active=True), self.store.query_goals(active=True)
        )
        try:
            placements = await self._plan_assignments(
                movable, blocks, facts, goals,
                context=(
                    f"Re-place only these displaced tasks because: {reason}. The returned reason "
                    "must name both that cause and the concrete constraint supporting the new slot."
                ),
            )
        except SchedulingPlanError:
            logger.exception("reschedule_aborted_without_calendar_mutation")
            return corrections
        by_task = {int(item.task["id"]): item for item in placements}
        decisions = list(corrections)
        committed_moves: list[tuple[ScheduleDecision, dict[str, Any]]] = []
        original_by_id = {int(task["id"]): dict(task) for task in movable}

        async def abort_cascade(failure: str) -> list[ScheduleDecision]:
            logger.error("reschedule_cascade_aborting", extra={
                "scheduler_event": {
                    "failure": failure,
                    "committed_move_ids": [item.id for item, _ in committed_moves],
                }
            })
            reversals = await self._rollback_cascade_moves(
                committed_moves, failure, trigger
            )
            transient_ids = {item.id for item, _ in committed_moves}
            stable = [item for item in decisions if item.id not in transient_ids]
            return stable + reversals

        late_fixed: set[int] = set()
        for index, task in enumerate(movable):
            task_id = int(task["id"])
            # Reconcile the current task and every still-promised old interval
            # immediately before mutation. If another task was manually fixed
            # after planning, its interval stops being releasable before this
            # task's fresh availability check.
            remaining_tasks: list[dict[str, Any]] = []
            expected_remaining = {
                int(item["id"])
                for item in movable[index:]
                if int(item["id"]) not in late_fixed
            }
            for remaining in movable[index:]:
                remaining_id = int(remaining["id"])
                if remaining_id in late_fixed:
                    continue
                fresh_remaining = await self.store.get_task(remaining_id)
                if fresh_remaining is not None:
                    remaining_tasks.append(fresh_remaining)
            loaded_remaining = {int(item["id"]) for item in remaining_tasks}
            if loaded_remaining != expected_remaining:
                missing = sorted(expected_remaining - loaded_remaining)
                return await abort_cascade(
                    f"task state disappeared for {missing}, invalidating a promised release"
                )
            just_fixed, late_corrections = await self._manual_fixed_points(
                remaining_tasks, search_start, search_end
            )
            decisions.extend(late_corrections)
            late_fixed.update(just_fixed)
            for fixed_id in just_fixed:
                releasing_intervals.pop(fixed_id, None)
            if just_fixed:
                return await abort_cascade(
                    "a manual or unverifiable calendar edit revoked promised old "
                    f"intervals for tasks {sorted(just_fixed)}"
                )
            fresh_task = next(
                (item for item in remaining_tasks if int(item["id"]) == task_id),
                None,
            )
            if fresh_task is None:
                return await abort_cascade(
                    f"task {task_id} disappeared before its promised release"
                )
            task = fresh_task
            placement = by_task.get(task_id)
            if placement is None:
                try:
                    unscheduled = await self._unschedule_locked(
                        task,
                        self._causal_reason(
                            reason, "no safe gap remains before its deadline"
                        ),
                        trigger,
                    )
                    decisions.append(unscheduled)
                    if unscheduled.action != "unscheduled":
                        releasing_intervals.pop(task_id, None)
                        return await abort_cascade(
                            f"task {task_id} could not release its original calendar block"
                        )
                except Exception as exc:
                    releasing_intervals.pop(task_id, None)
                    logger.exception("reschedule_unschedule_skipped", extra={
                        "scheduler_event": {
                            "task_id": task_id,
                            "committed_decision_ids": [item.id for item in decisions],
                            "error": str(exc),
                        }
                    })
                    return await abort_cascade(
                        f"task {task_id} failed to release its original block: {exc}"
                    )
                continue
            rationale = self._causal_reason(reason, placement.reasoning)
            try:
                moved = await self._schedule_task_locked(
                    task_id, placement.start, placement.end, rationale, trigger,
                    placement.facts_used, releasing_intervals,
                )
                decisions.append(moved)
                if moved.action in {"moved", "shortened", "extended"}:
                    committed_moves.append((moved, original_by_id[task_id]))
            except Exception as exc:
                # The old interval was not released; later placements must see
                # it as busy rather than relying on the original cascade plan.
                releasing_intervals.pop(task_id, None)
                logger.exception("reschedule_move_skipped", extra={
                    "scheduler_event": {
                        "task_id": task_id,
                        "committed_decision_ids": [item.id for item in decisions],
                        "error": str(exc),
                    }
                })
                return await abort_cascade(
                    f"task {task_id} failed to move and release its original block: {exc}"
                )
        return decisions

    async def reschedule(
        self,
        reason: str,
        affected_range: Any,
        *,
        trigger: Trigger = "conflict",
    ) -> list[ScheduleDecision]:
        """Selectively move automatic blocks while preserving fixed points."""
        async with self._mutation_lock:
            return await self._reschedule_locked(reason, affected_range, trigger)

    async def resolve_conflicts(self, start: datetime, end: datetime) -> list[ScheduleDecision]:
        """Compatibility alias for a conflict-triggered reschedule."""
        return await self.reschedule(
            "a new calendar conflict was added", (start, end), trigger="conflict"
        )

    async def detect_conflicts(
        self,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[ScheduleDecision]:
        """Find external overlaps after a complete read and trigger one cascade."""
        range_start = _utc(start, "start") if start is not None else timeutil.now_utc()
        range_end = _utc(end, "end") if end is not None else (
            range_start + timedelta(days=config.SCHEDULER_LOOKAHEAD_DAYS)
        )
        now = timeutil.now_utc()
        if range_end <= now:
            raise ValueError("cannot detect conflicts in a fully elapsed range")
        range_start = max(range_start, now)
        if range_end <= range_start:
            raise ValueError("end must be later than start")
        async with self._mutation_lock:
            scheduled = await self.store.query_tasks(status="scheduled")
            scheduled = [task for task in scheduled if (
                task.get("scheduled_start") and task.get("scheduled_end")
            )]
            if not scheduled:
                return []
            # Reconcile every managed scheduled block before selecting by the
            # requested range. A hand-dragged block may have moved into the
            # range even though its stale SQLite interval was outside it.
            calendar_snapshot = await self._fresh_calendar_snapshot(
                scheduled, range_start, range_end
            )
            _fixed, corrections = await self._manual_fixed_points(
                scheduled,
                range_start,
                range_end,
                calendar_snapshot=calendar_snapshot,
            )
            # Reuse the genuinely fresh, cache-busted Google snapshot instead
            # of issuing an exact query that may hit CalendarService's cache.
            # Local events and task state are uncached DB reads taken after
            # manual reconciliation.
            scheduled, local_records = await asyncio.gather(
                self.store.query_tasks(status="scheduled"),
                self.store.query_events(range_start, range_end),
            )
            relevant = [task for task in scheduled if (
                task.get("scheduled_start") and task.get("scheduled_end")
                and _overlaps(
                    _utc(task["scheduled_start"], "scheduled_start"),
                    _utc(task["scheduled_end"], "scheduled_end"),
                    range_start,
                    range_end,
                )
            )]
            managed_ids = {
                str(task.get("gcal_event_id") or "").strip()
                for task in scheduled if task.get("gcal_event_id")
            }
            externals = [
                _external_block(record, "gcal")
                for record in calendar_snapshot[0]
                if _event_id(record) not in managed_ids
                and _utc(
                    record.get("start_time", record.get("start")), "Google start"
                ) < range_end
                and range_start < _utc(
                    record.get("end_time", record.get("end")), "Google end"
                )
            ]
            externals.extend(
                _external_block(record, "event") for record in local_records
            )
            task_blocks = [
                ScheduleBlock(
                    start=_utc(task["scheduled_start"], "scheduled_start"),
                    end=_utc(task["scheduled_end"], "scheduled_end"),
                    title=str(task.get("title") or "Untitled Task"),
                    source="task",
                    source_id=str(task["id"]),
                    metadata=dict(task),
                )
                for task in relevant
            ]
            blockers = externals + task_blocks
            conflicts: list[tuple[dict[str, Any], ScheduleBlock]] = []
            for task in relevant:
                task_start = _utc(task["scheduled_start"], "scheduled_start")
                task_end = _utc(task["scheduled_end"], "scheduled_end")
                own_id = str(task.get("gcal_event_id") or "").strip()
                for block in blockers:
                    if block.source == "task" and block.source_id == str(task["id"]):
                        continue
                    if own_id and block.source == "gcal" and block.source_id == own_id:
                        continue
                    if _overlaps(task_start, task_end, block.start, block.end):
                        conflicts.append((task, block))
                        break
            if not conflicts:
                return corrections
            conflict_start = min(
                _utc(task["scheduled_start"], "scheduled_start") for task, _ in conflicts
            )
            conflict_end = max(
                _utc(task["scheduled_end"], "scheduled_end") for task, _ in conflicts
            )
            titles = list(dict.fromkeys(block.title for _, block in conflicts))
            cause = f"{', '.join(titles[:2])} now overlaps the work block"
            cascade = await self._reschedule_locked(
                cause, (conflict_start, conflict_end), "conflict"
            )
            correction_ids = {item.id for item in corrections}
            return corrections + [item for item in cascade if item.id not in correction_ids]

    async def format_change_summary(
        self,
        decisions: Sequence[Any],
        *,
        mark_surfaced: bool = True,
    ) -> str:
        """Batch changes into one casual message.

        ``mark_surfaced`` defaults to the original contract. Callers with a
        delivery acknowledgement should pass ``False`` and invoke
        :meth:`mark_decisions_surfaced` only after Telegram confirms delivery;
        marking here cannot prove the message reached the user.
        """
        clauses: list[str] = []
        for decision in decisions:
            task_id = int(_record_value(decision, "task_id"))
            title = _record_value(decision, "title")
            if not title:
                task = await self.store.get_task(task_id)
                title = task.get("title") if task else f"task {task_id}"
            action = str(_record_value(decision, "action"))
            start = _utc(_record_value(decision, "start"), "decision start")
            reason = " ".join(str(_record_value(decision, "reasoning", "")).split()).rstrip(" .")
            short_reason = reason
            if config.REASONING_VERBOSITY == "brief" and len(short_reason) > 100:
                short_reason = short_reason[:97].rsplit(" ", 1)[0] + "…"
            local_time = timeutil.to_local(start).strftime("%-I:%M").lower()
            if action == "unscheduled":
                clause = f"left {title} unscheduled — {short_reason}"
            elif action == "moved":
                clause = f"moved {title} to {local_time} — {short_reason}"
            elif action == "shortened":
                clause = f"shortened {title} at {local_time} — {short_reason}"
            elif action == "extended":
                clause = f"extended {title} at {local_time} — {short_reason}"
            else:
                clause = f"put {title} at {local_time} — {short_reason}"
            clauses.append(clause)
        message = "; ".join(clauses)
        if mark_surfaced:
            await self.mark_decisions_surfaced(decisions)
        return message

    async def mark_decisions_surfaced(self, decisions: Sequence[Any]) -> None:
        """Acknowledge that a batch of decisions was delivered to the user."""
        ids = [
            int(decision_id)
            for decision in decisions
            if (decision_id := _record_value(decision, "id")) is not None
        ]
        for decision_id in dict.fromkeys(ids):
            await self.store.mark_decision_surfaced(decision_id)

    async def explain_schedule(self, task_id: int) -> list[dict[str, object]]:
        """Return recorded decision history without reconstructing rationale."""
        return await self.store.get_schedule_decisions(task_id)


async def create_scheduler_engine(
    store: Store, calendar: CalendarService
) -> SchedulerEngine:
    """Create a scheduler without making network calls."""
    return SchedulerEngine(store, calendar)
