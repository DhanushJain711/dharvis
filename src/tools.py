"""Canonical strict OpenAI function-calling schemas for the assistant."""

from __future__ import annotations

from typing import Any

JsonSchema = dict[str, Any]
ToolSchema = dict[str, Any]

CATEGORIES = ["school", "work", "personal", "fitness", "career", "errand"]
TASK_STATUSES = ["pending", "scheduled", "completed", "dropped"]


def _object(properties: dict[str, JsonSchema]) -> JsonSchema:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


def _nullable(kind: str, description: str, **extra: Any) -> JsonSchema:
    return {"type": [kind, "null"], "description": description, **extra}


def _tool(name: str, description: str, properties: dict[str, JsonSchema]) -> ToolSchema:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "strict": True,
            "parameters": _object(properties),
        },
    }


_task_input = _object(
    {
        "title": {"type": "string", "description": "Concise action-oriented task title."},
        "description": _nullable("string", "Supporting detail, or null when none was given."),
        "deadline": _nullable("string", "Resolved UTC ISO-8601 deadline, or null when there is no deadline."),
        "estimated_minutes": _nullable("integer", "Positive duration estimate in minutes, or null when unknown.", minimum=1),
        "category": {"type": "string", "enum": CATEGORIES, "description": "The task's life-area category."},
        "energy": {"type": "string", "enum": ["deep_focus", "light", "errand"], "description": "The attention or mobility mode the task requires."},
        "priority": {"type": "string", "enum": ["low", "medium", "high"], "description": "User importance, not deadline urgency."},
        "goal_id": _nullable("integer", "Associated goal id, or null when the task is unrelated to a goal.", minimum=1),
    }
)

_event_input = _object(
    {
        "title": {"type": "string", "description": "Concise event title."},
        "description": _nullable("string", "Supporting detail, or null when none was given."),
        "start": {"type": "string", "description": "Event start as an aware UTC ISO-8601 datetime."},
        "end": {"type": "string", "description": "Event end as an aware UTC ISO-8601 datetime later than start."},
        "location": _nullable("string", "Physical or virtual location, or null when unspecified."),
        "category": _nullable("string", "Optional life-area category.", enum=CATEGORIES + [None]),
    }
)


TOOLS: list[ToolSchema] = [
    _tool(
        "add_task",
        "Create one or more actionable tasks. Use this for work the user must complete, not for fixed-time appointments. Send all tasks from one user message in one call.",
        {"tasks": {"type": "array", "description": "Every task to create.", "items": _task_input, "minItems": 1}},
    ),
    _tool(
        "add_event",
        "Create one or more fixed-time calendar events. Use this for appointments or commitments with known start and end times, not flexible work. Send all events from one user message in one call.",
        {"events": {"type": "array", "description": "Every event to create.", "items": _event_input, "minItems": 1}},
    ),
    _tool(
        "update_task",
        "Change fields on an existing task. Query tasks first when the id is not already present in context; use schedule_task for placement changes. Null field values mean unchanged; nullable fields are cleared only by naming them in clear_fields.",
        {
            "task_id": {"type": "integer", "minimum": 1, "description": "Exact local task id."},
            "title": _nullable("string", "Replacement title, or null to leave unchanged."),
            "description": _nullable("string", "Replacement description, or null to leave unchanged."),
            "deadline": _nullable("string", "Replacement UTC deadline, or null to leave unchanged."),
            "estimated_minutes": _nullable("integer", "Replacement positive estimate, or null to leave unchanged.", minimum=1),
            "category": _nullable("string", "Replacement category, or null to leave unchanged.", enum=CATEGORIES + [None]),
            "energy": _nullable("string", "Replacement energy mode, or null to leave unchanged.", enum=["deep_focus", "light", "errand", None]),
            "priority": _nullable("string", "Replacement priority, or null to leave unchanged.", enum=["low", "medium", "high", None]),
            "status": _nullable("string", "Replacement lifecycle status, or null to leave unchanged.", enum=TASK_STATUSES + [None]),
            "goal_id": _nullable("integer", "Replacement goal id, or null to leave unchanged.", minimum=1),
            "clear_fields": {
                "type": "array",
                "items": {"type": "string", "enum": ["description", "deadline", "estimated_minutes", "goal_id"]},
                "description": "Nullable fields to set to null. Use an empty array when clearing nothing, and leave each named field's value null.",
            },
        },
    ),
    _tool(
        "update_event",
        "Change fields on an existing local or synchronized event. Query the schedule first when the id is ambiguous. Null field values mean unchanged; nullable fields are cleared only by naming them in clear_fields.",
        {
            "event_id": {"type": "integer", "minimum": 1, "description": "Exact local event id."},
            "title": _nullable("string", "Replacement title, or null to leave unchanged."),
            "description": _nullable("string", "Replacement description, or null to leave unchanged."),
            "start": _nullable("string", "Replacement aware UTC start, or null to leave unchanged."),
            "end": _nullable("string", "Replacement aware UTC end, or null to leave unchanged."),
            "location": _nullable("string", "Replacement location, or null to leave unchanged."),
            "category": _nullable("string", "Replacement category, or null to leave unchanged."),
            "clear_fields": {
                "type": "array",
                "items": {"type": "string", "enum": ["description", "location", "category"]},
                "description": "Nullable fields to set to null. Use an empty array when clearing nothing, and leave each named field's value null.",
            },
        },
    ),
    _tool(
        "complete_task",
        "Mark an existing task completed and optionally record how long it actually took. Do not use update_task for normal completion.",
        {
            "task_id": {"type": "integer", "minimum": 1, "description": "Exact task id to complete."},
            "actual_minutes": _nullable("integer", "Observed minutes spent, or null when unknown.", minimum=0),
        },
    ),
    _tool("delete_task", "Drop an existing task the user no longer intends to do. Query first if the id is uncertain.", {"task_id": {"type": "integer", "minimum": 1, "description": "Exact task id to drop."}}),
    _tool("delete_event", "Delete an existing event only when the user explicitly cancels or removes it. Query first if the id is uncertain.", {"event_id": {"type": "integer", "minimum": 1, "description": "Exact local event id to delete."}}),
    _tool(
        "query_schedule",
        "Read the merged schedule of Google Calendar events, local events, and scheduled task work blocks over a UTC range. Use before answering availability or calendar questions.",
        {
            "start": {"type": "string", "description": "Inclusive aware UTC ISO-8601 range start."},
            "end": {"type": "string", "description": "Exclusive aware UTC ISO-8601 range end."},
        },
    ),
    _tool(
        "query_tasks",
        "Find tasks by lifecycle state, category, and deadline window. Use this instead of guessing task ids or task state.",
        {
            "status": _nullable("string", "Status filter, or null for every status.", enum=TASK_STATUSES + [None]),
            "category": _nullable("string", "Category filter, or null for every category.", enum=CATEGORIES + [None]),
            "due_before": _nullable("string", "Exclusive aware UTC deadline upper bound, or null."),
            "due_after": _nullable("string", "Inclusive aware UTC deadline lower bound, or null."),
        },
    ),
    _tool(
        "find_free_blocks",
        "Calculate genuinely open intervals after merging every calendar source and scheduled task. Use before choosing or proposing a work slot.",
        {
            "start": {"type": "string", "description": "Inclusive aware UTC search start."},
            "end": {"type": "string", "description": "Exclusive aware UTC search end."},
            "min_minutes": {"type": "integer", "minimum": 1, "description": "Minimum usable block length in minutes."},
        },
    ),
    _tool(
        "schedule_task",
        "Place or move a task work block on the dedicated Kalendra calendar after checking free time. This autonomously changes the schedule; its handler must atomically persist the placement and contemporaneous rationale in one transaction.",
        {
            "task_id": {"type": "integer", "minimum": 1, "description": "Exact task id being placed."},
            "start": {"type": "string", "description": "Aware UTC work-block start."},
            "end": {"type": "string", "description": "Aware UTC work-block end later than start."},
            "reasoning": {"type": "string", "description": "Required one-sentence plain-language explanation of why this slot was chosen, referencing the constraint or habit that drove it.", "minLength": 1},
            "trigger": {"type": "string", "enum": ["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"], "description": "The condition that caused this scheduling decision."},
        },
    ),
    _tool("explain_schedule", "Retrieve the complete decision history and stored reasoning for a task's current placement. Use for 'why is this scheduled here?' questions; never reconstruct a rationale from current conditions.", {"task_id": {"type": "integer", "minimum": 1, "description": "Exact task id whose placement should be explained."}}),
    _tool(
        "add_fact",
        "Store a durable user preference, constraint, or habit that can improve future planning. Do not store one-off requests as facts.",
        {
            "content": {"type": "string", "description": "Self-contained natural-language fact."},
            "category": {"type": "string", "description": "Stable grouping such as scheduling, energy, preference, or constraint."},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1, "description": "Confidence from 0 through 1."},
            "source": {"type": "string", "enum": ["seed", "extracted", "explicit"], "description": "How the fact was learned."},
        },
    ),
    _tool(
        "update_fact",
        "Correct, reconfirm, or deactivate an existing durable fact. Query facts first when its id is unknown.",
        {
            "fact_id": {"type": "integer", "minimum": 1, "description": "Exact fact id."},
            "content": _nullable("string", "Corrected content, or null to leave unchanged."),
            "category": _nullable("string", "Corrected category, or null to leave unchanged."),
            "confidence": _nullable("number", "New confidence from 0 through 1, or null to leave unchanged.", minimum=0, maximum=1),
            "active": _nullable("boolean", "Whether the fact remains active, or null to leave unchanged."),
        },
    ),
    _tool(
        "query_facts",
        "Retrieve durable preferences and constraints relevant to a decision. Use before explaining or making schedule choices that depend on habits.",
        {
            "category": _nullable("string", "Category filter, or null for every category."),
            "active": _nullable("boolean", "Active-state filter, or null for both active and inactive."),
            "min_confidence": _nullable("number", "Minimum confidence from 0 through 1, or null for no threshold.", minimum=0, maximum=1),
        },
    ),
    _tool(
        "add_goal",
        "Create a recurring quantitative goal when the user commits to a number of hours or sessions per week or month.",
        {
            "title": {"type": "string", "description": "Concise goal title."},
            "target_amount": {"type": "number", "exclusiveMinimum": 0, "description": "Required amount per period."},
            "target_unit": {"type": "string", "enum": ["hours", "sessions"], "description": "How progress is measured."},
            "period": {"type": "string", "enum": ["week", "month"], "description": "Goal reset period."},
            "category": {"type": "string", "description": "Life-area category for the goal."},
        },
    ),
    _tool(
        "log_goal_progress",
        "Record progress toward an existing recurring goal after work is completed or the user reports progress.",
        {
            "goal_id": {"type": "integer", "minimum": 1, "description": "Exact goal id."},
            "amount": {"type": "number", "exclusiveMinimum": 0, "description": "Progress amount in the goal's target unit."},
            "source": {"type": "string", "enum": ["task", "manual", "inferred"], "description": "How this progress was observed."},
            "logged_at": {"type": "string", "description": "Aware UTC time when the progress occurred."},
        },
    ),
    _tool(
        "query_goals",
        "Read recurring goals and their progress. Use before linking tasks, logging progress, or planning goal quota work.",
        {
            "active": _nullable("boolean", "Active-state filter, or null for all goals."),
            "category": _nullable("string", "Category filter, or null for all categories."),
        },
    ),
    _tool(
        "resolve_date",
        "Resolve a natural-language date or time phrase using the user's timezone. Use whenever a date phrase could be relative or ambiguous instead of calculating dates mentally.",
        {"phrase": {"type": "string", "description": "The user's date phrase verbatim, such as 'next Tuesday afternoon'."}},
    ),
]

TOOLS_BY_NAME: dict[str, ToolSchema] = {
    tool["function"]["name"]: tool for tool in TOOLS
}


def get_tool_schemas() -> list[ToolSchema]:
    """Return the canonical OpenAI tool schema list."""
    return TOOLS
