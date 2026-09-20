# Dharvis Foundation Contracts

All `datetime` arguments and return values are timezone-aware. Persistence stores timestamp text in UTC only. Local-day ranges are half-open: `[start, next_day_start)`. The OpenAI JSON schemas in `src/tools.py` are the authoritative tool contract; this document is the implementation index.

The aliases used below are:

```python
Record = dict[str, Any]
CalendarRecord = dict[str, Any]
Message = dict[str, Any]
Fact = dict[str, Any]
ToolSchema = dict[str, Any]
TaskStatus = Literal["pending", "scheduled", "completed", "dropped"]
ReminderStatus = Literal["pending", "delivered", "cancelled"]
EventColorId = Literal["1", "2", "3", "4", "5", "6", "7", "8", "9", "10", "11"]
DecisionAction = Literal["scheduled", "moved", "unscheduled", "shortened", "extended"]
Trigger = Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"]
ProgressSource = Literal["task", "manual", "inferred"]
ActualMinutesSource = Literal["user", "debrief", "calendar", "inferred"]
MessageRole = Literal["user", "assistant", "tool"]
ScheduleSource = Literal["gcal", "event", "task"]
ReasoningVerbosity = Literal["brief", "full"]
RunMode = Literal["polling", "webhook"]
```

## Configuration (`src.config`)

```python
Config.TELEGRAM_BOT_TOKEN: str
Config.ALLOWED_USER_ID: int | None
Config.TELEGRAM_POLL_TIMEOUT_SECONDS: int
Config.RUN_MODE: RunMode | str
Config.PUBLIC_BASE_URL: str
Config.TELEGRAM_WEBHOOK_PATH: str
Config.TELEGRAM_WEBHOOK_SECRET: str
Config.OPENAI_API_KEY: str
Config.AGENT_MODEL_ID: str
Config.SUMMARY_MODEL_ID: str
Config.SCHEDULER_MODEL_ID: str
Config.FACTS_MODEL_ID: str
Config.OPENAI_REASONING_EFFORT: str
Config.ANTHROPIC_API_KEY: str  # temporary legacy compatibility
Config.USER_TIMEZONE: str
Config.QUIET_HOURS_START: str
Config.QUIET_HOURS_END: str
Config.DAILY_BRIEF_TIME: str
Config.DAILY_DEBRIEF_TIME: str
Config.WEEKLY_REVIEW_TIME: str
Config.REASONING_VERBOSITY: ReasoningVerbosity
Config.GOOGLE_CALENDAR_CREDENTIALS_PATH: Path
Config.GOOGLE_CALENDAR_TOKEN_PATH: Path
Config.GOOGLE_CALENDAR_TOKEN_BASE64: str
Config.GOOGLE_CALENDAR_ID: str
Config.KALENDRA_CALENDAR_NAME: str
Config.KALENDRA_CALENDAR_ID: str | None
Config.DATA_DIR: Path
Config.DATABASE_PATH: Path
Config.APSCHEDULER_DATABASE_PATH: Path
Config.HEALTH_PORT: int
Config.MESSAGE_HISTORY_LIMIT: int
Config.DEFAULT_TASK_MINUTES: int
Config.SCHEDULER_LOOKAHEAD_DAYS: int
Config.validate() -> list[str]
```

Production startup requires both `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_ID` and fails closed when either is absent. `RUN_MODE` defaults to `polling`; webhook mode additionally requires a pathless HTTPS `PUBLIC_BASE_URL` and URL-safe webhook path and secret. Clock settings are local 24-hour `HH:MM` strings. `DATA_DIR` defaults to `./data` and derives the database, scheduler database, OAuth credentials, and OAuth token paths unless their corresponding explicit path variable is set. OAuth token material created or refreshed by the app is mode `0600`.

Saved `app_settings` morning/evening clocks override `DAILY_BRIEF_TIME` and `DAILY_DEBRIEF_TIME`; environment values remain the defaults for an unset preference. `Config` disables its dataclass representation, so `repr(config)` exposes no field values, including secrets.

## Time (`src.timeutil`)

```python
now_local() -> datetime
now_utc() -> datetime
to_utc(value: datetime) -> datetime
to_local(value: datetime) -> datetime
format_time_context() -> str
resolve_relative(phrase: str, ref: datetime | None = None) -> datetime
day_bounds(value: date | datetime) -> tuple[datetime, datetime]
```

A same-day weekday phrase with a clock resolves today if that time has not passed and next week otherwise. A bare same-day weekday resolves at the reference time today. Explicit `next` always chooses a future occurrence. `tonight` remains on the reference local date, even at 11 PM.

## Tool schemas (`src.tools`)

```python
get_tool_schemas() -> list[ToolSchema]
```

`TOOLS` is the ordered OpenAI tool list and `TOOLS_BY_NAME` is its name index. Every tool is strict, every object rejects additional properties, and nullable fields remain required in the JSON schema. Tool names are:

```text
add_task             add_event              update_task
update_event         confirm_event_change   complete_task
log_task_progress    delete_task
delete_event         query_schedule         query_tasks
add_reminder         update_reminder        cancel_reminder
query_reminders      find_free_blocks       schedule_task
explain_schedule
add_fact             update_fact            query_facts
add_goal             log_goal_progress      query_goals
resolve_date
```

`schedule_task.reasoning` is required and is a one-sentence explanation recorded at decision time. Its handler must call `Store.apply_schedule_decision` so placement and rationale commit atomically. `add_task`, `add_event`, and `add_reminder` accept arrays. Reminders are one-time Telegram nudges stored only in SQLite: their tools never call Google Calendar, free/busy, or the scheduler. Tasks include a nullable `series_key`, which groups recurring work for deterministic duration inference; an explicit `estimated_minutes` wins over inferred evidence and the configured default. Conflicting fixed-event changes return a one-time proposal and require `confirm_event_change` in a later user turn. Strict update schemas require every field: `null` means unchanged, while nullable values are cleared only through the required `clear_fields` array (`[]` means clear nothing). `add_event.color_id` and `update_event.color_id` accept only Google event color ID strings `1` through `11`: lavender, sage, grape, flamingo, banana, tangerine, peacock, graphite, blueberry, basil, and tomato. A null add color uses Dharvis's deterministic category/kind default; a null update color is unchanged unless `color_id` is listed in `clear_fields`, which removes the override so the event inherits Kalendra's calendar color. External events are read-only.

`log_task_progress` requires `task_id`, `total_minutes`, `remaining_minutes`, and `notes`; the last three are nullable. `total_minutes` is cumulative observed work, not an increment, and retries repeat the same total. Only an explicit user remaining-time estimate changes `estimated_minutes`; elapsed work is never subtracted to guess it. Progress preserves task identity, unfinished status, and the existing scheduled interval. It neither credits a goal nor automatically replans or resizes a work block. `complete_task` is reserved for finished work; `calendar_sync_pending` in its result means local completion succeeded and owned calendar cleanup remains queued.

## Schema migration (`src.migrate`)

```python
run_migrations(db_path: str | Path | None = None) -> None  # async
main() -> None
```

`src/schema.sql` is the only canonical schema file. Later agents must not add migration files.
The current canonical schema version is 6. It adds task fields `progress_minutes` (nonnegative, default 0), `progress_notes`, `progress_updated_at`, and `reopened_at`, plus `app_settings` (morning/evening local clocks), `processed_updates` (Telegram update receipts), `calendar_cleanup` (pending owned work-block deletions), and `debrief_learning_attempts` (immutable evidence snapshots with a separate processing acknowledgement). Version 5's nullable `events.color_id` remains constrained to Google event color ID strings `1` through `11`; null means inheritance from the calendar. Existing task duplicates are preserved, not merged or deleted during migration.
The migration runner alone accepts naive timestamps from the retired database and interprets them in `USER_TIMEZONE` before converting to UTC. Runtime APIs reject naive datetimes; this recovery policy must not be copied into new writes.

## Store (`src.store`)

```python
Store(db_path: str | Path | None = None)
Store.initialize() -> None  # async
Store.connection() -> AsyncIterator[aiosqlite.Connection]  # @asynccontextmanager
Store.add_tasks(tasks: list[Record]) -> list[Record]  # async
Store.get_notification_times() -> dict[str, str]  # async
Store.set_notification_times(*, morning: str | None = None, evening: str | None = None) -> dict[str, str]  # async
Store.has_processed_update(update_id: int) -> bool  # async
Store.mark_update_processed(update_id: int) -> None  # async
Store.list_pending_calendar_cleanup(limit: int = 50, *, task_id: int | None = None) -> list[Record]  # async
Store.ack_calendar_cleanup(gcal_event_id: str) -> None  # async
Store.add_events(events: list[Record]) -> list[Record]  # async
Store.add_reminders(reminders: list[Record]) -> list[Record]  # async
Store.get_task(task_id: int) -> Record | None  # async
Store.get_event(event_id: int) -> Record | None  # async
Store.get_reminder(reminder_id: int) -> Record | None  # async
Store.update_task(task_id: int, changes: Record) -> Record  # async
Store.update_event(event_id: int, changes: Record) -> Record  # async
Store.update_reminder(reminder_id: int, changes: Record) -> Record  # async
Store.cancel_reminder(reminder_id: int, cancelled_at: datetime | None = None) -> Record  # async
Store.query_reminders(status: ReminderStatus | None = None, remind_before: datetime | None = None, remind_after: datetime | None = None) -> list[Record]  # async
Store.claim_due_reminders(now: datetime, lease_for: timedelta = timedelta(minutes=2), limit: int = 20) -> list[Record]  # async
Store.ack_reminder_delivery(reminder_id: int, claim_token: str, delivered_at: datetime | None = None) -> Record  # async
Store.release_reminder_delivery(reminder_id: int, claim_token: str, failed_at: datetime, retry_at: datetime) -> Record  # async
Store.create_event_change_proposal(operation: Literal["create", "update"], payload: Record, conflicts: list[Record], expires_at: datetime) -> Record  # async
Store.get_event_change_proposal(proposal_id: str) -> Record | None  # async
Store.consume_event_change_proposal(proposal_id: str, consumed_at: datetime | None = None) -> Record  # async
Store.claim_event_change_proposal(proposal_id: str, claimed_at: datetime | None = None) -> Record  # async
Store.finalize_event_change_proposal(proposal_id: str, claim_token: str, consumed_at: datetime | None = None) -> Record  # async
Store.release_event_change_proposal(proposal_id: str, claim_token: str) -> Record  # async
Store.infer_task_duration(title: str, category: str, energy: str, series_key: str | None = None, limit: int = 5) -> Record  # async
Store.complete_task(task_id: int, actual_minutes: int | None = None, actual_minutes_source: ActualMinutesSource | None = None, *, observed_at: datetime | None = None) -> Record  # async
Store.log_task_progress(task_id: int, total_minutes: int | None = None, remaining_minutes: int | None = None, notes: str | None = None) -> Record  # async
Store.drop_task(task_id: int) -> Record  # async
Store.delete_task(task_id: int) -> Record  # async; drops without erasing history
Store.delete_event(event_id: int) -> bool  # async
Store.query_tasks(status: TaskStatus | None = None, category: str | None = None, due_before: datetime | None = None, due_after: datetime | None = None) -> list[Record]  # async
Store.query_events(start: datetime, end: datetime) -> list[Record]  # async
Store.apply_schedule_decision(task_id: int, action: DecisionAction, start: datetime, end: datetime, previous_start: datetime | None, previous_end: datetime | None, trigger: Trigger, reasoning: str, facts_used: list[int], gcal_event_id: str | None) -> Record  # async, one DB transaction
Store.get_schedule_decisions(task_id: int) -> list[Record]  # async
Store.get_decisions_for_task(task_id: int) -> list[Record]  # async
Store.get_unsurfaced_decisions(since: datetime) -> list[Record]  # async
Store.get_schedule_decisions_between(start: datetime, end: datetime) -> list[Record]  # async
Store.mark_decision_surfaced(decision_id: int) -> None  # async
Store.add_fact(fact: Record) -> Record  # async
Store.update_fact(fact_id: int, changes: Record) -> Record  # async
Store.query_facts(category: str | None = None, active: bool | None = True, min_confidence: float | None = None) -> list[Record]  # async
Store.add_goal(goal: Record) -> Record  # async
Store.log_goal_progress(goal_id: int, amount: float, source: ProgressSource, logged_at: datetime, task_id: int | None = None) -> Record  # async
Store.get_goal_progress(goal_id: int, period_start: datetime) -> Record  # async
Store.ensure_goal_schedule_item(goal_id: int, task: Record, period_start: datetime, period_end: datetime, ordinal: int, planned_amount: float) -> Record  # async
Store.get_goal_schedule_items(goal_id: int, period_start: datetime, period_end: datetime) -> list[Record]  # async
Store.cancel_goal_schedule_items(goal_id: int, period_start: datetime, period_end: datetime, keep_count: int) -> list[Record]  # async
Store.finalize_cancelled_goal_schedule_items(goal_id: int, period_start: datetime, period_end: datetime, *, task_ids: Sequence[int]) -> list[Record]  # async
Store.query_goals(active: bool | None = True, category: str | None = None) -> list[Record]  # async
Store.append_message(role: MessageRole, content: str, tool_calls: list[Record], session_id: str) -> Record  # async
Store.get_messages(session_id: str, limit: int = 100) -> list[Record]  # async
Store.get_messages_between(start: datetime, end: datetime) -> list[Record]  # async
Store.get_daily_log(local_date: date) -> Record | None  # async
Store.upsert_daily_log(local_date: date, changes: Record) -> Record  # async
Store.save_debrief_learning(local_date: date, changes: Record, snapshot: Record) -> Record  # async
Store.get_pending_debrief_learning(local_date: date | None = None, limit: int = 50) -> list[Record]  # async
Store.ack_debrief_learning(attempt_id: int) -> None  # async
Store.record_usage(component: Literal["agent_loop", "session_summary", "scheduler", "facts"], model: str, usage: Record, estimated_cost_usd: float | None, session_id: str | None = None) -> None  # async
Store.usage_summary(start: datetime, end: datetime) -> list[Record]  # async
create_store(db_path: str | Path | None = None) -> Store  # async
```

Every Store connection enables `PRAGMA foreign_keys = ON`. `apply_schedule_decision` is the only placement mutation contract: it updates the task placement and inserts the nonblank `schedule_decisions` row in one transaction. `facts_used` accepts only existing integer fact IDs. Event-change proposals are one-time, expiring records: callers claim a proposal before the external write, finalize it only after the write succeeds, and release the claim only after compensated failure. Goal-session cancellation is likewise two phase: retain its local calendar ID until the owned remote block is deleted, then finalize the local cleanup. `infer_task_duration` uses only recent completed tasks with the same normalized `series_key`, category, and energy; it takes a robust median of recorded `actual_minutes`, returns evidence task IDs, and uses neither a model nor a vector index.

`add_tasks` serializes creation and reuses an active task only when its complete title (case/whitespace normalized), deadline instant, category, and goal match. It returns that task unchanged; task-family similarity does not merge occurrences. Existing duplicate rows are not repaired destructively.

`complete_task` atomically sets completion, clears local placement, queues the old calendar ID for cleanup, and credits the linked goal once. Replays preserve the original completion timestamp and observed duration. Session goals receive one session; hour goals use supplied actual minutes, otherwise nonzero partial progress, otherwise the estimate. Partial minutes never become final `actual_minutes` for duration inference. Reopening through `update_task(status="pending")` atomically clears completion metadata, records `reopened_at`, removes automatic task goal credit and old daily-log completion entries, and preserves partial progress, manual goal logs, and queued cleanup. An `observed_at` at or before `reopened_at` cannot re-complete the task from a stale checklist.

`save_debrief_learning` commits daily-log changes and a snapshot of that persisted row together with the caller's same-day conversation, decisions, and metadata in one transaction. Retries read the saved snapshot unchanged; acknowledgement sets only `processed_at`. Concurrently reopened tasks are excluded from saved completion entries. Notification clock updates validate all supplied values before atomically saving them; quiet-hour policy belongs to `jobs.update_notification_times`. Telegram receipts retain 30 days of update IDs and do not use message text as identity.

Reminder delivery uses an explicit `pending -> delivered` or `pending -> cancelled` lifecycle. The dispatcher atomically claims due records with opaque, expiring per-reminder leases, acknowledges only after Telegram accepts the send, and releases failed claims with a retry time. This provides at-least-once delivery: a crash after Telegram accepts a message but before acknowledgement can cause a rare duplicate, while expired leases and startup catch-up prevent silent loss. Delivered and cancelled reminders are immutable audit records, and a reminder under a live delivery lease cannot be edited or cancelled.

## Calendar (`src.calendar_service`)

```python
CalendarError(RuntimeError)
CalendarReconnectRequiredError(CalendarError)
CalendarWriteUncertainError(CalendarError)
CalendarReconnectRequired = CalendarReconnectRequiredError
CalendarService(credentials_path: Path | None = None, token_path: Path | None = None, calendar_id: str | None = None)
CalendarService.is_available() -> bool
CalendarService.list_events(start: datetime, end: datetime, *, force_refresh: bool = False) -> list[CalendarRecord]  # async
CalendarService.get_events_between(start: datetime, end: datetime, *, force_refresh: bool = False) -> list[CalendarRecord]  # async
CalendarService.get_today_events() -> list[CalendarRecord]  # async
CalendarService.get_upcoming_events(days: int = 7) -> list[CalendarRecord]  # async
CalendarService.check_availability(start: datetime, end: datetime) -> bool  # async
CalendarService.get_owned_event(gcal_event_id: str) -> CalendarRecord  # async
CalendarService.create_event(event: CalendarRecord, reasoning: str | None = None, *, category: str | None = None, kind: str = "fixed-event") -> CalendarRecord  # async
CalendarService.update_event(gcal_event_id: str, changes: CalendarRecord, *, category: str | None = None, kind: str | None = None) -> CalendarRecord  # async
CalendarService.delete_event(gcal_event_id: str) -> None  # async
CalendarService.clear_kalendra_range(start: datetime, end: datetime) -> None  # async
CalendarService.create_work_block(task_id: int, title: str, start: datetime, end: datetime, reasoning: str | None = None, *, category: str | None = None, kind: str = "task-block", goal_id: int | None = None) -> str  # async
CalendarService.update_work_block(gcal_event_id: str, title: str, start: datetime, end: datetime, reasoning: str | None = None, *, category: str | None = None, kind: str = "task-block", goal_id: int | None = None) -> None  # async
CalendarService.delete_work_block(gcal_event_id: str) -> None  # async
run_oauth_flow(credentials_path: Path | None = None, token_path: Path | None = None) -> bool
create_calendar_service() -> CalendarService  # async
normalize_event_color_id(value: Any) -> str
```

OAuth uses the read/write Calendar scope. Reads cover every visible calendar and cache complete results in memory for 60 seconds; `force_refresh=True` bypasses that cache for write-adjacent safety checks. Returned Google records retain visible-calendar metadata (summary, primary/access role, and colors). Writes are restricted to marked, application-owned events on a dedicated secondary calendar named `Kalendra`; its ID is persisted beside the OAuth token, and primary-calendar and other external events are read-only. `get_owned_event` performs an exact-ID read from that owned calendar, verifies the ownership marker, and returns the normalized record; it never searches external calendars or depends on a time range. New owned events carry `kalendra_owned=v1` and canonical `kalendra_kind` values `fixed-event`, `task-block`, or `goal-session` (the legacy work-block marker remains readable). Category and kind select a deterministic event-level color unless the user supplies an event color ID from `1` through `11`; setting one updates the Google event override, while explicitly clearing it sends null and restores inheritance from Kalendra's calendar color. A manual nondefault event color survives later metadata updates. Google event colors (`color_id`) and calendar colors (`calendar_color_id`) are different palette namespaces: only a non-null event-level ID can be copied exactly from a reference. A reference with null `color_id` inherits its source calendar's color, which may not have an exact event-palette equivalent, so callers must ask for a named event color rather than copying the calendar ID. Creating a block requires a nonblank rationale, supplied as `reasoning` or `event["reasoning"]`, which is rendered in the Google event description. `clear_kalendra_range` and `delete_work_block` delete only owned movable task/goal blocks, never fixed events; remote 404 deletion is retry-safe.

Credential refresh is transparent and refreshed tokens are replaced atomically with mode `0600`. Missing or malformed authorization, `invalid_grant`/`invalid_scope`, a required reauthentication, HTTP 401, and a scope-specific HTTP 403 raise `CalendarReconnectRequiredError`. Transport failures, rate limits, transient refresh errors, token-storage failures, and other permission errors do not falsely request reconnection. Safe reads/deletes use bounded retries for transient failures; ambiguous inserts/updates are not automatically retried. `CalendarWriteUncertainError` means a write may have reached Google: callers must not assert rollback or release a confirmation claim as compensated. A token-save failure after confirmed remote success retains the valid credentials for a later storage retry without reporting that write as failed.

Every returned `start_time` and `end_time` is UTC-aware ISO-8601 text. Google `dateTime` offsets are converted to UTC; all-day `date` values become the UTC instants for local midnight boundaries. Returned occupied events always have positive duration. Explicit Google entries whose normalized start and end instants are equal are omitted as non-occupying without making the read incomplete; reversed or missing endpoints, structurally invalid or non-object entries, and HTTP or inaccessible-calendar failures still make the read incomplete and preserve fail-closed behavior.

## Free/busy (`src.freebusy`)

```python
ScheduleBlock(start: datetime, end: datetime, title: str, source: ScheduleSource, source_id: str, metadata: dict[str, Any])
FreeBlock(start: datetime, end: datetime, after: str | None = None, before: str | None = None)
FreeBlock.after_title: str | None
FreeBlock.before_title: str | None
CalendarQueryIncompleteError(CalendarError)
compute_free_blocks(start: datetime, end: datetime, min_minutes: int, constraints: Any) -> list[FreeBlock]
is_free(start: datetime, end: datetime, constraints: Any) -> bool
next_free_block(after: datetime, min_minutes: int, constraints: Any, *, search_end: datetime) -> FreeBlock | None
query_schedule(store: Store, calendar: CalendarService, start: datetime, end: datetime, *, force_refresh: bool = False) -> list[ScheduleBlock]  # async
merge_blocks(blocks: list[ScheduleBlock]) -> list[ScheduleBlock]
find_free_blocks(store: Store, calendar: CalendarService, start: datetime, end: datetime, min_minutes: int) -> list[FreeBlock]  # async
overlapping_blocks(blocks: Sequence[ScheduleBlock], start: datetime, end: datetime) -> list[ScheduleBlock]
has_conflict(blocks: list[ScheduleBlock], start: datetime, end: datetime) -> bool
```

`compute_free_blocks` is deterministic and accepts mapping- or object-style constraints. Busy values may be supplied as `busy_intervals`, `busy_blocks`, or `busy`; waking and quiet hours accept clock pairs or `{start, end}` mappings; `timezone` selects the IANA zone; and `buffer_minutes` defaults to 15. It merges overlapping buffered busy intervals, respects cross-midnight waking and quiet hours, interprets all-day boundaries at local midnight, and constructs each local day independently so DST transitions retain their real elapsed length. Free blocks identify their adjacent blockers through `after` and `before` (also exposed by the compatibility properties `after_title` and `before_title`).

`query_schedule(..., force_refresh=True)` makes the Google portion a cache-bypassing read and still fails closed when that response is incomplete. `overlapping_blocks` uses half-open intervals, so touching boundaries are not conflicts.

## Agent and history (`src.agent`, `src.history`)

```python
AgentTurnResult(text: str, *, failed: bool = False, mutation_attempted: bool = False)  # subclass of str
AgentTurnResult.failed: bool
AgentTurnResult.mutation_attempted: bool
AgentTurnResult.retry_safe: bool
Agent(history: History | None = None)
Agent.build_system_prompt() -> str
Agent.respond(message: str, session_id: str) -> str  # async
Agent.execute_tool(name: str, arguments: dict[str, Any]) -> Any  # async
Agent.run_tool_loop(message: str, session_id: str) -> str  # async
create_agent(history: History | None = None) -> Agent  # async

History(store: Store)
History.append(session_id: str, role: MessageRole, content: str, tool_calls: list[dict[str, Any]] | None = None) -> Message  # async
History.load(session_id: str, limit: int = 100) -> list[Message]  # async
History.clear(session_id: str) -> None  # async
History.to_openai_input(messages: list[Message]) -> list[dict[str, Any]]
create_history(store: Store) -> History  # async
```

Agent replies retain the `str` interface and carry `AgentTurnResult` metadata per turn. `retry_safe` is true only for a failed turn with no attempted mutation. Empty, incomplete, and failed model responses are explicit failures; after a possible mutation the reply warns that some changes may already be saved. Transport code must use the typed metadata, not wording or shared agent state, to decide whether a delivered reply may receive a receipt.

## Telegram (`src.telegram_handler`)

```python
TelegramHandler(agent: Agent | Any | None = None, *, store: Any | None = None, database: Any | None = None, claude_agent: Any | None = None, calendar_service: Any | None = None)
TelegramHandler.is_authorized(user_id: int) -> bool
TelegramHandler.start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.times_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.create_application(token: str | None = None) -> Application
create_telegram_handler(agent: Agent, store: Any | None = None) -> TelegramHandler
build_application() -> Application
initialize_application_runtime(application: Application) -> None  # async
```

`/times` displays saved morning/evening clocks. `/times morning HH:MM` and `/times evening HH:MM` accept strict local 24-hour times and reject quiet-hour choices. Settings take effect in the running scheduler and are loaded before job registration after restart. Authorized new-message processing serializes identical update IDs in process and records durable receipts only after a qualifying reply is delivered. Safely retryable pre-mutation failures and transport failures remain unreceipted; delivered replies after a possible mutation suppress automatic replay. This is not an exactly-once guarantee across a process crash between tool effects, delivery, and receipt persistence.

## Scheduler (`src.scheduler_engine`)

```python
ScheduleDecision(task_id: int, action: DecisionAction, start: datetime, end: datetime, previous_start: datetime | None, previous_end: datetime | None, trigger: Trigger, reasoning: str, facts_used: list[int])
SchedulerEngine(store: Store, calendar: CalendarService)
SchedulerEngine.choose_slot(task_id: int, candidates: list[FreeBlock], trigger: Trigger) -> ScheduleDecision  # async
SchedulerEngine.schedule_task(task_id: int, start: datetime, end: datetime, reasoning: str, trigger: Trigger, facts_used: list[int] | None = None) -> ScheduleDecision  # async
SchedulerEngine.plan_day(local_date: date) -> list[ScheduleDecision]  # async
SchedulerEngine.reconcile_goal_schedule(reference: date | datetime | None = None, **kwargs: Any) -> list[ScheduleDecision]  # async
SchedulerEngine.replan_missed_goal_sessions(reference: date | datetime | None = None, **kwargs: Any) -> list[ScheduleDecision]  # async
SchedulerEngine.refresh_goal_plan(reference: date | datetime | None = None, **kwargs: Any) -> list[ScheduleDecision]  # async
SchedulerEngine.build_daily_plan(local_date: date) -> list[ScheduleDecision]  # async
SchedulerEngine.reschedule(reason: str, affected_range: Any, *, trigger: Trigger = "conflict") -> list[ScheduleDecision]  # async
SchedulerEngine.detect_conflicts(start: datetime | None = None, end: datetime | None = None) -> list[ScheduleDecision]  # async
SchedulerEngine.format_change_summary(decisions: Sequence[Any], *, mark_surfaced: bool = True) -> str  # async
SchedulerEngine.mark_decisions_surfaced(decisions: Sequence[Any]) -> None  # async
SchedulerEngine.resolve_conflicts(start: datetime, end: datetime) -> list[ScheduleDecision]  # async
SchedulerEngine.explain_schedule(task_id: int) -> list[dict[str, object]]  # async
create_scheduler_engine(store: Store, calendar: CalendarService) -> SchedulerEngine  # async
```

`reconcile_goal_schedule` materializes idempotent goal-session tasks for outstanding weekly or monthly quota, schedules urgent ordinary work first, and paces goal sessions across remaining local dates. Elapsed automatic sessions are rescheduled; manually requested placements remain fixed. It also retries durable two-phase cleanup of surplus goal sessions. Conflict detection repairs manual Google moves, removes owned orphan work blocks when safe, and emits a durable repair-required signal if a remote repair cannot complete.

## Proactive jobs (`src.jobs`)

```python
send_daily_brief(store: Store, telegram: Any, local_date: date) -> None  # async
send_daily_debrief(store: Store, telegram: Any, local_date: date) -> None  # async
send_weekly_review(store: Store, telegram: Any, local_date: date) -> None  # async
handle_debrief_submission(store: Store, facts_engine: Any, telegram: Any, event: Record, session_id: str | None = None) -> None  # async
load_notification_times(store: Store) -> None  # async
update_notification_times(store: Store, *, morning: str | None = None, evening: str | None = None) -> dict[str, str]  # async
deliver_due_reminders(store: Store, telegram: Any, *, now: datetime | None = None) -> int  # async
run_daily_planning(engine: SchedulerEngine, local_date: date) -> None  # async
reconcile_calendar(engine: SchedulerEngine) -> None  # async
configure_jobs(scheduler: Any, store: Store, engine: SchedulerEngine, telegram: Any, facts_engine: Any | None = None) -> None
create_job_scheduler() -> Any
start_job_scheduler(scheduler: Any, *, catch_up: bool = True) -> Any
shutdown_job_scheduler(scheduler: Any, *, wait: bool = True) -> Any
start_scheduler(application: Application) -> None
stop_scheduler() -> None
run_startup_catchup() -> None  # async
```

The stable reminder dispatcher job runs every 30 seconds and is also the first startup catch-up action. Explicit reminder delivery bypasses quiet-hour and active-conversation deferral so the requested instant is honored. It processes a bounded leased batch, retries individual failures with capped exponential backoff, and continues past a failed send. The morning brief only reads reminder state: it includes pending reminders overdue through the end of the second following local day, but does not claim, acknowledge, or suppress their normal due-time delivery.

Morning/evening clock changes replace the stable planning, morning, and debrief jobs and remove stale deferred occurrences. Planning remains 15 minutes before the morning clock; daily delivery markers prevent resending an already delivered check-in after an edit or restart. Briefs render local human-readable times and concise recorded reasons. Weekly reviews report saved completion counts, concrete goal totals, and still-open planned tasks without inventing behavioral insights.

Debrief submission persists outcomes and immutable day-scoped learning evidence without awaiting facts-model extraction. A separate five-minute job and startup catch-up retry pending snapshots with a 30-second extraction timeout, including older days. A successful extraction is acknowledged separately; failures retain the original evidence for retry. A reopened task invalidates an older queued completion observation instead of rewriting its snapshot. Follow-up reflections create new attempts without replaying task completion or goal credit. Calendar cleanup is retried at startup and during reconciliation independently of successful local completion.

## Facts (`src.facts_engine`)

```python
FactsEngine(store: Store)
FactsEngine.extract_facts(user_message: str, assistant_message: str) -> list[Fact]  # async
FactsEngine.extract_from_day(daily_log: Mapping[str, Any] | Sequence[Any] | str | None, conversation: Sequence[Any] | Mapping[str, Any] | str | None, decisions: Sequence[Any] | Mapping[str, Any] | str | None) -> list[Fact]  # async
FactsEngine.consolidate(candidates: list[Fact], *, contradictions: Sequence[Mapping[str, Any]] | None = None, override_fact_ids: set[int] | None = None, observation_key: str | None = None, evidence_key: str | None = None) -> list[Fact]  # async
FactsEngine.relevant_facts(context: str, category: str | None = None, limit: int = 20) -> list[Fact]  # async
FactsEngine.seed_facts(facts: list[Fact]) -> list[Fact]  # async
FactsEngine.confirm_fact(fact_id: int, confidence: float = 1.0) -> Fact  # async
FactsEngine.deactivate_fact(fact_id: int) -> Fact  # async
create_facts_engine(store: Store) -> FactsEngine  # async
```

Exact extraction inputs have a cached observation identity; daily evidence has a separate date identity. Replaying an attempt or adding a reflection on the same day cannot turn one day into multiple supporting observations for a fact. New same-day evidence text is retained without increasing its evidence count or confidence as another independent habit sample.

## Runtime integration (`src.integration`)

```python
jsonable(value: Any) -> Any
complete_task_with_calendar(store: Store, calendar: CalendarService | None, task_id: int, actual_minutes: int | None = None, *, actual_minutes_source: ActualMinutesSource | None = None, observed_at: datetime | None = None) -> Record  # async
drain_calendar_cleanup(store: Store, calendar: CalendarService | None, *, limit: int = 50) -> int  # async
build_tool_handlers(store: Store, calendar: CalendarService, scheduler: SchedulerEngine, facts_engine: FactsEngine) -> dict[str, ToolHandler]  # async
```

The returned registry contains exactly every name in `TOOLS_BY_NAME`. Tool
results contain only JSON-compatible values and never naive datetimes.
`complete_task_with_calendar` commits local completion first, attempts queued owned-block deletion, and returns `calendar_sync_pending` plus a plain-language warning when cleanup remains. `drain_calendar_cleanup` acknowledges only successful deletions and returns the repaired count; failed entries remain durable.

## Entrypoint (`src.main`)

```python
build_application() -> tuple[TelegramHandler, object]  # async
main(*, check_only: bool = False) -> None  # async
run() -> None
```

Run with `python -m src.main`; `python -m src.main --check` builds the configured schema and Telegram application, then exits before network polling. The check still requires explicit safe values for both `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_ID`; either missing value makes the CLI exit with status 2.

## Webhook ingress (`src.web`)

```python
application: Application
lifespan(app: FastAPI) -> AsyncIterator[None]  # async context manager
app: FastAPI
healthz() -> dict[str, object]  # async; GET /healthz
telegram_webhook(path: str, request: Request) -> Response  # async; POST /{path:path}
```

`src.web` is the webhook-mode ASGI entry point. Its lifespan initializes the
application runtime, starts the Telegram application, registers the configured
webhook, starts the shared scheduler, and reverses those operations on shutdown.
The update route matches only `TELEGRAM_WEBHOOK_PATH` and verifies Telegram's
secret-token header before enqueueing a decoded update. Launch it with
`uvicorn src.web:app --host 0.0.0.0 --port 8000`.

## Legacy import compatibility

These surfaces remain only so the pre-foundation imports do not break. New agents build against `Store`, `Agent`, `CalendarService`, and `timeutil` above.

```python
Database(db_path: Path | None = None)
Database.init_db() -> None  # async; delegates only to run_migrations
Database.add_task(title: str, deadline: str | datetime | None = None, priority: str = "medium", description: str | None = None) -> int  # async
Database.get_task(task_id: int) -> dict[str, Any] | None  # async
Database.get_pending_tasks() -> list[dict[str, Any]]  # async
Database.get_tasks_due_by(date: str | datetime) -> list[dict[str, Any]]  # async
Database.complete_task(task_id: int | None = None, title: str | None = None) -> bool  # async
Database.delete_task(task_id: int | None = None, title: str | None = None) -> bool  # async
Database.update_task(task_id: int, **fields: Any) -> bool  # async
Database.fuzzy_match_task(title: str) -> dict[str, Any] | None  # async
Database.add_event(title: str, start_time: str | datetime, end_time: str | datetime | None = None, location: str | None = None, description: str | None = None, source: str = "bot") -> int  # async
Database.get_event(event_id: int) -> dict[str, Any] | None  # async
Database.get_events_between(start: str | datetime, end: str | datetime) -> list[dict[str, Any]]  # async
Database.delete_event(event_id: int | None = None, title: str | None = None) -> bool  # async
Database.update_event(event_id: int, **fields: Any) -> bool  # async
Database.fuzzy_match_event(title: str) -> dict[str, Any] | None  # async
Database.add_conversation(user_message: str, bot_response: str) -> int  # async; writes canonical messages
Database.get_recent_conversations(limit: int = 5) -> list[dict[str, Any]]  # async; reads canonical messages

get_current_time(timezone: str | None = None) -> datetime
format_datetime_for_display(dt: datetime | str | None) -> str
format_datetime_iso(dt: datetime) -> str
parse_iso_datetime(iso_str: str) -> datetime | None
get_day_range(date: datetime | None = None) -> tuple[datetime, datetime]
get_week_range(date: datetime | None = None) -> tuple[datetime, datetime]
format_task_for_prompt(task: dict[str, Any]) -> str
format_event_for_prompt(event: dict[str, Any]) -> str
format_tasks_list(tasks: list[dict[str, Any]]) -> str
format_events_list(events: list[dict[str, Any]]) -> str

ClaudeAgent(api_key: str | None = None)
ClaudeAgent.build_system_prompt(current_time: str | None = None, tasks: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None, gcal_events: list[dict[str, Any]] | None = None) -> str
ClaudeAgent.process_message(user_message: str, tasks: list[dict[str, Any]] | None = None, events: list[dict[str, Any]] | None = None, gcal_events: list[dict[str, Any]] | None = None) -> AgentResponse
create_agent() -> ClaudeAgent  # async legacy factory
```
