"""Runtime composition and the canonical implementations of agent tools."""

from __future__ import annotations

import re
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from . import timeutil
from .calendar_service import (
    FIXED_EVENT_KIND,
    CalendarService,
    normalize_event_color_id,
)
from .config import config
from .facts_engine import FactsEngine
from .freebusy import ScheduleBlock, find_free_blocks, overlapping_blocks, query_schedule
from .scheduler_engine import SchedulerEngine
from .store import Store

ToolHandler = Callable[..., Awaitable[Any]]
logger = logging.getLogger(__name__)


class _EventApplyError(RuntimeError):
    """An event write failed with explicit compensation status."""

    def __init__(self, message: str, *, compensated: bool) -> None:
        super().__init__(message)
        self.compensated = compensated
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP = {
    "about", "after", "before", "because", "from", "that", "the", "their",
    "this", "with", "your", "you", "and", "for", "into", "slot", "time",
}


def _aware(value: str | datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def jsonable(value: Any) -> Any:
    """Convert runtime records and dataclasses into tool-result JSON values."""
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be returned from a tool")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Mapping):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    return value


def _words(value: str) -> set[str]:
    return {
        word for word in _WORD_RE.findall(value.lower())
        if len(word) >= 3 and word not in _STOP
    }


async def build_tool_handlers(
    store: Store,
    calendar: CalendarService,
    scheduler: SchedulerEngine,
    facts_engine: FactsEngine,
) -> dict[str, ToolHandler]:
    """Bind every schema in ``src.tools`` to real application services."""

    async def add_task(tasks: list[dict[str, Any]]) -> Any:
        payloads: list[dict[str, Any]] = []
        for task in tasks:
            item = dict(task)
            item["deadline"] = _aware(item.get("deadline"), "deadline")
            # A user-supplied estimate is authoritative.  Otherwise reuse only
            # deterministic evidence from completed tasks, falling back to the
            # configured default when the task family has no usable history.
            if item.get("estimated_minutes") is None:
                inferred = await store.infer_task_duration(
                    str(item.get("title") or ""),
                    str(item.get("category") or "personal"),
                    str(item.get("energy") or "light"),
                    item.get("series_key"),
                )
                item["series_key"] = inferred["series_key"]
                item["estimated_minutes"] = (
                    inferred["estimated_minutes"] or config.DEFAULT_TASK_MINUTES
                )
                item["estimate_source"] = inferred["estimate_source"]
            else:
                item["estimate_source"] = "user"
            payloads.append(item)
        return jsonable(await store.add_tasks(payloads))

    def _validate_event(event: dict[str, Any]) -> dict[str, Any]:
        item = dict(event)
        if item.get("color_id") is not None:
            item["color_id"] = normalize_event_color_id(item["color_id"])
        item["start"] = _aware(item.get("start"), "start")
        item["end"] = _aware(item.get("end"), "end")
        if item["start"] is None or item["end"] is None or item["end"] <= item["start"]:
            raise ValueError("event end must be later than start")
        return item

    async def _conflicts_for(
        events: list[dict[str, Any]], *, ignore_event_id: int | None = None
    ) -> list[dict[str, Any]]:
        """Check candidates against the merged live schedule and one another.

        The overlap helper implements half-open intervals, so an event ending at
        another's start remains valid while even a one-second intersection is a
        conflict.
        """
        start = min(item["start"] for item in events)
        end = max(item["end"] for item in events)
        # Availability must not be decided from CalendarService's short-lived
        # cache when it is about to authorize an external write.
        occupied = await query_schedule(store, calendar, start, end, force_refresh=True)
        if ignore_event_id is not None:
            occupied = [
                block for block in occupied
                if not (block.source == "event" and block.source_id == str(ignore_event_id))
            ]
        conflicts: list[dict[str, Any]] = []
        candidates = list(occupied)
        for index, item in enumerate(events):
            for block in overlapping_blocks(candidates, item["start"], item["end"]):
                conflicts.append({
                    "event_index": index,
                    "proposed_title": item["title"],
                    "start": item["start"], "end": item["end"],
                    "conflict": jsonable(block),
                })
            # Candidate events must be checked too, making a multi-event create
            # all-or-nothing rather than partially writing a conflicting batch.
            candidates.append(ScheduleBlock(
                item["start"], item["end"], str(item["title"]), "event",
                f"candidate-{index}", {},
            ))
        return conflicts

    async def _apply_event_create(events: list[dict[str, Any]]) -> dict[str, Any]:
        created_remote: list[dict[str, Any]] = []
        try:
            for item in events:
                created_remote.append(await calendar.create_event(
                    item, "the user requested this fixed-time event",
                    category=item.get("category"), kind=FIXED_EVENT_KIND,
                ))
            local_events = []
            for item, created in zip(events, created_remote, strict=True):
                local = dict(item)
                local["source"] = "bot"
                local["gcal_event_id"] = created["gcal_event_id"]
                # Google is authoritative for the effective event-level color.
                # In particular, its response tells us which deterministic
                # default was applied when the caller supplied null.
                if "color_id" in created:
                    local["color_id"] = created["color_id"]
                local_events.append(local)
            records = await store.add_events(local_events)
        except Exception as exc:
            compensated = True
            for created in created_remote:
                try:
                    await calendar.delete_event(str(created["gcal_event_id"]))
                except Exception:
                    # A later reconciliation can find an orphaned owned event;
                    # never hide the original failure by replacing it here.
                    compensated = False
            raise _EventApplyError(
                "event creation failed", compensated=compensated
            ) from exc
        schedule_changes: list[Any] = []
        reconciliation_pending = False
        try:
            for item in events:
                schedule_changes.extend(await scheduler.detect_conflicts(item["start"], item["end"]))
        except Exception:
            # The event and its local record have committed.  Do not turn a
            # delayed reconciliation failure into a retryable event write.
            logger.exception("event_conflict_reconciliation_failed")
            schedule_changes = []
            reconciliation_pending = True
        result: dict[str, Any] = {"events": records, "schedule_changes": schedule_changes}
        if reconciliation_pending:
            result["reconciliation_pending"] = True
            result["warning"] = "The event was added, but schedule reconciliation will retry shortly."
        return result

    async def _proposal(
        operation: str, payload: dict[str, Any], conflicts: list[dict[str, Any]]
    ) -> dict[str, Any]:
        proposal = await store.create_event_change_proposal(
            operation, jsonable(payload), jsonable(conflicts),
            timeutil.now_utc() + timedelta(minutes=15),
        )
        return {
            "confirmation_required": True,
            "proposal_id": proposal["id"],
            "expires_at": proposal["expires_at"],
            "conflicts": conflicts,
        }

    def _conflict_key(conflict: dict[str, Any]) -> tuple[Any, ...]:
        """Stable identity for a disclosed blocking interval."""
        block = conflict.get("conflict", {})
        return (
            conflict.get("event_index"), block.get("source"),
            str(block.get("source_id")), block.get("start"), block.get("end"),
        )

    def _new_conflicts(
        current: list[dict[str, Any]], disclosed: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        disclosed_keys = {_conflict_key(item) for item in disclosed}
        return [item for item in current if _conflict_key(item) not in disclosed_keys]

    async def _claim_proposal(proposal_id: str, now: datetime) -> tuple[str, dict[str, Any]] | None:
        try:
            claimed = await store.claim_event_change_proposal(proposal_id, now)
        except (KeyError, ValueError):
            return None
        return str(claimed["claim_token"]), claimed

    async def add_event(events: list[dict[str, Any]]) -> Any:
        validated: list[dict[str, Any]] = []
        for event in events:
            validated.append(_validate_event(event))
        conflicts = await _conflicts_for(validated)
        if conflicts:
            return jsonable(await _proposal("create", {"events": validated}, conflicts))
        # Recheck after planning, immediately before calendar mutation.
        conflicts = await _conflicts_for(validated)
        if conflicts:
            return jsonable(await _proposal("create", {"events": validated}, conflicts))
        return jsonable(await _apply_event_create(validated))

    async def update_task(task_id: int, clear_fields: list[str], **changes: Any) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        payload = {key: value for key, value in changes.items() if value is not None}
        if "deadline" in payload:
            payload["deadline"] = _aware(payload["deadline"], "deadline")
        payload["clear_fields"] = clear_fields
        updated = await store.update_task(task_id, payload)
        schedule_changes: list[Any] = []
        deadline_changed = "deadline" in payload or "deadline" in clear_fields
        if (
            deadline_changed
            and current.get("scheduled_start")
            and current.get("scheduled_end")
        ):
            new_deadline = updated.get("deadline")
            deadline_text = (
                timeutil.to_local(new_deadline).strftime("%a %-I:%M%p")
                if isinstance(new_deadline, datetime)
                else "no deadline"
            )
            schedule_changes = await scheduler.reschedule(
                f"the deadline changed to {deadline_text}",
                (current["scheduled_start"], current["scheduled_end"]),
                trigger="deadline_shift",
            )
        result = dict(updated)
        result["schedule_changes"] = schedule_changes
        return jsonable(result)

    async def update_event(event_id: int, clear_fields: list[str], **changes: Any) -> Any:
        current = await store.get_event(event_id)
        if current is None:
            raise KeyError(f"Event {event_id} does not exist")
        if current.get("source") != "bot":
            raise ValueError("external Google Calendar events are read-only")
        allowed_clear_fields = {"description", "location", "category", "color_id"}
        invalid_clear_fields = set(clear_fields) - allowed_clear_fields
        if invalid_clear_fields:
            raise ValueError(
                f"event fields cannot be cleared: {sorted(invalid_clear_fields)}"
            )
        contradictory_fields = {
            field for field in clear_fields if changes.get(field) is not None
        }
        if contradictory_fields:
            raise ValueError(
                "event fields cannot be both cleared and assigned: "
                f"{sorted(contradictory_fields)}"
            )
        payload = {key: value for key, value in changes.items() if value is not None}
        if "color_id" in payload:
            payload["color_id"] = normalize_event_color_id(payload["color_id"])
        for key in ("start", "end"):
            if key in payload:
                payload[key] = _aware(payload[key], key)
        proposed = dict(current)
        proposed.update(payload)
        proposed["start"] = payload.get("start", proposed.get("start_time"))
        proposed["end"] = payload.get("end", proposed.get("end_time"))
        proposed = _validate_event(proposed)
        proposal_payload = {
            "event_id": event_id,
            "clear_fields": clear_fields,
            "changes": payload,
        }
        changed_fields = set(payload) | set(clear_fields)
        color_only = bool(changed_fields) and changed_fields <= {"color_id"}
        if not color_only:
            conflicts = await _conflicts_for([proposed], ignore_event_id=event_id)
            if conflicts:
                return jsonable(await _proposal("update", proposal_payload, conflicts))
            # Freshly read the merged schedule immediately before the external
            # mutation; a concurrent calendar edit becomes a new proposal.
            conflicts = await _conflicts_for([proposed], ignore_event_id=event_id)
            if conflicts:
                return jsonable(await _proposal("update", proposal_payload, conflicts))
        return jsonable(await _apply_event_update(
            event_id, current, payload, clear_fields, recheck=False,
            skip_reconciliation=color_only,
        ))

    async def _apply_event_update(
        event_id: int,
        current: dict[str, Any],
        payload: dict[str, Any],
        clear_fields: list[str],
        *,
        recheck: bool,
        skip_reconciliation: bool = False,
    ) -> dict[str, Any]:
        proposed = dict(current)
        proposed.update(payload)
        proposed["start"] = payload.get("start", proposed.get("start_time"))
        proposed["end"] = payload.get("end", proposed.get("end_time"))
        proposed = _validate_event(proposed)
        if recheck:
            conflicts = await _conflicts_for([proposed], ignore_event_id=event_id)
            if conflicts:
                raise ValueError("the calendar changed and this event still conflicts")
        gcal_id = str(current.get("gcal_event_id") or "")
        remote_updated: dict[str, Any] | None = None
        remote_preimage_color: str | None = None
        remote_mutated = False
        if gcal_id:
            calendar_payload = dict(payload)
            for field in clear_fields:
                calendar_payload[field] = None
            if set(calendar_payload) & {
                "title", "description", "start", "end", "start_time",
                "end_time", "location", "category", "color_id",
            }:
                # Compensation must restore Google's live preimage, not a
                # potentially stale SQLite value (the user may have recolored
                # the event directly in Google Calendar).
                live_event = await calendar.get_owned_event(gcal_id)
                if live_event.get("color_id") is not None:
                    remote_preimage_color = normalize_event_color_id(
                        live_event["color_id"]
                    )
                remote_updated = await calendar.update_event(gcal_id, calendar_payload)
                remote_mutated = True
        store_payload = dict(payload)
        # Persist Google's returned effective color after a set. Clearing stays
        # represented by clear_fields so SQLite stores null/inherited state.
        local_clear_fields = list(clear_fields)
        if "color_id" not in clear_fields and remote_updated is not None and "color_id" in remote_updated:
            if remote_updated["color_id"] is None:
                # This is a local persistence instruction only. The Google
                # patch already happened above, so do not route it back through
                # the calendar boundary a second time.
                store_payload.pop("color_id", None)
                local_clear_fields.append("color_id")
            else:
                store_payload["color_id"] = normalize_event_color_id(
                    remote_updated["color_id"]
                )
        store_payload["clear_fields"] = local_clear_fields
        try:
            updated = await store.update_event(event_id, store_payload)
        except Exception as exc:
            if remote_mutated:
                try:
                    await calendar.update_event(gcal_id, {
                        "title": current.get("title"),
                        "description": current.get("description"),
                        "start_time": current.get("start_time"),
                        "end_time": current.get("end_time"),
                        "location": current.get("location"),
                        "category": current.get("category"),
                        "color_id": remote_preimage_color,
                    })
                except Exception as rollback_error:
                    raise _EventApplyError(
                        "event update failed and calendar rollback could not be verified",
                        compensated=False,
                    ) from rollback_error
            raise _EventApplyError("event update failed", compensated=True) from exc
        start = updated.get("start_time")
        end = updated.get("end_time")
        reconciliation_pending = False
        try:
            schedule_changes = (
                await scheduler.detect_conflicts(start, end)
                if (
                    not skip_reconciliation
                    and isinstance(start, datetime)
                    and isinstance(end, datetime)
                )
                else []
            )
        except Exception:
            logger.exception("event_conflict_reconciliation_failed")
            schedule_changes = []
            reconciliation_pending = True
        result = dict(updated)
        result["schedule_changes"] = schedule_changes
        if reconciliation_pending:
            result["reconciliation_pending"] = True
            result["warning"] = "The event was updated, but schedule reconciliation will retry shortly."
        return result

    async def confirm_event_change(proposal_id: str) -> Any:
        proposal = await store.get_event_change_proposal(proposal_id)
        if proposal is None:
            return {"applied": False, "reason": "proposal_not_found"}
        now = timeutil.now_utc()
        if proposal["consumed_at"] is not None:
            return {"applied": False, "reason": "proposal_already_used"}
        if proposal["expires_at"] <= now:
            return {"applied": False, "reason": "proposal_expired"}
        payload = proposal["payload"]
        operation = proposal["operation"]
        if operation == "create":
            events = [_validate_event(event) for event in payload["events"]]
            conflicts = await _conflicts_for(events)
            if _new_conflicts(conflicts, proposal["conflicts"]):
                return jsonable(await _proposal("create", {"events": events}, conflicts))
            claim = await _claim_proposal(proposal_id, now)
            if claim is None:
                return {"applied": False, "reason": "proposal_unavailable"}
            token, _ = claim
            try:
                result = await _apply_event_create(events)
            except _EventApplyError as exc:
                if exc.compensated:
                    await store.release_event_change_proposal(proposal_id, token)
                raise
            await store.finalize_event_change_proposal(proposal_id, token)
            return jsonable(result)
        event_id = int(payload["event_id"])
        current = await store.get_event(event_id)
        if current is None:
            return {"applied": False, "reason": "event_not_found"}
        changes = dict(payload["changes"])
        for key in ("start", "end"):
            if key in changes:
                changes[key] = _aware(changes[key], key)
        proposed = dict(current)
        proposed.update(changes)
        proposed["start"] = changes.get("start", proposed.get("start_time"))
        proposed["end"] = changes.get("end", proposed.get("end_time"))
        conflicts = await _conflicts_for([_validate_event(proposed)], ignore_event_id=event_id)
        if _new_conflicts(conflicts, proposal["conflicts"]):
            return jsonable(await _proposal("update", payload, conflicts))
        claim = await _claim_proposal(proposal_id, now)
        if claim is None:
            return {"applied": False, "reason": "proposal_unavailable"}
        token, _ = claim
        try:
            result = await _apply_event_update(
                event_id, current, changes, list(payload["clear_fields"]),
                recheck=False,
            )
        except _EventApplyError as exc:
            if exc.compensated:
                await store.release_event_change_proposal(proposal_id, token)
            raise
        await store.finalize_event_change_proposal(proposal_id, token)
        return jsonable(result)

    async def complete_task(task_id: int, actual_minutes: int | None) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_work_block(gcal_id)
        return jsonable(await store.complete_task(task_id, actual_minutes))

    async def delete_task(task_id: int) -> Any:
        current = await store.get_task(task_id)
        if current is None:
            raise KeyError(f"Task {task_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_work_block(gcal_id)
        return jsonable(await store.delete_task(task_id))

    async def delete_event(event_id: int) -> Any:
        current = await store.get_event(event_id)
        if current is None:
            raise KeyError(f"Event {event_id} does not exist")
        gcal_id = str(current.get("gcal_event_id") or "")
        if gcal_id:
            await calendar.delete_event(gcal_id)
        if not await store.delete_event(event_id):
            raise RuntimeError(f"Event {event_id} could not be deleted locally")
        return {"deleted": True, "event_id": event_id}

    async def query_schedule_tool(start: str, end: str) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        return jsonable(await query_schedule(store, calendar, start_dt, end_dt))

    async def query_tasks(
        status: str | None,
        category: str | None,
        due_before: str | None,
        due_after: str | None,
    ) -> Any:
        return jsonable(await store.query_tasks(
            status=status,
            category=category,
            due_before=_aware(due_before, "due_before"),
            due_after=_aware(due_after, "due_after"),
        ))

    async def add_reminder(reminders: list[dict[str, Any]]) -> Any:
        payloads: list[dict[str, Any]] = []
        for reminder in reminders:
            item = dict(reminder)
            moment = _aware(item.get("remind_at"), "remind_at")
            assert moment is not None
            item["remind_at"] = moment
            payloads.append(item)
        return jsonable(await store.add_reminders(payloads))

    async def update_reminder(
        reminder_id: int,
        message: str | None,
        remind_at: str | None,
    ) -> Any:
        changes: dict[str, Any] = {}
        if message is not None:
            changes["message"] = message
        if remind_at is not None:
            moment = _aware(remind_at, "remind_at")
            assert moment is not None
            changes["remind_at"] = moment
        if not changes:
            raise ValueError("provide a new reminder message or due time")
        return jsonable(await store.update_reminder(reminder_id, changes))

    async def cancel_reminder(reminder_id: int) -> Any:
        return jsonable(await store.cancel_reminder(reminder_id))

    async def query_reminders(
        status: str | None,
        remind_before: str | None,
        remind_after: str | None,
    ) -> Any:
        before = _aware(remind_before, "remind_before")
        after = _aware(remind_after, "remind_after")
        return jsonable(await store.query_reminders(status, before, after))

    async def find_free_blocks_tool(start: str, end: str, min_minutes: int) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        return jsonable(await find_free_blocks(
            store, calendar, start_dt, end_dt, min_minutes
        ))

    async def _facts_for_reason(reasoning: str) -> list[int]:
        reason_words = _words(reasoning)
        matched: list[int] = []
        for fact in await store.query_facts(active=True):
            fact_words = _words(str(fact.get("content") or ""))
            if fact_words and len(reason_words & fact_words) >= min(2, len(fact_words)):
                matched.append(int(fact["id"]))
        return matched

    async def schedule_task(
        task_id: int,
        start: str,
        end: str,
        reasoning: str,
        trigger: str,
    ) -> Any:
        start_dt, end_dt = _aware(start, "start"), _aware(end, "end")
        assert start_dt is not None and end_dt is not None
        decision = await scheduler.schedule_task(
            task_id,
            start_dt,
            end_dt,
            reasoning,
            trigger,  # type: ignore[arg-type]
            await _facts_for_reason(reasoning),
        )
        return jsonable(decision)

    async def explain_schedule(task_id: int) -> Any:
        return jsonable(await scheduler.explain_schedule(task_id))

    async def add_fact(content: str, category: str, confidence: float, source: str) -> Any:
        return jsonable(await store.add_fact({
            "content": content, "category": category, "confidence": confidence,
            "source": source,
        }))

    async def update_fact(fact_id: int, **changes: Any) -> Any:
        return jsonable(await store.update_fact(
            fact_id, {key: value for key, value in changes.items() if value is not None}
        ))

    async def query_facts(
        category: str | None, active: bool | None, min_confidence: float | None
    ) -> Any:
        return jsonable(await store.query_facts(category, active, min_confidence))

    async def add_goal(**goal: Any) -> Any:
        return jsonable(await store.add_goal(goal))

    async def log_goal_progress(
        goal_id: int, amount: float, source: str, logged_at: str
    ) -> Any:
        moment = _aware(logged_at, "logged_at")
        assert moment is not None
        return jsonable(await store.log_goal_progress(
            goal_id, amount, source, moment  # type: ignore[arg-type]
        ))

    async def query_goals(active: bool | None, category: str | None) -> Any:
        return jsonable(await store.query_goals(active, category))

    async def resolve_date(phrase: str) -> Any:
        local = timeutil.resolve_relative(phrase)
        return {
            "phrase": phrase,
            "local": local.isoformat(),
            "utc": timeutil.to_utc(local).isoformat().replace("+00:00", "Z"),
            "timezone": str(local.tzinfo),
        }

    handlers: dict[str, ToolHandler] = {
        "add_task": add_task,
        "add_event": add_event,
        "update_task": update_task,
        "update_event": update_event,
        "confirm_event_change": confirm_event_change,
        "complete_task": complete_task,
        "delete_task": delete_task,
        "delete_event": delete_event,
        "query_schedule": query_schedule_tool,
        "query_tasks": query_tasks,
        "add_reminder": add_reminder,
        "update_reminder": update_reminder,
        "cancel_reminder": cancel_reminder,
        "query_reminders": query_reminders,
        "find_free_blocks": find_free_blocks_tool,
        "schedule_task": schedule_task,
        "explain_schedule": explain_schedule,
        "add_fact": add_fact,
        "update_fact": update_fact,
        "query_facts": query_facts,
        "add_goal": add_goal,
        "log_goal_progress": log_goal_progress,
        "query_goals": query_goals,
        "resolve_date": resolve_date,
    }
    return handlers
