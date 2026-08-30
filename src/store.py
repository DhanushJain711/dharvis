"""Timezone-safe asynchronous persistence for Dharvis."""

from __future__ import annotations

import asyncio
import json
import re
import statistics
import unicodedata
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, AsyncIterator, Literal, Sequence

import aiosqlite

from . import timeutil
from .config import config
from .migrate import run_migrations

Record = dict[str, Any]
TaskStatus = Literal["pending", "scheduled", "completed", "dropped"]

_TASK_FIELDS = {
    "title", "description", "deadline", "estimated_minutes", "category", "energy",
    "priority", "status", "scheduled_start", "scheduled_end", "gcal_event_id",
    "goal_id", "completed_at", "actual_minutes", "series_key",
    "estimate_source", "actual_minutes_source",
}
_TASK_UPDATE_FIELDS = {
    "title", "description", "deadline", "estimated_minutes", "category", "energy",
    "priority", "status", "goal_id", "series_key", "estimate_source",
    "actual_minutes_source",
}
_TASK_CLEARABLE = {
    "description", "deadline", "estimated_minutes", "goal_id", "series_key",
    "estimate_source", "actual_minutes_source",
}
_EVENT_FIELDS = {
    "title", "description", "start_time", "end_time", "location", "category",
    "source", "gcal_event_id",
}
_EVENT_CLEARABLE = {"description", "location", "category"}
_FACT_FIELDS = {
    "content", "category", "confidence", "source", "evidence_count",
    "last_confirmed_at", "active",
}
_DAILY_LOG_FIELDS = {"brief_sent_at", "debrief_sent_at", "planned", "completed", "notes"}
_DATETIME_FIELDS = {
    "deadline", "scheduled_start", "scheduled_end", "created_at", "completed_at",
    "start_time", "end_time", "decided_at", "start", "end", "previous_start",
    "previous_end", "last_confirmed_at", "logged_at", "brief_sent_at", "debrief_sent_at",
    "period_start", "period_end", "cancelled_at", "expires_at", "claimed_at", "consumed_at",
}
_JSON_FIELDS = {"facts_used", "tool_calls", "planned", "completed", "payload", "conflicts"}
_BOOL_FIELDS = {"active", "surfaced_to_user", "scheduling_enabled"}
_PLACEHOLDER_REASONING = {
    "", "because", "because reason", "because reasons", "dummy", "example", "n a",
    "na", "no reason", "none", "placeholder", "reason", "reasons", "some reason",
    "some reasons", "test", "tbd", "todo", "unknown",
}
_PLACEHOLDER_REASONING_PHRASES = {
    "because reason", "because reasons", "n a", "no reason", "some reason",
    "some reasons",
}
_PLACEHOLDER_REASONING_WORDS = {
    "dummy", "placeholder", "tbd", "todo", "unknown",
}
_WORD_RE = re.compile(r"[a-z0-9]+")
_STOP_WORDS = {
    "a", "an", "and", "at", "do", "done", "for", "i", "in", "is", "it", "mark",
    "go", "going", "me", "my", "of", "on", "please", "that", "the", "thing", "this", "to", "user",
    "wants", "would",
}
_SERIES_STOP_WORDS = {
    "assignment", "complete", "finish", "homework", "next", "problem", "set",
    "this", "week", "worksheet", "work",
}
_ORDINAL_RE = re.compile(r"^\d+(?:st|nd|rd|th)?$")
_NUMBERED_TOKEN_RE = re.compile(r"^([a-z]+)[-_#]?\d+$")


def _utc_text(value: datetime, name: str = "datetime") -> str:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a timezone-aware datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware; naive datetimes are not allowed")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _utc_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Persisted timestamp is naive: {value!r}")
    return parsed.astimezone(UTC)


def _utc_epoch_us(value: str | None) -> int | None:
    """Return an exact integer UTC timestamp for SQLite comparisons."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("UTC timestamp must be text")
    delta = _utc_datetime(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return (
        delta.days * 86_400_000_000
        + delta.seconds * 1_000_000
        + delta.microseconds
    )


def _record(row: aiosqlite.Row | None) -> Record | None:
    if row is None:
        return None
    result: Record = dict(row)
    for key, value in tuple(result.items()):
        if value is None:
            continue
        if key in _DATETIME_FIELDS:
            result[key] = _utc_datetime(value)
        elif key in _JSON_FIELDS:
            result[key] = json.loads(value)
        elif key in _BOOL_FIELDS:
            result[key] = bool(value)
    return result


def _records(rows: Sequence[aiosqlite.Row]) -> list[Record]:
    return [_record(row) for row in rows]  # type: ignore[misc]


def _require_nonempty_text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _normalize_reasoning(reasoning: str) -> str:
    text = _require_nonempty_text(reasoning, "reasoning")
    normalized = " ".join(_WORD_RE.findall(text.casefold()))
    padded = f" {normalized} "
    words = set(normalized.split())
    if (
        normalized in _PLACEHOLDER_REASONING
        or normalized.startswith(("placeholder ", "todo ", "tbd ", "test ", "unknown "))
        or words & _PLACEHOLDER_REASONING_WORDS
        or any(f" {phrase} " in padded for phrase in _PLACEHOLDER_REASONING_PHRASES)
    ):
        raise ValueError("reasoning must be a real explanation, not placeholder text")
    if len(_WORD_RE.findall(normalized)) < 2 or len(normalized) < 8:
        raise ValueError("reasoning must contain a meaningful explanation")
    return text


def _normalized_words(text: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", text.casefold())
    words: set[str] = set()
    for word in _WORD_RE.findall(normalized):
        if word in _STOP_WORDS:
            continue
        if word in {"like", "liked", "likes", "liking"} or word.startswith("prefer"):
            word = "prefer"
        elif word in {"evening", "evenings", "late", "night", "nighttime", "nights"}:
            word = "late"
        elif word in {"early", "morning", "mornings"}:
            word = "early"
        elif len(word) > 4 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 3 and word.endswith("es"):
            word = word[:-2]
        elif len(word) > 3 and word.endswith("s"):
            word = word[:-1]
        words.add(word)
    return words


def _trigrams(text: str) -> set[str]:
    compact = " ".join(_WORD_RE.findall(unicodedata.normalize("NFKD", text.casefold())))
    padded = f"  {compact}  "
    return {padded[index:index + 3] for index in range(max(0, len(padded) - 2))}


def _text_similarity(left: str, right: str) -> float:
    left_words, right_words = _normalized_words(left), _normalized_words(right)
    if left_words and right_words:
        intersection = len(left_words & right_words)
        overlap = intersection / min(len(left_words), len(right_words))
        jaccard = intersection / len(left_words | right_words)
    else:
        overlap = jaccard = 0.0
    left_tri, right_tri = _trigrams(left), _trigrams(right)
    trigram = (
        2 * len(left_tri & right_tri) / (len(left_tri) + len(right_tri))
        if left_tri and right_tri else 0.0
    )
    return min(1.0, 0.55 * overlap + 0.25 * jaccard + 0.20 * trigram)


def normalize_series_key(title: str) -> str:
    """Return a stable lexical key for recurring tasks without semantic search.

    Sequence numbers, dates, and generic action words are discarded, while the
    subject-bearing tokens remain. For example, ``Math pset #4`` and
    ``finish math pset 5`` both normalize to ``math pset``.
    """
    clean = _require_nonempty_text(title, "title")
    normalized = unicodedata.normalize("NFKD", clean.casefold())
    tokens: list[str] = []
    for token in _WORD_RE.findall(normalized):
        if _ORDINAL_RE.fullmatch(token) or re.fullmatch(r"20\d{2}", token):
            continue
        numbered = _NUMBERED_TOKEN_RE.fullmatch(token)
        if numbered:
            token = numbered.group(1)
        if token in _SERIES_STOP_WORDS:
            continue
        tokens.append(token)
    # If every word was generic, retain normalized words rather than producing
    # a key shared by unrelated empty-looking task names.
    if not tokens:
        tokens = _WORD_RE.findall(normalized)
    return " ".join(tokens)


def _fact_similarity(left: str, right: str) -> float:
    """Similarity with a guard against merging contradictory time preferences."""
    left_words, right_words = _normalized_words(left), _normalized_words(right)
    timing_terms = {"early", "afternoon", "late"}
    left_timing, right_timing = left_words & timing_terms, right_words & timing_terms
    if left_timing and right_timing and left_timing.isdisjoint(right_timing):
        return 0.0
    return _text_similarity(left, right)


def _db_value(field: str, value: Any) -> Any:
    if value is None:
        return None
    if field in _DATETIME_FIELDS:
        return _utc_text(value, field)
    if field in _JSON_FIELDS:
        return json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    if field in _BOOL_FIELDS:
        return int(bool(value))
    return value


class Store:
    """Async repository facade over the canonical SQLite schema."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        supplied = db_path if db_path is not None else config.DATABASE_PATH
        self.db_path = Path(supplied)
        self._memory = str(supplied) == ":memory:"
        self._connect_target = (
            f"file:dharvis-{id(self)}?mode=memory&cache=shared"
            if self._memory else str(self.db_path)
        )
        self._uri = self._memory
        self._keeper: aiosqlite.Connection | None = None
        self._initialize_lock = asyncio.Lock()

    async def initialize(self) -> None:
        """Install or migrate the canonical schema."""
        async with self._initialize_lock:
            if self._memory:
                if self._keeper is not None:
                    return
                keeper = await aiosqlite.connect(self._connect_target, uri=True)
                try:
                    keeper.row_factory = aiosqlite.Row
                    await keeper.create_function(
                        "utc_epoch_us", 1, _utc_epoch_us, deterministic=True
                    )
                    await keeper.execute("PRAGMA foreign_keys = ON")
                    schema = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")
                    await keeper.executescript(schema)
                    await keeper.commit()
                except Exception:
                    await keeper.close()
                    raise
                self._keeper = keeper
                return
            await run_migrations(self.db_path)

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[aiosqlite.Connection]:
        """Yield a database connection with foreign-key enforcement enabled."""
        if self._memory and self._keeper is None:
            await self.initialize()
        db = await aiosqlite.connect(self._connect_target, uri=self._uri)
        db.row_factory = aiosqlite.Row
        try:
            await db.create_function(
                "utc_epoch_us", 1, _utc_epoch_us, deterministic=True
            )
            await db.execute("PRAGMA foreign_keys = ON")
            yield db
        finally:
            await db.close()

    async def add_tasks(self, tasks: list[Record]) -> list[Record]:
        """Persist a batch of task payloads and return their records."""
        if not isinstance(tasks, list) or not tasks:
            raise ValueError("tasks must be a non-empty list")
        allowed = _TASK_FIELDS - {
            "status", "scheduled_start", "scheduled_end", "gcal_event_id",
            "completed_at", "actual_minutes", "actual_minutes_source",
        }
        ids: list[int] = []
        async with self.connection() as db:
            try:
                for task in tasks:
                    unknown = set(task) - allowed
                    if unknown:
                        raise ValueError(f"Unsupported task fields: {sorted(unknown)}")
                    values = {
                        key: value for key, value in task.items()
                        if key in allowed and value is not None
                    }
                    values["title"] = _require_nonempty_text(task.get("title"), "title")
                    values["series_key"] = _require_nonempty_text(
                        task.get("series_key") or normalize_series_key(values["title"]),
                        "series_key",
                    )
                    if "estimated_minutes" in values and "estimate_source" not in values:
                        values["estimate_source"] = "user"
                    if "estimate_source" in values and "estimated_minutes" not in values:
                        raise ValueError(
                            "estimate_source requires a non-null estimated_minutes value"
                        )
                    if "deadline" in values:
                        values["deadline"] = _db_value("deadline", values["deadline"])
                    fields = list(values)
                    cursor = await db.execute(
                        f"INSERT INTO tasks ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                        [values[field] for field in fields],
                    )
                    ids.append(int(cursor.lastrowid))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            rows = await self._fetch_ids(db, "tasks", ids)
        return rows

    async def add_events(self, events: list[Record]) -> list[Record]:
        """Persist a batch of local event payloads and return their records."""
        if not isinstance(events, list) or not events:
            raise ValueError("events must be a non-empty list")
        ids: list[int] = []
        async with self.connection() as db:
            try:
                for event in events:
                    payload = dict(event)
                    for alias, target in (("start", "start_time"), ("end", "end_time")):
                        if alias in payload:
                            if target in payload:
                                raise ValueError(f"Use either {alias} or {target}, not both")
                            payload[target] = payload.pop(alias)
                    unknown = set(payload) - _EVENT_FIELDS
                    if unknown:
                        raise ValueError(f"Unsupported event fields: {sorted(unknown)}")
                    payload["title"] = _require_nonempty_text(payload.get("title"), "title")
                    if "start_time" not in payload or "end_time" not in payload:
                        raise ValueError("events require start and end datetimes")
                    start = _utc_text(payload["start_time"], "start")
                    end = _utc_text(payload["end_time"], "end")
                    if end <= start:
                        raise ValueError("event end must be later than start")
                    payload["start_time"], payload["end_time"] = start, end
                    values = {key: value for key, value in payload.items() if value is not None}
                    fields = list(values)
                    cursor = await db.execute(
                        f"INSERT INTO events ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                        [values[field] for field in fields],
                    )
                    ids.append(int(cursor.lastrowid))
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            rows = await self._fetch_ids(db, "events", ids)
        return rows

    async def _fetch_ids(
        self, db: aiosqlite.Connection, table: str, ids: list[int]
    ) -> list[Record]:
        if not ids:
            return []
        cursor = await db.execute(
            f"SELECT * FROM {table} WHERE id IN ({', '.join('?' for _ in ids)})", ids
        )
        by_id = {row["id"]: _record(row) for row in await cursor.fetchall()}
        return [by_id[item] for item in ids]  # type: ignore[list-item]

    async def _get(
        self, table: str, item_id: int, db: aiosqlite.Connection | None = None
    ) -> Record | None:
        if db is not None:
            cursor = await db.execute(f"SELECT * FROM {table} WHERE id = ?", (item_id,))
            return _record(await cursor.fetchone())
        async with self.connection() as connection:
            return await self._get(table, item_id, connection)

    async def get_task(self, task_id: int) -> Record | None:
        return await self._get("tasks", task_id)

    async def get_event(self, event_id: int) -> Record | None:
        return await self._get("events", event_id)

    async def _update(
        self,
        table: str,
        item_id: int,
        changes: Record,
        allowed: set[str],
        clearable: set[str],
        aliases: dict[str, str] | None = None,
    ) -> Record:
        payload = dict(changes)
        for alias, target in (aliases or {}).items():
            if alias in payload:
                if target in payload:
                    raise ValueError(f"Use either {alias} or {target}, not both")
                payload[target] = payload.pop(alias)
        clear_fields = payload.pop("clear_fields", [])
        if not isinstance(clear_fields, list) or any(not isinstance(field, str) for field in clear_fields):
            raise ValueError("clear_fields must be a list of field names")
        invalid_clear = set(clear_fields) - clearable
        if invalid_clear:
            raise ValueError(f"Fields cannot be cleared: {sorted(invalid_clear)}")
        contradictory = {
            field for field in clear_fields
            if field in payload and payload[field] is not None
        }
        if contradictory:
            raise ValueError(
                "Fields cannot be both cleared and assigned: "
                f"{sorted(contradictory)}"
            )
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(f"Unsupported {table[:-1]} fields: {sorted(unknown)}")
        assignments: Record = {field: None for field in clear_fields}
        for field, value in payload.items():
            if value is not None:
                assignments[field] = _db_value(field, value)
        if "title" in assignments:
            assignments["title"] = _require_nonempty_text(assignments["title"], "title")
        async with self.connection() as db:
            if await self._get(table, item_id, db) is None:
                raise KeyError(f"{table[:-1].capitalize()} {item_id} does not exist")
            if assignments:
                sql = ", ".join(f"{field} = ?" for field in assignments)
                try:
                    await db.execute(
                        f"UPDATE {table} SET {sql} WHERE id = ?",
                        [*assignments.values(), item_id],
                    )
                    await db.commit()
                except Exception:
                    await db.rollback()
                    raise
            updated = await self._get(table, item_id, db)
        assert updated is not None
        return updated

    async def update_task(self, task_id: int, changes: Record) -> Record:
        return await self._update(
            "tasks", task_id, changes, _TASK_UPDATE_FIELDS, _TASK_CLEARABLE
        )

    async def update_event(self, event_id: int, changes: Record) -> Record:
        return await self._update(
            "events", event_id, changes, _EVENT_FIELDS, _EVENT_CLEARABLE,
            {"start": "start_time", "end": "end_time"},
        )

    async def complete_task(
        self,
        task_id: int,
        actual_minutes: int | None = None,
        actual_minutes_source: Literal["user", "debrief", "calendar", "inferred"] | None = None,
    ) -> Record:
        if actual_minutes is not None and (
            not isinstance(actual_minutes, int) or isinstance(actual_minutes, bool) or actual_minutes < 0
        ):
            raise ValueError("actual_minutes must be a non-negative integer or None")
        if actual_minutes is None and actual_minutes_source is not None:
            raise ValueError("actual_minutes_source requires actual_minutes")
        source = (actual_minutes_source or "user") if actual_minutes is not None else None
        async with self.connection() as db:
            if await self._get("tasks", task_id, db) is None:
                raise KeyError(f"Task {task_id} does not exist")
            await db.execute(
                """UPDATE tasks SET status = 'completed', completed_at = ?,
                   actual_minutes = ?, actual_minutes_source = ?,
                   scheduled_start = NULL, scheduled_end = NULL,
                   gcal_event_id = NULL WHERE id = ?""",
                (
                    _utc_text(timeutil.now_utc(), "completed_at"), actual_minutes,
                    source, task_id,
                ),
            )
            await db.commit()
            result = await self._get("tasks", task_id, db)
        assert result is not None
        return result

    async def drop_task(self, task_id: int) -> Record:
        async with self.connection() as db:
            if await self._get("tasks", task_id, db) is None:
                raise KeyError(f"Task {task_id} does not exist")
            await db.execute(
                "UPDATE tasks SET status = 'dropped', completed_at = NULL WHERE id = ?",
                (task_id,),
            )
            await db.commit()
            result = await self._get("tasks", task_id, db)
        assert result is not None
        return result

    async def delete_task(self, task_id: int) -> Record:
        return await self.drop_task(task_id)

    async def delete_event(self, event_id: int) -> bool:
        async with self.connection() as db:
            cursor = await db.execute("DELETE FROM events WHERE id = ?", (event_id,))
            await db.commit()
            return cursor.rowcount > 0

    async def create_event_change_proposal(
        self,
        operation: Literal["create", "update"],
        payload: dict[str, Any],
        conflicts: list[dict[str, Any]],
        expires_at: datetime,
    ) -> Record:
        """Persist an expiring, one-time confirmation for a conflicting event change."""
        if operation not in {"create", "update"}:
            raise ValueError("operation must be 'create' or 'update'")
        if not isinstance(payload, dict):
            raise TypeError("payload must be a dictionary")
        if not isinstance(conflicts, list) or any(
            not isinstance(conflict, dict) for conflict in conflicts
        ):
            raise TypeError("conflicts must be a list of dictionaries")
        created_at = timeutil.now_utc()
        created_text = _utc_text(created_at, "created_at")
        expires_text = _utc_text(expires_at, "expires_at")
        if expires_at.astimezone(UTC) <= created_at:
            raise ValueError("expires_at must be in the future")
        proposal_id = uuid.uuid4().hex
        async with self.connection() as db:
            await db.execute(
                """INSERT INTO event_change_proposals
                   (id, operation, payload, conflicts, created_at, expires_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    proposal_id, operation,
                    json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
                    json.dumps(conflicts, separators=(",", ":"), ensure_ascii=False),
                    created_text, expires_text,
                ),
            )
            await db.commit()
            cursor = await db.execute(
                "SELECT * FROM event_change_proposals WHERE id = ?", (proposal_id,)
            )
            result = _record(await cursor.fetchone())
        assert result is not None
        return result

    async def get_event_change_proposal(self, proposal_id: str) -> Record | None:
        """Return a confirmation proposal, including consumed/expiry metadata."""
        clean_id = _require_nonempty_text(proposal_id, "proposal_id")
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
            )
            return _record(await cursor.fetchone())

    async def claim_event_change_proposal(
        self, proposal_id: str, claimed_at: datetime | None = None
    ) -> Record:
        """Atomically reserve one unexpired proposal for an external write.

        The returned ``claim_token`` is an opaque capability that must be
        presented to finalize or release the reservation.  It is intentionally
        retained after finalization as an audit link between the external
        mutation and its one-time confirmation.
        """
        clean_id = _require_nonempty_text(proposal_id, "proposal_id")
        claimed = claimed_at or timeutil.now_utc()
        claimed_text = _utc_text(claimed, "claimed_at")
        claim_token = uuid.uuid4().hex
        # Shared in-memory SQLite can raise SQLITE_LOCKED immediately instead
        # of honouring a connection busy timeout. Retry only this short,
        # idempotent acquisition transaction; after the winner commits, the
        # loser deterministically observes its claim and receives ValueError.
        for attempt in range(4):
            try:
                async with self.connection() as db:
                    try:
                        await db.execute("BEGIN IMMEDIATE")
                        cursor = await db.execute(
                            "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                        )
                        proposal = _record(await cursor.fetchone())
                        if proposal is None:
                            raise KeyError(
                                f"Event change proposal {clean_id!r} does not exist"
                            )
                        if proposal["consumed_at"] is not None:
                            raise ValueError("event change proposal has already been consumed")
                        if proposal["claimed_at"] is not None:
                            raise ValueError("event change proposal has already been claimed")
                        if proposal["expires_at"] <= claimed.astimezone(UTC):
                            raise ValueError("event change proposal has expired")
                        update = await db.execute(
                            """UPDATE event_change_proposals
                               SET claimed_at = ?, claim_token = ?
                               WHERE id = ? AND consumed_at IS NULL AND claimed_at IS NULL""",
                            (claimed_text, claim_token, clean_id),
                        )
                        if update.rowcount != 1:
                            raise ValueError("event change proposal is no longer available")
                        await db.commit()
                        cursor = await db.execute(
                            "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                        )
                        result = _record(await cursor.fetchone())
                    except Exception:
                        await db.rollback()
                        raise
            except aiosqlite.OperationalError as exc:
                if attempt == 3 or not any(
                    token in str(exc).casefold() for token in ("locked", "busy")
                ):
                    raise
                await asyncio.sleep(0.005 * (2 ** attempt))
                continue
            assert result is not None
            return result
        raise RuntimeError("unreachable claim retry exhaustion")

    async def finalize_event_change_proposal(
        self,
        proposal_id: str,
        claim_token: str,
        consumed_at: datetime | None = None,
    ) -> Record:
        """Consume a proposal held by the matching claim after its write commits."""
        clean_id = _require_nonempty_text(proposal_id, "proposal_id")
        clean_token = _require_nonempty_text(claim_token, "claim_token")
        consumed = consumed_at or timeutil.now_utc()
        consumed_text = _utc_text(consumed, "consumed_at")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                proposal = _record(await cursor.fetchone())
                if proposal is None:
                    raise KeyError(f"Event change proposal {clean_id!r} does not exist")
                if proposal["consumed_at"] is not None:
                    raise ValueError("event change proposal has already been consumed")
                if proposal["claim_token"] != clean_token:
                    raise ValueError("event change proposal claim token is invalid")
                update = await db.execute(
                    """UPDATE event_change_proposals SET consumed_at = ?
                       WHERE id = ? AND claim_token = ? AND consumed_at IS NULL""",
                    (consumed_text, clean_id, clean_token),
                )
                if update.rowcount != 1:
                    raise ValueError("event change proposal claim is no longer available")
                await db.commit()
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                result = _record(await cursor.fetchone())
            except Exception:
                await db.rollback()
                raise
        assert result is not None
        return result

    async def release_event_change_proposal(
        self, proposal_id: str, claim_token: str
    ) -> Record:
        """Release a claim only after the caller has compensated its write.

        Possession of the opaque token is required; callers must invoke this
        only when no lasting remote or local side effect remains.
        """
        clean_id = _require_nonempty_text(proposal_id, "proposal_id")
        clean_token = _require_nonempty_text(claim_token, "claim_token")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                proposal = _record(await cursor.fetchone())
                if proposal is None:
                    raise KeyError(f"Event change proposal {clean_id!r} does not exist")
                if proposal["consumed_at"] is not None:
                    raise ValueError("event change proposal has already been consumed")
                if proposal["claim_token"] != clean_token:
                    raise ValueError("event change proposal claim token is invalid")
                update = await db.execute(
                    """UPDATE event_change_proposals
                       SET claimed_at = NULL, claim_token = NULL
                       WHERE id = ? AND claim_token = ? AND consumed_at IS NULL""",
                    (clean_id, clean_token),
                )
                if update.rowcount != 1:
                    raise ValueError("event change proposal claim is no longer available")
                await db.commit()
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                result = _record(await cursor.fetchone())
            except Exception:
                await db.rollback()
                raise
        assert result is not None
        return result

    async def consume_event_change_proposal(
        self, proposal_id: str, consumed_at: datetime | None = None
    ) -> Record:
        """Atomically consume an unexpired proposal exactly once."""
        clean_id = _require_nonempty_text(proposal_id, "proposal_id")
        consumed = consumed_at or timeutil.now_utc()
        consumed_text = _utc_text(consumed, "consumed_at")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                proposal = _record(await cursor.fetchone())
                if proposal is None:
                    raise KeyError(f"Event change proposal {clean_id!r} does not exist")
                if proposal["consumed_at"] is not None:
                    raise ValueError("event change proposal has already been consumed")
                if proposal["claimed_at"] is not None:
                    raise ValueError(
                        "event change proposal has been claimed and must be finalized"
                    )
                if proposal["expires_at"] <= consumed.astimezone(UTC):
                    raise ValueError("event change proposal has expired")
                update = await db.execute(
                    """UPDATE event_change_proposals SET consumed_at = ?
                       WHERE id = ? AND consumed_at IS NULL""",
                    (consumed_text, clean_id),
                )
                if update.rowcount != 1:
                    raise ValueError("event change proposal has already been consumed")
                await db.commit()
                cursor = await db.execute(
                    "SELECT * FROM event_change_proposals WHERE id = ?", (clean_id,)
                )
                result = _record(await cursor.fetchone())
            except Exception:
                await db.rollback()
                raise
        assert result is not None
        return result

    async def query_tasks(
        self,
        status: TaskStatus | None = None,
        category: str | None = None,
        due_before: datetime | None = None,
        due_after: datetime | None = None,
    ) -> list[Record]:
        clauses: list[str] = []
        params: list[Any] = []
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if due_before is not None:
            clauses.append("utc_epoch_us(deadline) < utc_epoch_us(?)")
            params.append(_utc_text(due_before, "due_before"))
        if due_after is not None:
            clauses.append("utc_epoch_us(deadline) >= utc_epoch_us(?)")
            params.append(_utc_text(due_after, "due_after"))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.connection() as db:
            cursor = await db.execute(
                f"""SELECT * FROM tasks{where}
                    ORDER BY deadline IS NULL, utc_epoch_us(deadline),
                             utc_epoch_us(created_at), id""",
                params,
            )
            return _records(await cursor.fetchall())

    async def query_events(self, start: datetime, end: datetime) -> list[Record]:
        start_text, end_text = _utc_text(start, "start"), _utc_text(end, "end")
        if end_text <= start_text:
            raise ValueError("end must be later than start")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM events
                   WHERE utc_epoch_us(start_time) < utc_epoch_us(?)
                     AND utc_epoch_us(end_time) > utc_epoch_us(?)
                   ORDER BY utc_epoch_us(start_time), utc_epoch_us(end_time), id""",
                (end_text, start_text),
            )
            return _records(await cursor.fetchall())

    async def _validate_fact_ids(
        self, db: aiosqlite.Connection, facts_used: list[int]
    ) -> None:
        if not isinstance(facts_used, list) or any(type(item) is not int for item in facts_used):
            raise ValueError("facts_used must be a list of integer fact ids")
        if not facts_used:
            return
        unique = set(facts_used)
        cursor = await db.execute(
            f"SELECT id FROM facts WHERE id IN ({', '.join('?' for _ in unique)})",
            list(unique),
        )
        existing = {row["id"] for row in await cursor.fetchall()}
        missing = unique - existing
        if missing:
            raise ValueError(f"facts_used contains nonexistent fact ids: {sorted(missing)}")

    async def _insert_decision(
        self,
        db: aiosqlite.Connection,
        task_id: int,
        action: str,
        start: datetime,
        end: datetime,
        previous_start: datetime | None,
        previous_end: datetime | None,
        trigger: str,
        reasoning: str,
        facts_used: list[int],
    ) -> int:
        start_text, end_text = _utc_text(start, "start"), _utc_text(end, "end")
        if end_text <= start_text:
            raise ValueError("end must be later than start")
        if (previous_start is None) != (previous_end is None):
            raise ValueError("previous_start and previous_end must both be set or both be None")
        previous_start_text = (
            _utc_text(previous_start, "previous_start") if previous_start is not None else None
        )
        previous_end_text = (
            _utc_text(previous_end, "previous_end") if previous_end is not None else None
        )
        if previous_start_text and previous_end_text and previous_end_text <= previous_start_text:
            raise ValueError("previous_end must be later than previous_start")
        explanation = _normalize_reasoning(reasoning)
        await self._validate_fact_ids(db, facts_used)
        cursor = await db.execute(
            """INSERT INTO schedule_decisions
               (task_id, action, "start", "end", previous_start, previous_end,
                trigger, reasoning, facts_used)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                task_id, action, start_text, end_text, previous_start_text,
                previous_end_text, trigger, explanation,
                json.dumps(facts_used, separators=(",", ":")),
            ),
        )
        return int(cursor.lastrowid)

    async def apply_schedule_decision(
        self,
        task_id: int,
        action: Literal["scheduled", "moved", "unscheduled", "shortened", "extended"],
        start: datetime,
        end: datetime,
        previous_start: datetime | None,
        previous_end: datetime | None,
        trigger: Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"],
        reasoning: str,
        facts_used: list[int],
        gcal_event_id: str | None,
    ) -> Record:
        """Atomically mutate placement and record its contemporaneous rationale."""
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                if await self._get("tasks", task_id, db) is None:
                    raise KeyError(f"Task {task_id} does not exist")
                decision_id = await self._insert_decision(
                    db, task_id, action, start, end, previous_start, previous_end,
                    trigger, reasoning, facts_used,
                )
                if action == "unscheduled":
                    await db.execute(
                        """UPDATE tasks SET status = 'pending', scheduled_start = NULL,
                           scheduled_end = NULL, gcal_event_id = NULL WHERE id = ?""",
                        (task_id,),
                    )
                else:
                    await db.execute(
                        """UPDATE tasks SET status = 'scheduled', scheduled_start = ?,
                           scheduled_end = ?, gcal_event_id = ?, completed_at = NULL WHERE id = ?""",
                        (_utc_text(start, "start"), _utc_text(end, "end"), gcal_event_id, task_id),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            result = await self._get("schedule_decisions", decision_id, db)
        assert result is not None
        return result

    async def record_decision(
        self,
        task_id: int,
        action: Literal["scheduled", "moved", "unscheduled", "shortened", "extended"],
        start: datetime,
        end: datetime,
        previous: tuple[datetime, datetime] | list[datetime] | None,
        trigger: Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"],
        reasoning: str,
        facts_used: list[int],
    ) -> Record:
        """Record a decision without changing task placement."""
        if previous is None:
            previous_start = previous_end = None
        elif isinstance(previous, (tuple, list)) and len(previous) == 2:
            previous_start, previous_end = previous
        else:
            raise ValueError("previous must be None or a two-item (start, end) pair")
        async with self.connection() as db:
            try:
                decision_id = await self._insert_decision(
                    db, task_id, action, start, end, previous_start, previous_end,
                    trigger, reasoning, facts_used,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            result = await self._get("schedule_decisions", decision_id, db)
        assert result is not None
        return result

    async def get_schedule_decisions(self, task_id: int) -> list[Record]:
        """Return a task's decision history in chronological order."""
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM schedule_decisions WHERE task_id = ?
                   ORDER BY utc_epoch_us(decided_at), id""",
                (task_id,),
            )
            return _records(await cursor.fetchall())

    async def get_decisions_for_task(self, task_id: int) -> list[Record]:
        """Return a task's decisions newest-first."""
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM schedule_decisions WHERE task_id = ?
                   ORDER BY utc_epoch_us(decided_at) DESC, id DESC""",
                (task_id,),
            )
            return _records(await cursor.fetchall())

    async def get_unsurfaced_decisions(self, since: datetime) -> list[Record]:
        """Return unsurfaced decisions at or after ``since`` chronologically."""
        since_text = _utc_text(since, "since")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM schedule_decisions
                   WHERE surfaced_to_user = 0
                     AND utc_epoch_us(decided_at) >= utc_epoch_us(?)
                   ORDER BY utc_epoch_us(decided_at), id""",
                (since_text,),
            )
            return _records(await cursor.fetchall())

    async def get_schedule_decisions_between(
        self, start: datetime, end: datetime
    ) -> list[Record]:
        """Return decisions in the half-open UTC interval ``[start, end)``."""
        start_text, end_text = _utc_text(start, "start"), _utc_text(end, "end")
        if end_text <= start_text:
            raise ValueError("end must be later than start")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM schedule_decisions
                   WHERE utc_epoch_us(decided_at) >= utc_epoch_us(?)
                     AND utc_epoch_us(decided_at) < utc_epoch_us(?)
                   ORDER BY utc_epoch_us(decided_at), id""",
                (start_text, end_text),
            )
            return _records(await cursor.fetchall())

    async def mark_decision_surfaced(self, decision_id: int) -> None:
        async with self.connection() as db:
            cursor = await db.execute(
                "UPDATE schedule_decisions SET surfaced_to_user = 1 WHERE id = ?",
                (decision_id,),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Schedule decision {decision_id} does not exist")
            await db.commit()

    async def find_task_by_description(self, text: str) -> list[Record]:
        """Return up to three fuzzy task candidates with numeric ranking scores."""
        query = _require_nonempty_text(text, "text")
        tasks = await self.query_tasks()
        now = timeutil.now_utc()
        ranked: list[Record] = []
        for task in tasks:
            haystack = " ".join(
                part for part in (task["title"], task.get("description")) if part
            )
            similarity = _text_similarity(query, haystack)
            age_days = max(0.0, (now - task["created_at"]).total_seconds() / 86400)
            recency = 1 / (1 + age_days / 30)
            status_bias = {
                "pending": 0.12, "scheduled": 0.06, "completed": 0.0, "dropped": -0.05,
            }[task["status"]]
            score = max(0.0, min(1.0, similarity * 0.82 + recency * 0.06 + status_bias))
            candidate = dict(task)
            candidate["score"] = round(score, 6)
            ranked.append(candidate)
        ranked.sort(
            key=lambda item: (item["score"], item["created_at"], item["id"]),
            reverse=True,
        )
        return ranked[:3]

    async def infer_task_duration(
        self,
        title: str,
        category: str,
        energy: str,
        series_key: str | None = None,
        limit: int = 5,
    ) -> Record:
        """Infer a recurring task's duration from recent matching completions.

        Matching is deterministic: the normalized series key, category, and
        energy must all agree. The estimate is a median after removing extreme
        median-absolute-deviation outliers when enough samples exist. No vector
        index or model call is involved.
        """
        clean_title = _require_nonempty_text(title, "title")
        clean_category = _require_nonempty_text(category, "category")
        clean_energy = _require_nonempty_text(energy, "energy")
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
            raise ValueError("limit must be a positive integer")
        key = _require_nonempty_text(
            series_key or normalize_series_key(clean_title), "series_key"
        )
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM tasks
                   WHERE status = 'completed'
                     AND actual_minutes IS NOT NULL AND actual_minutes > 0
                     AND category = ? AND energy = ?
                   ORDER BY utc_epoch_us(completed_at) DESC, id DESC""",
                (clean_category, clean_energy),
            )
            rows = _records(await cursor.fetchall())
        matching = [
            task for task in rows
            if (task.get("series_key") or normalize_series_key(task["title"])) == key
        ]
        # The SQL ordering establishes recency, but the limit must be applied
        # after deterministic series matching.  Otherwise a busy category can
        # hide valid older completions behind unrelated recent tasks.
        matching = matching[:limit]
        if not matching:
            return {
                "series_key": key,
                "estimated_minutes": None,
                "estimate_source": "default",
                "evidence_task_ids": [],
            }

        retained = matching
        durations = [int(task["actual_minutes"]) for task in matching]
        if len(durations) >= 4:
            center = float(statistics.median(durations))
            deviation = float(
                statistics.median(abs(duration - center) for duration in durations)
            )
            if deviation > 0:
                retained = [
                    task for task in matching
                    if abs(int(task["actual_minutes"]) - center) <= 3 * deviation
                ]
                durations = [int(task["actual_minutes"]) for task in retained]
        estimate = max(5, int(5 * round(float(statistics.median(durations)) / 5)))
        return {
            "series_key": key,
            "estimated_minutes": estimate,
            "estimate_source": "history",
            "evidence_task_ids": [int(task["id"]) for task in retained],
        }

    async def add_fact(self, fact: Record) -> Record:
        unknown = set(fact) - _FACT_FIELDS
        if unknown:
            raise ValueError(f"Unsupported fact fields: {sorted(unknown)}")
        values: Record = {
            "content": _require_nonempty_text(fact.get("content"), "content"),
            "category": _require_nonempty_text(fact.get("category"), "category"),
            "confidence": fact.get("confidence", 1.0),
            "source": fact.get("source", "explicit"),
        }
        for key in ("evidence_count", "last_confirmed_at", "active"):
            if key in fact:
                values[key] = _db_value(key, fact[key])
        fields = list(values)
        async with self.connection() as db:
            cursor = await db.execute(
                f"INSERT INTO facts ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                [values[field] for field in fields],
            )
            await db.commit()
            result = await self._get("facts", int(cursor.lastrowid), db)
        assert result is not None
        return result

    async def upsert_fact(self, content: str, category: str) -> Record:
        """Insert a fact or reconfirm a near-duplicate active fact."""
        clean_content = _require_nonempty_text(content, "content")
        clean_category = _require_nonempty_text(category, "category")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    "SELECT * FROM facts WHERE active = 1 ORDER BY id",
                )
                best_row = None
                best_score = 0.0
                for row in await cursor.fetchall():
                    score = _fact_similarity(clean_content, row["content"])
                    if score > best_score:
                        best_row, best_score = row, score
                if best_row is not None and best_score >= 0.68:
                    await db.execute(
                        """UPDATE facts SET evidence_count = evidence_count + 1,
                           last_confirmed_at = ? WHERE id = ?""",
                        (_utc_text(timeutil.now_utc(), "last_confirmed_at"), best_row["id"]),
                    )
                    fact_id = int(best_row["id"])
                else:
                    inserted = await db.execute(
                        """INSERT INTO facts (content, category, confidence, source)
                           VALUES (?, ?, 1.0, 'explicit')""",
                        (clean_content, clean_category),
                    )
                    fact_id = int(inserted.lastrowid)
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            result = await self._get("facts", fact_id, db)
        assert result is not None
        return result

    async def update_fact(self, fact_id: int, changes: Record) -> Record:
        unknown = set(changes) - _FACT_FIELDS
        if unknown:
            raise ValueError(f"Unsupported fact fields: {sorted(unknown)}")
        assignments = {
            key: _db_value(key, value) for key, value in changes.items() if value is not None
        }
        if "content" in assignments:
            assignments["content"] = _require_nonempty_text(assignments["content"], "content")
        if "category" in assignments:
            assignments["category"] = _require_nonempty_text(assignments["category"], "category")
        async with self.connection() as db:
            if await self._get("facts", fact_id, db) is None:
                raise KeyError(f"Fact {fact_id} does not exist")
            if assignments:
                await db.execute(
                    f"UPDATE facts SET {', '.join(f'{field} = ?' for field in assignments)} WHERE id = ?",
                    [*assignments.values(), fact_id],
                )
                await db.commit()
            result = await self._get("facts", fact_id, db)
        assert result is not None
        return result

    async def query_facts(
        self,
        category: str | None = None,
        active: bool | None = True,
        min_confidence: float | None = None,
    ) -> list[Record]:
        clauses: list[str] = []
        params: list[Any] = []
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if active is not None:
            clauses.append("active = ?")
            params.append(int(active))
        if min_confidence is not None:
            clauses.append("confidence >= ?")
            params.append(min_confidence)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.connection() as db:
            cursor = await db.execute(
                f"""SELECT * FROM facts{where}
                    ORDER BY category, confidence DESC,
                             utc_epoch_us(last_confirmed_at) DESC, id""",
                params,
            )
            return _records(await cursor.fetchall())

    async def get_active_facts(self) -> str:
        """Format active facts as a compact, prompt-ready category block."""
        facts = await self.query_facts(active=True)
        if not facts:
            return "No active facts."
        lines: list[str] = []
        current_category: str | None = None
        for fact in facts:
            if fact["category"] != current_category:
                if lines:
                    lines.append("")
                current_category = fact["category"]
                lines.append(f"{current_category}:")
            lines.append(
                f"- {fact['content']} (confidence {fact['confidence']:.2f}; "
                f"evidence {fact['evidence_count']})"
            )
        return "\n".join(lines)

    async def add_goal(self, goal: Record) -> Record:
        allowed = {
            "title", "target_amount", "target_unit", "period", "category", "active",
            "session_minutes", "scheduling_enabled",
        }
        unknown = set(goal) - allowed
        if unknown:
            raise ValueError(f"Unsupported goal fields: {sorted(unknown)}")
        required = {"title", "target_amount", "target_unit", "period", "category"}
        missing = required - set(goal)
        if missing:
            raise ValueError(f"Missing goal fields: {sorted(missing)}")
        values = dict(goal)
        values["title"] = _require_nonempty_text(values["title"], "title")
        values["category"] = _require_nonempty_text(values["category"], "category")
        for field in ("active", "scheduling_enabled"):
            if field in values:
                values[field] = int(bool(values[field]))
        if "session_minutes" in values and (
            not isinstance(values["session_minutes"], int)
            or isinstance(values["session_minutes"], bool)
            or values["session_minutes"] <= 0
        ):
            raise ValueError("session_minutes must be a positive integer")
        fields = list(values)
        async with self.connection() as db:
            cursor = await db.execute(
                f"INSERT INTO goals ({', '.join(fields)}) VALUES ({', '.join('?' for _ in fields)})",
                [values[field] for field in fields],
            )
            await db.commit()
            result = await self._get("goals", int(cursor.lastrowid), db)
        assert result is not None
        return result

    async def log_goal_progress(
        self,
        goal_id: int,
        amount: float,
        source: Literal["task", "manual", "inferred"],
        logged_at: datetime,
        task_id: int | None = None,
    ) -> Record:
        logged_text = _utc_text(logged_at, "logged_at")
        if (
            not isinstance(amount, (int, float))
            or isinstance(amount, bool)
            or amount <= 0
        ):
            raise ValueError("amount must be positive")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                if await self._get("goals", goal_id, db) is None:
                    raise KeyError(f"Goal {goal_id} does not exist")
                if task_id is not None:
                    task = await self._get("tasks", task_id, db)
                    if task is None:
                        raise KeyError(f"Task {task_id} does not exist")
                    if task.get("goal_id") != goal_id:
                        raise ValueError("task_id must refer to a task linked to this goal")
                    existing = await db.execute(
                        "SELECT * FROM goal_progress WHERE goal_id = ? AND task_id = ?",
                        (goal_id, task_id),
                    )
                    prior = _record(await existing.fetchone())
                    if prior is not None:
                        await db.commit()
                        return prior
                cursor = await db.execute(
                    """INSERT INTO goal_progress
                       (goal_id, amount, source, logged_at, task_id)
                       VALUES (?, ?, ?, ?, ?)""",
                    (goal_id, amount, source, logged_text, task_id),
                )
                await db.commit()
                result = await self._get("goal_progress", int(cursor.lastrowid), db)
            except Exception:
                await db.rollback()
                raise
        assert result is not None
        return result

    async def get_goal_progress(self, goal_id: int, period_start: datetime) -> Record:
        """Summarize progress, remainder, and local calendar days left."""
        _utc_text(period_start, "period_start")
        async with self.connection() as db:
            goal = await self._get("goals", goal_id, db)
            if goal is None:
                raise KeyError(f"Goal {goal_id} does not exist")
            local_start_date = timeutil.to_local(period_start).date()
            if goal["period"] == "week":
                local_end_date = local_start_date + timedelta(days=7)
            else:
                local_end_date = (
                    date(local_start_date.year + 1, 1, 1)
                    if local_start_date.month == 12
                    else date(local_start_date.year, local_start_date.month + 1, 1)
                )
            start_utc = timeutil.day_bounds(local_start_date)[0]
            end_utc = timeutil.day_bounds(local_end_date)[0]
            cursor = await db.execute(
                """SELECT COALESCE(SUM(amount), 0.0) AS amount_done
                   FROM goal_progress
                   WHERE goal_id = ?
                     AND utc_epoch_us(logged_at) >= utc_epoch_us(?)
                     AND utc_epoch_us(logged_at) < utc_epoch_us(?)""",
                (goal_id, _utc_text(start_utc), _utc_text(end_utc)),
            )
            row = await cursor.fetchone()
        amount_done = float(row["amount_done"])
        target = float(goal["target_amount"])
        return {
            "goal_id": goal_id,
            "period_start": start_utc,
            "period_end": end_utc,
            "amount_done": amount_done,
            "amount_remaining": max(0.0, target - amount_done),
            "days_left": max(0, (local_end_date - timeutil.now_local().date()).days),
        }

    async def _goal_schedule_item(
        self, db: aiosqlite.Connection, row: aiosqlite.Row | None
    ) -> Record | None:
        item = _record(row)
        if item is None:
            return None
        item["task"] = await self._get("tasks", int(item["task_id"]), db)
        return item

    async def ensure_goal_schedule_item(
        self,
        goal_id: int,
        task: Record,
        period_start: datetime,
        period_end: datetime,
        ordinal: int,
        planned_amount: float,
    ) -> Record:
        """Idempotently create one generated goal task and its period linkage."""
        start_text = _utc_text(period_start, "period_start")
        end_text = _utc_text(period_end, "period_end")
        if end_text <= start_text:
            raise ValueError("period_end must be later than period_start")
        if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal <= 0:
            raise ValueError("ordinal must be a positive integer")
        if (
            not isinstance(planned_amount, (int, float))
            or isinstance(planned_amount, bool)
            or planned_amount <= 0
        ):
            raise ValueError("planned_amount must be positive")
        allowed = _TASK_FIELDS - {
            "status", "scheduled_start", "scheduled_end", "gcal_event_id", "goal_id",
            "completed_at", "actual_minutes", "actual_minutes_source",
        }
        unknown = set(task) - allowed
        if unknown:
            raise ValueError(f"Unsupported generated task fields: {sorted(unknown)}")
        title = _require_nonempty_text(task.get("title"), "title")

        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                goal = await self._get("goals", goal_id, db)
                if goal is None:
                    raise KeyError(f"Goal {goal_id} does not exist")
                existing_cursor = await db.execute(
                    """SELECT * FROM goal_schedule_items
                       WHERE goal_id = ? AND period_start = ? AND ordinal = ?""",
                    (goal_id, start_text, ordinal),
                )
                existing = await self._goal_schedule_item(
                    db, await existing_cursor.fetchone()
                )
                if existing is not None:
                    await db.commit()
                    return existing

                values = {
                    key: value for key, value in task.items()
                    if key in allowed and value is not None
                }
                values["title"] = title
                values["goal_id"] = goal_id
                values["series_key"] = _require_nonempty_text(
                    task.get("series_key") or f"goal:{goal_id}", "series_key"
                )
                if "deadline" not in values:
                    values["deadline"] = period_end
                if "estimated_minutes" in values:
                    values.setdefault("estimate_source", "goal")
                for field in tuple(values):
                    values[field] = _db_value(field, values[field])
                fields = list(values)
                task_cursor = await db.execute(
                    f"INSERT INTO tasks ({', '.join(fields)}) "
                    f"VALUES ({', '.join('?' for _ in fields)})",
                    [values[field] for field in fields],
                )
                task_id = int(task_cursor.lastrowid)
                item_cursor = await db.execute(
                    """INSERT INTO goal_schedule_items
                       (goal_id, task_id, period_start, period_end, ordinal, planned_amount)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        goal_id, task_id, start_text, end_text, ordinal,
                        float(planned_amount),
                    ),
                )
                await db.commit()
                row = await (
                    await db.execute(
                        "SELECT * FROM goal_schedule_items WHERE id = ?",
                        (int(item_cursor.lastrowid),),
                    )
                ).fetchone()
                result = await self._goal_schedule_item(db, row)
            except Exception:
                await db.rollback()
                raise
        assert result is not None
        return result

    async def get_goal_schedule_items(
        self, goal_id: int, period_start: datetime, period_end: datetime
    ) -> list[Record]:
        """Return goal items whose periods overlap the supplied UTC range."""
        start_text = _utc_text(period_start, "period_start")
        end_text = _utc_text(period_end, "period_end")
        if end_text <= start_text:
            raise ValueError("period_end must be later than period_start")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM goal_schedule_items
                   WHERE goal_id = ?
                     AND utc_epoch_us(period_start) < utc_epoch_us(?)
                     AND utc_epoch_us(period_end) > utc_epoch_us(?)
                   ORDER BY utc_epoch_us(period_start), ordinal, id""",
                (goal_id, end_text, start_text),
            )
            result: list[Record] = []
            for row in await cursor.fetchall():
                item = await self._goal_schedule_item(db, row)
                assert item is not None
                result.append(item)
            return result

    async def cancel_goal_schedule_items(
        self,
        goal_id: int,
        period_start: datetime,
        period_end: datetime,
        keep_count: int,
    ) -> list[Record]:
        """Mark surplus generated items cancelled, retaining remote IDs for cleanup.

        Call :meth:`finalize_cancelled_goal_schedule_items` only after the
        caller has successfully deleted each returned task's owned calendar
        block.  This two-phase ordering prevents the local availability view
        from claiming a block is free while its remote counterpart survives.
        """
        start_text = _utc_text(period_start, "period_start")
        end_text = _utc_text(period_end, "period_end")
        if end_text <= start_text:
            raise ValueError("period_end must be later than period_start")
        if not isinstance(keep_count, int) or isinstance(keep_count, bool) or keep_count < 0:
            raise ValueError("keep_count must be a non-negative integer")
        cancelled_at = _utc_text(timeutil.now_utc(), "cancelled_at")
        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                cursor = await db.execute(
                    """SELECT gsi.*, t.status AS task_status,
                              EXISTS (
                                  SELECT 1 FROM schedule_decisions sd
                                  WHERE sd.task_id = gsi.task_id
                                    AND sd.trigger = 'user_request'
                              ) AS manually_fixed
                       FROM goal_schedule_items gsi
                       JOIN tasks t ON t.id = gsi.task_id
                       WHERE gsi.goal_id = ? AND gsi.period_start = ?
                         AND gsi.period_end = ? AND gsi.cancelled_at IS NULL
                       ORDER BY gsi.ordinal, gsi.id""",
                    (goal_id, start_text, end_text),
                )
                rows = await cursor.fetchall()
                completed_ids = {
                    int(row["id"]) for row in rows
                    if row["task_status"] == "completed"
                }
                manually_fixed_ids = {
                    int(row["id"]) for row in rows
                    if bool(row["manually_fixed"])
                    and int(row["id"]) not in completed_ids
                }
                open_rows = [
                    row for row in rows
                    if int(row["id"]) not in completed_ids | manually_fixed_ids
                ]
                # Completed occurrences have already contributed progress and
                # therefore do not consume the outstanding quota. A manually
                # placed open occurrence still does, because it represents an
                # intentionally preserved future session.
                keep_open = max(0, keep_count - len(manually_fixed_ids))
                to_cancel = open_rows[keep_open:]
                snapshots: list[Record] = []
                for row in to_cancel:
                    base_row = await (
                        await db.execute(
                            "SELECT * FROM goal_schedule_items WHERE id = ?",
                            (row["id"],),
                        )
                    ).fetchone()
                    snapshot = await self._goal_schedule_item(db, base_row)
                    assert snapshot is not None
                    snapshots.append(snapshot)
                    await db.execute(
                        "UPDATE goal_schedule_items SET cancelled_at = ? WHERE id = ?",
                        (cancelled_at, row["id"]),
                    )
                    await db.execute(
                        """UPDATE tasks SET status = 'dropped', completed_at = NULL
                           WHERE id = ? AND status IN ('pending', 'scheduled')""",
                        (row["task_id"],),
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
        for snapshot in snapshots:
            snapshot["cancelled_at"] = _utc_datetime(cancelled_at)
            if snapshot.get("task", {}).get("status") in {"pending", "scheduled"}:
                snapshot["task"]["status"] = "dropped"
        return snapshots

    async def finalize_cancelled_goal_schedule_items(
        self,
        goal_id: int,
        period_start: datetime,
        period_end: datetime,
        *,
        task_ids: Sequence[int],
    ) -> list[Record]:
        """Clear local blocks for cancelled items after owned events are deleted.

        ``task_ids`` must be the task IDs returned by
        :meth:`cancel_goal_schedule_items`; restricting finalization to those
        rows makes a remote-delete retry safe and avoids clearing an unrelated
        manual placement.
        """
        start_text = _utc_text(period_start, "period_start")
        end_text = _utc_text(period_end, "period_end")
        if end_text <= start_text:
            raise ValueError("period_end must be later than period_start")
        if not isinstance(task_ids, Sequence) or isinstance(task_ids, (str, bytes)):
            raise TypeError("task_ids must be a sequence of positive integers")
        normalized_ids = list(task_ids)
        if any(
            not isinstance(task_id, int)
            or isinstance(task_id, bool)
            or task_id <= 0
            for task_id in normalized_ids
        ):
            raise ValueError("task_ids must be a sequence of positive integers")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise ValueError("task_ids must not contain duplicates")
        if not normalized_ids:
            return []

        async with self.connection() as db:
            try:
                await db.execute("BEGIN IMMEDIATE")
                placeholders = ", ".join("?" for _ in normalized_ids)
                cursor = await db.execute(
                    f"""SELECT * FROM goal_schedule_items
                        WHERE goal_id = ? AND period_start = ? AND period_end = ?
                          AND cancelled_at IS NOT NULL
                          AND task_id IN ({placeholders})
                        ORDER BY ordinal, id""",
                    (goal_id, start_text, end_text, *normalized_ids),
                )
                rows = await cursor.fetchall()
                found_ids = {int(row["task_id"]) for row in rows}
                missing_ids = set(normalized_ids) - found_ids
                if missing_ids:
                    raise ValueError(
                        "task_ids must identify cancelled items in the supplied goal period: "
                        f"{sorted(missing_ids)}"
                    )
                for row in rows:
                    await db.execute(
                        """UPDATE tasks
                           SET status = 'dropped', completed_at = NULL,
                               scheduled_start = NULL, scheduled_end = NULL,
                               gcal_event_id = NULL
                           WHERE id = ?""",
                        (row["task_id"],),
                    )
                await db.commit()
                result: list[Record] = []
                for row in rows:
                    refreshed = await (
                        await db.execute(
                            "SELECT * FROM goal_schedule_items WHERE id = ?", (row["id"],)
                        )
                    ).fetchone()
                    item = await self._goal_schedule_item(db, refreshed)
                    assert item is not None
                    result.append(item)
            except Exception:
                await db.rollback()
                raise
        return result

    async def query_goals(
        self, active: bool | None = True, category: str | None = None
    ) -> list[Record]:
        clauses: list[str] = []
        params: list[Any] = []
        if active is not None:
            clauses.append("active = ?")
            params.append(int(active))
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self.connection() as db:
            cursor = await db.execute(
                f"""SELECT * FROM goals{where}
                    ORDER BY category, utc_epoch_us(created_at), id""",
                params,
            )
            goals = _records(await cursor.fetchall())
        today = timeutil.now_local().date()
        for goal in goals:
            local_start = (
                today - timedelta(days=today.weekday())
                if goal["period"] == "week" else today.replace(day=1)
            )
            goal["progress"] = await self.get_goal_progress(
                goal["id"], timeutil.day_bounds(local_start)[0]
            )
        return goals

    async def get_schedulable_tasks(self) -> list[Record]:
        """Return pending, estimated, near-deadline tasks ranked by urgency."""
        now = timeutil.now_utc()
        horizon = now + timedelta(days=config.SCHEDULER_LOOKAHEAD_DAYS)
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM tasks WHERE status = 'pending'
                   AND estimated_minutes IS NOT NULL AND deadline IS NOT NULL
                   AND utc_epoch_us(deadline) >= utc_epoch_us(?)
                   AND utc_epoch_us(deadline) < utc_epoch_us(?)
                   AND scheduled_start IS NULL AND scheduled_end IS NULL
                   ORDER BY utc_epoch_us(deadline), id""",
                (_utc_text(now), _utc_text(horizon)),
            )
            tasks = _records(await cursor.fetchall())
        priority_weight = {"low": 1.0, "medium": 2.0, "high": 3.0}
        for task in tasks:
            hours_left = max(
                (task["deadline"] - now).total_seconds() / 3600,
                0.25,
            )
            task["urgency_score"] = round(
                priority_weight[task["priority"]] / hours_left, 8
            )
        tasks.sort(
            key=lambda task: (task["urgency_score"], -task["deadline"].timestamp()),
            reverse=True,
        )
        return tasks

    async def append_message(
        self,
        role: Literal["user", "assistant", "tool"],
        content: str,
        tool_calls: list[Record],
        session_id: str,
    ) -> Record:
        if not isinstance(tool_calls, list):
            raise ValueError("tool_calls must be a list")
        _require_nonempty_text(session_id, "session_id")
        if not isinstance(content, str):
            raise TypeError("content must be a string")
        async with self.connection() as db:
            cursor = await db.execute(
                """INSERT INTO messages (role, content, tool_calls, session_id)
                   VALUES (?, ?, ?, ?)""",
                (
                    role,
                    content,
                    json.dumps(tool_calls, separators=(",", ":"), ensure_ascii=False),
                    session_id,
                ),
            )
            await db.commit()
            result = await self._get("messages", int(cursor.lastrowid), db)
        assert result is not None
        return result

    async def get_messages(self, session_id: str, limit: int = 100) -> list[Record]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 0:
            raise ValueError("limit must be a non-negative integer")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM (
                       SELECT * FROM messages WHERE session_id = ?
                       ORDER BY utc_epoch_us(created_at) DESC, id DESC LIMIT ?
                   ) ORDER BY utc_epoch_us(created_at), id""",
                (session_id, limit),
            )
            return _records(await cursor.fetchall())

    async def get_messages_between(
        self, start: datetime, end: datetime
    ) -> list[Record]:
        """Return conversation records in the half-open UTC interval ``[start, end)``."""
        start_text, end_text = _utc_text(start, "start"), _utc_text(end, "end")
        if end_text <= start_text:
            raise ValueError("end must be later than start")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT * FROM messages
                   WHERE utc_epoch_us(created_at) >= utc_epoch_us(?)
                     AND utc_epoch_us(created_at) < utc_epoch_us(?)
                   ORDER BY utc_epoch_us(created_at), id""",
                (start_text, end_text),
            )
            return _records(await cursor.fetchall())

    async def get_daily_log(self, local_date: date) -> Record | None:
        if type(local_date) is not date:
            raise TypeError("local_date must be a date")
        async with self.connection() as db:
            cursor = await db.execute(
                "SELECT * FROM daily_log WHERE date = ?", (local_date.isoformat(),)
            )
            return _record(await cursor.fetchone())

    async def upsert_daily_log(self, local_date: date, changes: Record) -> Record:
        if type(local_date) is not date:
            raise TypeError("local_date must be a date")
        unknown = set(changes) - _DAILY_LOG_FIELDS
        if unknown:
            raise ValueError(f"Unsupported daily-log fields: {sorted(unknown)}")
        values = {key: _db_value(key, value) for key, value in changes.items()}
        async with self.connection() as db:
            try:
                await db.execute(
                    "INSERT OR IGNORE INTO daily_log (date) VALUES (?)",
                    (local_date.isoformat(),),
                )
                if values:
                    await db.execute(
                        f"UPDATE daily_log SET {', '.join(f'{field} = ?' for field in values)} WHERE date = ?",
                        [*values.values(), local_date.isoformat()],
                    )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            cursor = await db.execute(
                "SELECT * FROM daily_log WHERE date = ?", (local_date.isoformat(),)
            )
            result = _record(await cursor.fetchone())
        assert result is not None
        return result

    async def record_usage(
        self,
        component: Literal["agent_loop", "session_summary", "scheduler", "facts"],
        model: str,
        usage: Record,
        estimated_cost_usd: float | None,
        session_id: str | None = None,
    ) -> None:
        """Persist one model call's token, cache, and estimated-cost counters."""
        allowed = {"agent_loop", "session_summary", "scheduler", "facts"}
        if component not in allowed:
            raise ValueError(f"unknown usage component: {component}")
        counters = {
            key: int(usage.get(key, 0) or 0)
            for key in (
                "input_tokens", "cached_tokens", "cache_write_tokens",
                "output_tokens", "reasoning_tokens", "total_tokens",
            )
        }
        if any(value < 0 for value in counters.values()):
            raise ValueError("usage counters must be non-negative")
        async with self.connection() as db:
            await db.execute(
                """INSERT INTO usage_events (
                       component, model, session_id, input_tokens, cached_tokens,
                       cache_write_tokens, output_tokens, reasoning_tokens,
                       total_tokens, estimated_cost_usd
                   ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    component, model, session_id,
                    counters["input_tokens"], counters["cached_tokens"],
                    counters["cache_write_tokens"], counters["output_tokens"],
                    counters["reasoning_tokens"], counters["total_tokens"],
                    estimated_cost_usd,
                ),
            )
            await db.commit()

    async def usage_summary(self, start: datetime, end: datetime) -> list[Record]:
        """Aggregate usage by interactive/background component for a UTC range."""
        start_text, end_text = _utc_text(start, "start"), _utc_text(end, "end")
        if end_text <= start_text:
            raise ValueError("end must be later than start")
        async with self.connection() as db:
            cursor = await db.execute(
                """SELECT
                       CASE WHEN component IN ('agent_loop', 'session_summary')
                            THEN 'agent' ELSE 'background' END AS kind,
                       SUM(input_tokens) AS input_tokens,
                       SUM(cached_tokens) AS cached_tokens,
                       SUM(cache_write_tokens) AS cache_write_tokens,
                       SUM(output_tokens) AS output_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(estimated_cost_usd) AS estimated_cost_usd,
                       COUNT(*) AS calls
                   FROM usage_events
                   WHERE utc_epoch_us(occurred_at) >= utc_epoch_us(?)
                     AND utc_epoch_us(occurred_at) < utc_epoch_us(?)
                   GROUP BY kind ORDER BY kind""",
                (start_text, end_text),
            )
            return _records(await cursor.fetchall())


async def create_store(db_path: str | Path | None = None) -> Store:
    """Create and initialize a store instance."""
    store = Store(db_path)
    await store.initialize()
    return store
