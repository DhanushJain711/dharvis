# Agent-core integration needs

- `src.main` must construct the complete tool-handler registry and inject it into `Agent`; agent-core intentionally contains no store, calendar, free/busy, scheduling, facts, or goal business logic.
- `src.config` should default `AGENT_MODEL_ID` to `gpt-5.6-terra`, add `SUMMARY_MODEL_ID` defaulting to `gpt-5.6-luna`, and document both. Agent-core reads these environment variables directly until that lands.
- `pyproject.toml` package data currently includes only `schema.sql`; add `prompts/system.md` so wheel installs do not fall back to agent-core's minimal emergency prompt.
- `Store` needs explicit conversation/session APIs equivalent to `History.latest_session`, `resolve_session`, and atomic message-window lookup/append. `History` temporarily uses `Store.connection()` directly. Add the new session-facing signatures to `CONTRACTS.md` when the data-layer contract is updated.
- The canonical `schedule_task` schema has no `facts_used` argument, while `Store.apply_schedule_decision` requires `facts_used`. The scheduling handler needs an agreed source for the exact fact ids used at decision time; adding them after the decision would defeat the audit trail.
- `REASONING_VERBOSITY=full` semantics need cross-branch policy reconciliation. Agent-core intentionally preserves the newer product requirement that every schedule/move confirmation carries exactly one conversational rationale clause, rather than expanding it into a separate explanation.
- The in-process per-conversation lock prevents overlapping turns only inside one `Agent` instance. Production needs cross-process locking plus tool-call idempotency keys before multiple bot workers can safely execute external side effects.
