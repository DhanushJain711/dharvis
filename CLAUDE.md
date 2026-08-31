# Dharvis project context

Dharvis is a stateful Telegram personal assistant for one authorized user. It turns natural-language messages into tasks, one-time reminders, fixed calendar events, flexible work blocks, recurring goals, durable facts, and proactive daily planning. OpenAI models decide which typed tool to call and, for flexible tasks, which precomputed free block is the best fit. Deterministic Python owns time arithmetic, validation, persistence, and calendar safety.

## Mental model

- An **event** is a fixed-time commitment such as a meeting or appointment.
- A **reminder** is a quick, one-time Telegram nudge at a requested instant. It is SQLite-only and never reserves calendar time.
- A **task** is flexible work with a deadline, estimated duration, category, energy mode, and priority.
- A **work block** is a task's scheduled interval on the dedicated Google Calendar named `Kalendra`.
- A **schedule decision** is the immutable audit record explaining why a task was placed, moved, shortened, extended, or left unscheduled.
- A **fact** is a durable preference or observed behavior, such as “deep work usually happens before lunch.”
- A **goal** is a weekly or monthly target measured in hours or sessions. Tasks can be linked to goals.

The bot reads all visible Google calendars for availability and briefs, but it writes only marked events on its application-owned `Kalendra` secondary calendar. It never edits arbitrary events on the primary calendar.

## Runtime flow

The Telegram application composition root is `src/telegram_handler.py`. Polling
production uses `src/main.py`; webhook production uses FastAPI in `src/web.py`:

1. Initialize the canonical SQLite schema through `Store`.
2. Construct `CalendarService`, `SchedulerEngine`, and `FactsEngine`.
3. Bind every schema in `src/tools.py` to a real handler in `src/integration.py`.
4. Construct the stateful `Agent` and `TelegramHandler`.
5. Register persistent APScheduler jobs for reminder delivery, planning, briefs, debriefs, weekly reviews, and conflict reconciliation.
6. Start Telegram polling plus its dependency-aware `/healthz` server, or the
   ASGI webhook lifespan which registers Telegram's webhook and owns scheduler
   startup and clean shutdown.

For each Telegram message, `Agent.run_tool_loop()` loads active facts, local time context, and the last 20 messages from the current session. It calls the OpenAI Responses API, executes all returned tool calls concurrently, appends tool results, and repeats until the model returns text or reaches the eight-call limit. Tool failures are returned to the model as data so it can recover without exposing a stack trace.

Sessions roll over after four hours of silence. The previous session is summarized with the inexpensive summary model and carried into the next session. Messages, assistant output items, and tool results are persisted in SQLite.

## Scheduling model

Scheduling deliberately separates deterministic safety from model judgment:

1. `src/freebusy.py` merges Google events, bot-created local events, and existing task blocks.
2. It applies quiet hours, a 15-minute buffer around busy intervals, timezone/DST rules, and the requested minimum duration.
3. It returns concrete `FreeBlock` values annotated with the events or boundaries before and after each gap.
4. `SchedulerEngine` gives the model only those named blocks plus schedulable tasks, active facts, and active goal progress.
5. The model ranks assignments using deadline urgency, duration fit, task energy, priority, learned habits, and goal quotas.
6. Python rejects invented blocks, overlap, post-deadline work, quiet-hour work, false fact citations, and generic rationales. One corrected model attempt is allowed.
7. Immediately before writing, availability is fetched again without using the short-lived Google cache. The accepted work block is written to Google Calendar and its task placement plus rationale are committed atomically in SQLite.

Every accepted placement must explain a real constraint. “This is a good time” is invalid; “the 90-minute task fits the only two-hour gap before Friday's deadline” is valid. The full decision chain powers `/why` and prevents later reconstruction from stale conditions.

Fixed-event changes are checked against the merged schedule before any write. A conflict produces an expiring proposal and warning; only an explicit affirmative confirmation in a later user message may apply it. The proposal is atomically claimed before the external write and finalized only on success, preventing duplicate confirmations. New fixed events are never moved automatically.

Conflict reconciliation runs every 15 minutes and after bot-created events change. It selectively replans overlapping automatic task blocks while preserving user-confirmed and manually dragged blocks as fixed points. It also reconciles hand-moved owned blocks, removes owned orphan task/goal blocks when safe, and leaves a durable repair signal for failed remote repairs. Changes are batched into one Telegram message and unsurfaced changes can be folded into the next morning brief.

## Memory and learning

Memory has several layers:

- `messages`: recent conversational context and tool exchanges.
- `tasks`, `events`, `goals`, and `goal_progress`: explicit structured state.
- `reminders`: one-time Telegram delivery state, attempts, and durable leases; it is not calendar state.
- `schedule_decisions`: placement history, causal reasoning, and cited fact IDs.
- `daily_log`: planned versus completed work, brief/debrief markers, and retry state.
- `facts`: durable natural-language preferences with confidence, evidence count, source, and active state.
- `facts_engine_evidence`: the observations, confirmations, and contradictions behind each learned fact.

Explicit and seeded facts are trusted immediately. Extracted behavioral facts start inactive and require three supporting observations before they can influence scheduling. Contradictory evidence lowers confidence and can deactivate a fact. Manual calendar moves are recorded as user-request decisions and emitted as structured learning signals. Historical task duration reuse is deterministic: matching completed tasks must share a normalized task family (`series_key`), category, and energy; recent recorded actual minutes are robustly medianed, with evidence task IDs retained. An explicit estimate always wins, and no vector database or duration-prediction model is used.

The evening debrief records completed tasks and actual minutes, removes the owned work block before clearing its local ID, and updates linked goal progress idempotently. It gathers the local day's persisted messages and schedule decisions, then calls `FactsEngine.extract_from_day(daily_log, conversation, decisions)` with that complete evidence bundle. A later debrief follow-up answer is retained as additional day-scoped evidence without replaying completion or goal progress. A notably missed plan or at least 60 minutes of unexpected work triggers one short follow-up question.

## Proactive jobs

- Daily planning runs 15 minutes before the configured morning brief.
- A durable dispatcher checks every 30 seconds for due reminders. It claims records with short leases, acknowledges only after Telegram accepts the message, and retries failures with bounded exponential backoff. Startup catch-up delivers reminders missed while the process was down.
- The morning brief shows local commitments first, then nonduplicated external Google events, tasks due today, scheduled work blocks with reasons, behind-pace goals, pending reminders overdue through the next two local days, and unsurfaced schedule changes. Reading reminders for the brief does not mark them delivered or suppress the later due-time text. It retries rather than silently omit events when Google returns an incomplete view.
- The evening debrief sends a checklist for scheduled or due tasks and records actual completion.
- The Sunday review summarizes completion, goal progress, and the strongest learned behavioral pattern.
- Calendar reconciliation checks the configured lookahead window every 15 minutes.

Jobs use the user's IANA timezone, respect quiet hours, coalesce missed executions, and persist their job store and daily occurrence markers so restarts do not normally duplicate messages. An explicitly timed reminder is the exception to proactive-message deferral: it is sent during quiet hours or an active conversation because the user requested that exact instant. Reminder delivery is at least once; a crash after Telegram accepts a message but before SQLite acknowledgement can rarely produce a duplicate, which is preferable to silently losing the reminder.

The scheduler also creates a SQLite backup at 03:00 local time under
`DATA_DIR/backups` and retains dated backups for 14 days. Its nightly facts
fallback extracts the persisted day's evidence if a debrief was not submitted.

## Deployment modes

`RUN_MODE=polling` is the default and runs `python -m src.main` (including the
existing Railway deployment). `RUN_MODE=webhook` is for an HTTPS ASGI host such
as Azure App Service and runs `uvicorn src.web:app --host 0.0.0.0 --port 8000`.
Webhook mode requires `PUBLIC_BASE_URL`, `TELEGRAM_WEBHOOK_PATH`, and
`TELEGRAM_WEBHOOK_SECRET`; generate the two token values with
`python scripts/gen_webhook_secrets.py` and keep them out of source control.

`DATA_DIR` defaults to `./data` and is the single persistent root for SQLite,
the job store, OAuth credentials/token, and backups unless an explicit path
override is intentionally configured. Azure App Service uses `DATA_DIR=/home/data`.

## Important current boundaries

- Autonomous scheduling applies to flexible tasks, not fixed events. The conversational agent can suggest or create event times after querying availability, but events do not have their own importance score or optimization engine.
- Exam and interview events do not automatically generate study or preparation tasks. That would require a separate, deliberately designed event-to-prep workflow; today the user must ask for prep work explicitly.
- Event-to-event conflicts are not automatically resolved. Fixed events remain fixed; only overlapping task work blocks are replanned.
- Reminders remain separate from tasks, events, Google Calendar, free/busy, and the scheduler. Their only side effect is the requested Telegram delivery.
- Scheduling-enabled goals materialize idempotent task-backed sessions for their outstanding weekly or monthly quota. Sessions are paced across remaining days, missed automatic sessions are rescheduled, and manual goal-session placements are preserved.
- Calendar ownership is explicit (`kalendra_owned=v1`) and event kind is canonical hyphenated metadata: `fixed-event`, `task-block`, or `goal-session`. Deterministic category/kind Google colors distinguish these entries while preserving a user-selected nondefault color.
- The scheduler sees durable facts and goal progress, not raw chat history. Interactive agent behavior sees recent conversation history separately.

## Sources of truth

- `src/schema.sql`: the only canonical database schema.
- `src/tools.py`: the only canonical OpenAI tool schemas.
- `CONTRACTS.md`: public Python signatures and cross-module contracts.
- `src/timeutil.py`: all timezone conversion and relative-date behavior.
- `src/prompts/system.md`: assistant behavior, tool-use policy, and tone examples.
- `src/config.py` and `.env.example`: supported environment variables.

Do not add independent migration files or duplicate tool definitions elsewhere.

## Development commands

```bash
python -m src.main --check
pytest -q
python -m compileall -q src scripts tests
python scripts/chat_repl.py
```

The following use real OpenAI calls and write reports under `evals/`:

```bash
python scripts/eval_agent.py
python scripts/audit_scheduler.py
python scripts/audit_tone.py
```

Google Calendar authorization is created locally with:

```bash
python scripts/setup_gcal_auth.py
```

Never commit `.env`, `credentials.json`, `token.json`, SQLite databases, or other secret material.

## Non-negotiable invariants

- Runtime datetimes are timezone-aware; persisted timestamps are UTC.
- Local-day intervals are half-open: `[start, next_day_start)`.
- The model never performs free/busy arithmetic or invents a calendar gap.
- No schedule mutation exists without contemporaneous, nonblank reasoning.
- Calendar reads fail closed when Google returns an incomplete view.
- Writes stay inside the marked `Kalendra` calendar.
- Manual user corrections are fixed points and learning signals.
- External side effects and SQLite changes must have rollback or reconciliation behavior.
- Offline checks do not exercise live Google Calendar, Telegram delivery, or paid OpenAI model integrations; report those separately from fake-client and structural test results.
- The repository must continue to import cleanly and `python -m src.main --check` must succeed.
