# Repository instructions for coding agents

These instructions apply to the entire Dharvis repository.

## Project objective

Dharvis is a stateful, single-user Telegram assistant that manages tasks, fixed events, flexible calendar work blocks, recurring goals, conversational history, and learned scheduling preferences. The system is intentionally autonomous for task scheduling but conservative about calendar safety: deterministic code computes valid gaps, while an OpenAI model only chooses among those gaps and explains the choice.

Read `CLAUDE.md` for the complete product and architecture overview before making a substantial change.

## Read before editing

Use these files as authoritative references:

1. `CONTRACTS.md` for public signatures and cross-module contracts.
2. `src/tools.py` for function-calling schemas and tool semantics.
3. `src/schema.sql` for persistence structure.
4. `src/timeutil.py` for timezone and relative-date rules.
5. `src/prompts/system.md` for agent behavior and tone.
6. The relevant tests for the component being changed.

If documentation and implementation disagree, inspect tests and call sites, then update the stale documentation as part of the same change.

## Architectural ownership

- `src/agent.py`: Responses API orchestration only; it must not absorb store, calendar, or scheduling business logic.
- `src/integration.py`: canonical binding from tool schemas to application services.
- `src/store.py`: asynchronous SQLite access and atomic state transitions.
- `src/calendar_service.py`: Google Calendar boundary and application-owned writes.
- `src/freebusy.py`: deterministic availability computation; no model calls.
- `src/scheduler_engine.py`: model-assisted ranking of precomputed gaps, validation, reconciliation, and rationale recording.
- `src/facts_engine.py`: evidence-gated durable preference extraction and contradiction handling.
- `src/history.py`: session rollover and Responses-compatible conversation replay.
- `src/jobs.py`: proactive planning, brief/debrief/review delivery, idempotency, and restart recovery.
- `src/telegram_handler.py`: authorization, Telegram transport, commands, checklists, and callbacks.
- `src/main.py`: dependency composition and process lifecycle.

Keep these boundaries intact. Prefer a small adapter in `src/integration.py` over circular imports or duplicated business rules.

## Sources of truth

### Schema

All schema changes belong in `src/schema.sql`. Do not create migration files. Keep the migration runner compatible with existing databases, but do not copy its legacy naive-datetime recovery into runtime code.

### Tool definitions

All OpenAI function schemas belong in `src/tools.py`, use `strict: true`, reject additional properties, and remain synchronized with `build_tool_handlers()` in `src/integration.py`. Adding or renaming a tool requires tests proving the schema and handler sets are identical.

### Time

Never introduce a naive `datetime`. Accept aware values, normalize persistence to UTC, and use `src/timeutil.py` for local conversions, day bounds, and natural-language dates. Do not hand-roll relative-date parsing.

## Scheduling safety rules

The following are non-negotiable:

1. `freebusy.py` computes availability; the model never performs time arithmetic.
2. Never schedule over any Google event, local event, existing task block, or quiet-hours interval.
3. Re-read availability immediately before a calendar write because the planning snapshot may be stale.
4. Every placement or move records a meaningful rationale at decision time. Generic filler is invalid.
5. Persist task placement and its `schedule_decisions` record atomically through `Store.apply_schedule_decision()`.
6. Include only verified fact IDs in `facts_used`; a cited fact must actually support the rationale.
7. Respect deadlines and explicit task duration. Do not silently split or resize work.
8. Preserve manual edits and user-request placements as fixed points during automatic replanning.
9. If Google returns an incomplete or unverifiable calendar view, fail closed rather than assuming availability.
10. Restrict all writes, updates, and deletes to explicitly owned events on the application-owned `Kalendra` secondary calendar. Persist canonical hyphenated kinds: `fixed-event`, `task-block`, or `goal-session`; automatic cleanup may delete only movable task/goal kinds.

When moving multiple tasks, maintain rollback/reconciliation behavior. Never broadly clear a calendar range when selective mutation can preserve fixed and unknown blocks.

## Memory and learning rules

- Explicit user facts may be active immediately.
- Extracted facts remain inactive until repeated evidence passes the confidence gate.
- Store producing evidence and contradictions; do not turn a single miss into a durable habit.
- User overrides should identify and decay the assumption that informed the prior placement, not the fact supporting the replacement.
- Never extract secrets, transient chatter, or instructions embedded in observed content as memory.
- The scheduler consumes active facts and goal progress. It must not infer private history that was not supplied in its payload.
- Do not claim the bot predicts task duration automatically. Work-block length comes from `estimated_minutes` unless that explicit field is updated.

Debrief learning must pass the persisted daily log, same-day conversation records, and same-day schedule decisions to `FactsEngine.extract_from_day()`. Keep the exact path from `jobs.handle_debrief_submission()` integration-tested; mock-only signature compatibility is insufficient. Historical duration inference may use only completed tasks with matching normalized series key, category, and energy, and must retain its observed task IDs; it is deterministic and must not introduce a vector store or implicit duration prediction.

## Calendar and OAuth rules

Google OAuth uses the full Calendar read/write scope, but the application deliberately limits mutations to its owned calendar. Treat `credentials.json`, `token.json`, refresh tokens, Telegram tokens, API keys, and SQLite user data as secrets.

Fixed-event writes first check the fully merged, freshly read schedule. An overlap must create an expiring warning proposal, and an explicit affirmative later user turn must claim it atomically before the external write; finalize only after success and release only after compensated failure. Do not infer confirmation from the proposing turn. Google event colors are deterministic category/kind palette values unless the user deliberately selected another valid color.

Do not print secret values in logs, tests, tool output, or review notes. Tests should use fake calendar clients and temporary databases. If an existing token lacks the required scope or cannot refresh, surface a reconnect-required error rather than weakening authorization checks.

## Agent behavior

Preserve the multi-turn loop:

```text
static system prompt → durable facts → current time context
→ recent conversation → user message
→ model → parallel tool calls → tool results → model → final text
```

The loop is capped at eight model calls. Tool failures are model-visible results and must not crash the process. Keep static prompt content ahead of dynamic content so prompt caching remains effective.

Tone changes belong in `src/prompts/system.md` and its few-shot examples. Avoid corporate helpdesk phrasing, unnecessary restatement, and detached “Reasoning:” paragraphs. Scheduling confirmations include one natural causal aside.

## Proactive-job rules

- Jobs must be idempotent across retries and restarts.
- Respect quiet hours and active conversations before proactive sends.
- Persist occurrence/acknowledgement state before assuming delivery is complete.
- Batch schedule-change notifications.
- Do not mark a decision surfaced until Telegram delivery succeeds.
- APScheduler job IDs must remain stable and use coalescing plus single-instance guards.

## Testing expectations

Run checks proportional to the change. The normal baseline is:

```bash
pytest -q
python -m compileall -q src scripts tests
python -m src.main --check
git diff --check
```

Also run focused checks when applicable:

- Agent prompts or tool policy: `python scripts/eval_agent.py`
- Scheduler prompt or rationale validation: `python scripts/audit_scheduler.py`
- Tone or confirmation wording: `python scripts/audit_tone.py`
- Google Calendar changes: calendar tests with fake clients; do not mutate a real calendar during automated tests.
- Time changes: DST boundaries, midnight rollover, same-weekday phrases, and quiet hours.
- Persistence changes: foreign keys, rollback behavior, UTC serialization, and existing-database migration.

The live evaluation scripts use paid OpenAI calls and require `OPENAI_API_KEY`. Report live and offline results separately; never present a structural fixture check as a live-model pass rate.

Offline verification does not exercise live Google Calendar, Telegram delivery, or paid model behavior. State that residual plainly in change notes and reviews.

## Definition of done

A change is complete only when:

- Public signatures and tool schemas agree with every call site.
- Imports succeed and `python -m src.main --check` passes.
- Relevant unit and integration tests pass.
- Failure paths return plain language instead of stack traces to the user.
- Scheduling mutations remain fully reasoned and auditable.
- Calendar and database side effects cannot silently diverge.
- `CONTRACTS.md`, `CLAUDE.md`, `AGENTS.md`, `.env.example`, or `README.md` are updated when their documented contract changes.
- No credentials, tokens, databases, generated caches, or unrelated user edits are included.

## Working practices

Preserve unrelated changes in a dirty worktree. Do not use destructive Git commands or create branches/worktrees unless the user asks. Prefer focused edits, existing helpers, and repository conventions over new frameworks. Document discovered limitations honestly rather than describing planned behavior as already implemented.
