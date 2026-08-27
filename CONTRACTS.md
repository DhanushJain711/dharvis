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
DecisionAction = Literal["scheduled", "moved", "unscheduled", "shortened", "extended"]
Trigger = Literal["daily_plan", "conflict", "user_request", "deadline_shift", "goal_quota"]
ProgressSource = Literal["task", "manual", "inferred"]
MessageRole = Literal["user", "assistant", "tool"]
ScheduleSource = Literal["gcal", "event", "task"]
ReasoningVerbosity = Literal["brief", "full"]
```

## Configuration (`src.config`)

```python
Config.TELEGRAM_BOT_TOKEN: str
Config.ALLOWED_USER_ID: int | None
Config.TELEGRAM_POLL_TIMEOUT_SECONDS: int
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

Production startup requires both `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_ID` and fails closed when either is absent. Clock settings are local 24-hour `HH:MM` strings. OAuth token material created or refreshed by the app is mode `0600`.

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
update_event         complete_task          delete_task
delete_event         query_schedule         query_tasks
find_free_blocks     schedule_task          explain_schedule
add_fact             update_fact            query_facts
add_goal             log_goal_progress      query_goals
resolve_date
```

`schedule_task.reasoning` is required and is a one-sentence explanation recorded at decision time. Its handler must call `Store.apply_schedule_decision` so placement and rationale commit atomically. `add_task` and `add_event` accept arrays. Strict update schemas require every field: `null` means unchanged, while nullable values are cleared only through the required `clear_fields` array (`[]` means clear nothing).

## Schema migration (`src.migrate`)

```python
run_migrations(db_path: str | Path | None = None) -> None  # async
main() -> None
```

`src/schema.sql` is the only canonical schema file. Later agents must not add migration files.
The migration runner alone accepts naive timestamps from the retired database and interprets them in `USER_TIMEZONE` before converting to UTC. Runtime APIs reject naive datetimes; this recovery policy must not be copied into new writes.

## Store (`src.store`)

```python
Store(db_path: str | Path | None = None)
Store.initialize() -> None  # async
Store.connection() -> AsyncIterator[aiosqlite.Connection]  # @asynccontextmanager
Store.add_tasks(tasks: list[Record]) -> list[Record]  # async
Store.add_events(events: list[Record]) -> list[Record]  # async
Store.get_task(task_id: int) -> Record | None  # async
Store.get_event(event_id: int) -> Record | None  # async
Store.update_task(task_id: int, changes: Record) -> Record  # async
Store.update_event(event_id: int, changes: Record) -> Record  # async
Store.complete_task(task_id: int, actual_minutes: int | None = None) -> Record  # async
Store.drop_task(task_id: int) -> Record  # async
Store.delete_task(task_id: int) -> Record  # async; drops without erasing history
Store.delete_event(event_id: int) -> bool  # async
Store.query_tasks(status: TaskStatus | None = None, category: str | None = None, due_before: datetime | None = None, due_after: datetime | None = None) -> list[Record]  # async
Store.query_events(start: datetime, end: datetime) -> list[Record]  # async
Store.apply_schedule_decision(task_id: int, action: DecisionAction, start: datetime, end: datetime, previous_start: datetime | None, previous_end: datetime | None, trigger: Trigger, reasoning: str, facts_used: list[int], gcal_event_id: str | None) -> Record  # async, one DB transaction
Store.get_schedule_decisions(task_id: int) -> list[Record]  # async
Store.mark_decision_surfaced(decision_id: int) -> None  # async
Store.add_fact(fact: Record) -> Record  # async
Store.update_fact(fact_id: int, changes: Record) -> Record  # async
Store.query_facts(category: str | None = None, active: bool | None = True, min_confidence: float | None = None) -> list[Record]  # async
Store.add_goal(goal: Record) -> Record  # async
Store.log_goal_progress(goal_id: int, amount: float, source: ProgressSource, logged_at: datetime) -> Record  # async
Store.query_goals(active: bool | None = True, category: str | None = None) -> list[Record]  # async
Store.append_message(role: MessageRole, content: str, tool_calls: list[Record], session_id: str) -> Record  # async
Store.get_messages(session_id: str, limit: int = 100) -> list[Record]  # async
Store.get_daily_log(local_date: date) -> Record | None  # async
Store.upsert_daily_log(local_date: date, changes: Record) -> Record  # async
Store.record_usage(component: Literal["agent_loop", "session_summary", "scheduler", "facts"], model: str, usage: Record, estimated_cost_usd: float | None, session_id: str | None = None) -> None  # async
Store.usage_summary(start: datetime, end: datetime) -> list[Record]  # async
create_store(db_path: str | Path | None = None) -> Store  # async
```

Every Store connection enables `PRAGMA foreign_keys = ON`. `apply_schedule_decision` is the only placement mutation contract: it updates the task placement and inserts the nonblank `schedule_decisions` row in one transaction. `facts_used` accepts only existing integer fact IDs.

## Calendar (`src.calendar_service`)

```python
CalendarError(RuntimeError)
CalendarReconnectRequiredError(CalendarError)
CalendarReconnectRequired = CalendarReconnectRequiredError
CalendarService(credentials_path: Path | None = None, token_path: Path | None = None, calendar_id: str | None = None)
CalendarService.is_available() -> bool
CalendarService.list_events(start: datetime, end: datetime) -> list[CalendarRecord]  # async
CalendarService.get_events_between(start: datetime, end: datetime) -> list[CalendarRecord]  # async
CalendarService.get_today_events() -> list[CalendarRecord]  # async
CalendarService.get_upcoming_events(days: int = 7) -> list[CalendarRecord]  # async
CalendarService.check_availability(start: datetime, end: datetime) -> bool  # async
CalendarService.create_event(event: CalendarRecord, reasoning: str | None = None) -> CalendarRecord  # async
CalendarService.update_event(gcal_event_id: str, changes: CalendarRecord) -> CalendarRecord  # async
CalendarService.delete_event(gcal_event_id: str) -> None  # async
CalendarService.clear_kalendra_range(start: datetime, end: datetime) -> None  # async
CalendarService.create_work_block(task_id: int, title: str, start: datetime, end: datetime, reasoning: str | None = None) -> str  # async
CalendarService.update_work_block(gcal_event_id: str, title: str, start: datetime, end: datetime, reasoning: str | None = None) -> None  # async
CalendarService.delete_work_block(gcal_event_id: str) -> None  # async
run_oauth_flow(credentials_path: Path | None = None, token_path: Path | None = None) -> bool
create_calendar_service() -> CalendarService  # async
```

OAuth uses the read/write Calendar scope. Reads cover every visible calendar and cache complete results in memory for 60 seconds. Writes are restricted to marked, application-owned events on a dedicated secondary calendar named `Kalendra`; its ID is persisted beside the OAuth token, and primary-calendar events are never mutated. Creating a block requires a nonblank rationale, supplied as `reasoning` or `event["reasoning"]`, which is rendered in the Google event description. `clear_kalendra_range` deletes only marked Kalendra blocks. Credential refresh is transparent; absent, invalid, rejected, or unrefreshable credentials raise `CalendarReconnectRequiredError` so callers can request reconnection.

Every returned `start_time` and `end_time` is UTC-aware ISO-8601 text. Google `dateTime` offsets are converted to UTC; all-day `date` values become the UTC instants for local midnight boundaries. Event end times are required and later than starts.

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
query_schedule(store: Store, calendar: CalendarService, start: datetime, end: datetime) -> list[ScheduleBlock]  # async
merge_blocks(blocks: list[ScheduleBlock]) -> list[ScheduleBlock]
find_free_blocks(store: Store, calendar: CalendarService, start: datetime, end: datetime, min_minutes: int) -> list[FreeBlock]  # async
has_conflict(blocks: list[ScheduleBlock], start: datetime, end: datetime) -> bool
```

`compute_free_blocks` is deterministic and accepts mapping- or object-style constraints. Busy values may be supplied as `busy_intervals`, `busy_blocks`, or `busy`; waking and quiet hours accept clock pairs or `{start, end}` mappings; `timezone` selects the IANA zone; and `buffer_minutes` defaults to 15. It merges overlapping buffered busy intervals, respects cross-midnight waking and quiet hours, interprets all-day boundaries at local midnight, and constructs each local day independently so DST transitions retain their real elapsed length. Free blocks identify their adjacent blockers through `after` and `before` (also exposed by the compatibility properties `after_title` and `before_title`).

## Agent and history (`src.agent`, `src.history`)

```python
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

## Telegram (`src.telegram_handler`)

```python
TelegramHandler(agent: Agent | Any | None = None, *, store: Any | None = None, database: Any | None = None, claude_agent: Any | None = None, calendar_service: Any | None = None)
TelegramHandler.is_authorized(user_id: int) -> bool
TelegramHandler.start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.cost_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None  # async
TelegramHandler.create_application(token: str | None = None) -> Application
create_telegram_handler(agent: Agent, store: Any | None = None) -> TelegramHandler
```

## Scheduler (`src.scheduler_engine`)

```python
ScheduleDecision(task_id: int, action: DecisionAction, start: datetime, end: datetime, previous_start: datetime | None, previous_end: datetime | None, trigger: Trigger, reasoning: str, facts_used: list[int])
SchedulerEngine(store: Store, calendar: CalendarService)
SchedulerEngine.choose_slot(task_id: int, candidates: list[FreeBlock], trigger: Trigger) -> ScheduleDecision  # async
SchedulerEngine.schedule_task(task_id: int, start: datetime, end: datetime, reasoning: str, trigger: Trigger, facts_used: list[int] | None = None) -> ScheduleDecision  # async
SchedulerEngine.plan_day(local_date: date) -> list[ScheduleDecision]  # async
SchedulerEngine.build_daily_plan(local_date: date) -> list[ScheduleDecision]  # async
SchedulerEngine.reschedule(reason: str, affected_range: Any, *, trigger: Trigger = "conflict") -> list[ScheduleDecision]  # async
SchedulerEngine.detect_conflicts(start: datetime | None = None, end: datetime | None = None) -> list[ScheduleDecision]  # async
SchedulerEngine.format_change_summary(decisions: Sequence[Any], *, mark_surfaced: bool = True) -> str  # async
SchedulerEngine.resolve_conflicts(start: datetime, end: datetime) -> list[ScheduleDecision]  # async
SchedulerEngine.explain_schedule(task_id: int) -> list[dict[str, object]]  # async
create_scheduler_engine(store: Store, calendar: CalendarService) -> SchedulerEngine  # async
```

## Proactive jobs (`src.jobs`)

```python
send_daily_brief(store: Store, telegram: Any, local_date: date) -> None  # async
send_daily_debrief(store: Store, telegram: Any, local_date: date) -> None  # async
run_daily_planning(engine: SchedulerEngine, local_date: date) -> None  # async
reconcile_calendar(engine: SchedulerEngine) -> None  # async
configure_jobs(scheduler: Any, store: Store, engine: SchedulerEngine, telegram: Any) -> None
create_job_scheduler() -> Any
start_job_scheduler(scheduler: Any, *, catch_up: bool = True) -> Any
shutdown_job_scheduler(scheduler: Any, *, wait: bool = True) -> Any
run_startup_catchup() -> None  # async
```

## Facts (`src.facts_engine`)

```python
FactsEngine(store: Store)
FactsEngine.extract_facts(user_message: str, assistant_message: str) -> list[Fact]  # async
FactsEngine.consolidate(candidates: list[Fact]) -> list[Fact]  # async
FactsEngine.relevant_facts(context: str, category: str | None = None, limit: int = 20) -> list[Fact]  # async
FactsEngine.seed_facts(facts: list[Fact]) -> list[Fact]  # async
FactsEngine.confirm_fact(fact_id: int, confidence: float = 1.0) -> Fact  # async
FactsEngine.deactivate_fact(fact_id: int) -> Fact  # async
create_facts_engine(store: Store) -> FactsEngine  # async
```

## Runtime integration (`src.integration`)

```python
jsonable(value: Any) -> Any
build_tool_handlers(store: Store, calendar: CalendarService, scheduler: SchedulerEngine, facts_engine: FactsEngine) -> dict[str, ToolHandler]  # async
```

The returned registry contains exactly every name in `TOOLS_BY_NAME`. Tool
results contain only JSON-compatible values and never naive datetimes.

## Entrypoint (`src.main`)

```python
build_application() -> tuple[TelegramHandler, object]  # async
main(*, check_only: bool = False) -> None  # async
run() -> None
```

Run with `python -m src.main`; `python -m src.main --check` builds the configured schema and Telegram application, then exits before network polling. The check still requires explicit safe values for both `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_ID`; either missing value makes the CLI exit with status 2.

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
