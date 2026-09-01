PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    target_amount REAL NOT NULL CHECK (target_amount > 0),
    target_unit TEXT NOT NULL CHECK (target_unit IN ('hours', 'sessions')),
    period TEXT NOT NULL CHECK (period IN ('week', 'month')),
    category TEXT NOT NULL,
    session_minutes INTEGER NOT NULL DEFAULT 60 CHECK (session_minutes > 0),
    scheduling_enabled INTEGER NOT NULL DEFAULT 1 CHECK (scheduling_enabled IN (0, 1)),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    deadline TEXT CHECK (
        deadline IS NULL OR substr(deadline, -1) = 'Z' OR substr(deadline, -6) = '+00:00'
    ),
    estimated_minutes INTEGER CHECK (estimated_minutes IS NULL OR estimated_minutes > 0),
    category TEXT NOT NULL DEFAULT 'personal'
        CHECK (category IN ('school', 'work', 'personal', 'fitness', 'career', 'errand')),
    energy TEXT NOT NULL DEFAULT 'light'
        CHECK (energy IN ('deep_focus', 'light', 'errand')),
    priority TEXT NOT NULL DEFAULT 'medium'
        CHECK (priority IN ('low', 'medium', 'high')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'scheduled', 'completed', 'dropped')),
    scheduled_start TEXT CHECK (
        scheduled_start IS NULL OR substr(scheduled_start, -1) = 'Z'
        OR substr(scheduled_start, -6) = '+00:00'
    ),
    scheduled_end TEXT CHECK (
        scheduled_end IS NULL OR substr(scheduled_end, -1) = 'Z'
        OR substr(scheduled_end, -6) = '+00:00'
    ),
    gcal_event_id TEXT,
    goal_id INTEGER REFERENCES goals(id) ON DELETE SET NULL,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    completed_at TEXT CHECK (
        completed_at IS NULL OR substr(completed_at, -1) = 'Z'
        OR substr(completed_at, -6) = '+00:00'
    ),
    actual_minutes INTEGER CHECK (actual_minutes IS NULL OR actual_minutes >= 0),
    series_key TEXT,
    estimate_source TEXT CHECK (
        estimate_source IS NULL OR estimate_source IN ('user', 'history', 'default', 'goal')
    ),
    actual_minutes_source TEXT CHECK (
        actual_minutes_source IS NULL
        OR actual_minutes_source IN ('user', 'debrief', 'calendar', 'inferred')
    ),
    CHECK (
        (scheduled_start IS NULL AND scheduled_end IS NULL)
        OR (scheduled_start IS NOT NULL AND scheduled_end IS NOT NULL
            AND scheduled_end > scheduled_start)
    ),
    CHECK (deadline IS NULL OR (julianday(deadline) IS NOT NULL AND instr(deadline, 'T') = 11)),
    CHECK (scheduled_start IS NULL OR (julianday(scheduled_start) IS NOT NULL AND instr(scheduled_start, 'T') = 11)),
    CHECK (scheduled_end IS NULL OR (julianday(scheduled_end) IS NOT NULL AND instr(scheduled_end, 'T') = 11)),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11),
    CHECK (completed_at IS NULL OR (julianday(completed_at) IS NOT NULL AND instr(completed_at, 'T') = 11))
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT,
    start_time TEXT NOT NULL
        CHECK (substr(start_time, -1) = 'Z' OR substr(start_time, -6) = '+00:00'),
    end_time TEXT NOT NULL
        CHECK (substr(end_time, -1) = 'Z' OR substr(end_time, -6) = '+00:00'),
    location TEXT,
    category TEXT,
    source TEXT NOT NULL DEFAULT 'bot' CHECK (source IN ('bot', 'gcal')),
    gcal_event_id TEXT,
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    color_id TEXT CHECK (
        color_id IS NULL OR color_id IN ('1', '2', '3', '4', '5', '6', '7', '8', '9', '10', '11')
    ),
    CHECK (end_time > start_time),
    CHECK (julianday(start_time) IS NOT NULL AND instr(start_time, 'T') = 11),
    CHECK (julianday(end_time) IS NOT NULL AND instr(end_time, 'T') = 11),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS schedule_decisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id) ON DELETE RESTRICT,
    decided_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(decided_at, -1) = 'Z' OR substr(decided_at, -6) = '+00:00'),
    action TEXT NOT NULL
        CHECK (action IN ('scheduled', 'moved', 'unscheduled', 'shortened', 'extended')),
    "start" TEXT NOT NULL
        CHECK (substr("start", -1) = 'Z' OR substr("start", -6) = '+00:00'),
    "end" TEXT NOT NULL
        CHECK (substr("end", -1) = 'Z' OR substr("end", -6) = '+00:00'),
    previous_start TEXT CHECK (
        previous_start IS NULL OR substr(previous_start, -1) = 'Z'
        OR substr(previous_start, -6) = '+00:00'
    ),
    previous_end TEXT CHECK (
        previous_end IS NULL OR substr(previous_end, -1) = 'Z'
        OR substr(previous_end, -6) = '+00:00'
    ),
    trigger TEXT NOT NULL CHECK (
        trigger IN ('daily_plan', 'conflict', 'user_request', 'deadline_shift', 'goal_quota')
    ),
    reasoning TEXT NOT NULL CHECK (length(reasoning) > 0),
    facts_used TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(facts_used) AND json_type(facts_used) = 'array'),
    surfaced_to_user INTEGER NOT NULL DEFAULT 0 CHECK (surfaced_to_user IN (0, 1)),
    CHECK ("end" > "start"),
    CHECK (
        (previous_start IS NULL AND previous_end IS NULL)
        OR (previous_start IS NOT NULL AND previous_end IS NOT NULL
            AND previous_end > previous_start)
    ),
    CHECK (julianday(decided_at) IS NOT NULL AND instr(decided_at, 'T') = 11),
    CHECK (julianday("start") IS NOT NULL AND instr("start", 'T') = 11),
    CHECK (julianday("end") IS NOT NULL AND instr("end", 'T') = 11),
    CHECK (previous_start IS NULL OR (julianday(previous_start) IS NOT NULL AND instr(previous_start, 'T') = 11)),
    CHECK (previous_end IS NULL OR (julianday(previous_end) IS NOT NULL AND instr(previous_end, 'T') = 11))
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    content TEXT NOT NULL,
    category TEXT NOT NULL,
    confidence REAL NOT NULL CHECK (confidence >= 0.0 AND confidence <= 1.0),
    source TEXT NOT NULL CHECK (source IN ('seed', 'extracted', 'explicit')),
    evidence_count INTEGER NOT NULL DEFAULT 1 CHECK (evidence_count >= 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    last_confirmed_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(last_confirmed_at, -1) = 'Z' OR substr(last_confirmed_at, -6) = '+00:00'),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11),
    CHECK (julianday(last_confirmed_at) IS NOT NULL AND instr(last_confirmed_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS goal_progress (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    logged_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(logged_at, -1) = 'Z' OR substr(logged_at, -6) = '+00:00'),
    amount REAL NOT NULL CHECK (amount > 0),
    source TEXT NOT NULL CHECK (source IN ('task', 'manual', 'inferred')),
    task_id INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    CHECK (julianday(logged_at) IS NOT NULL AND instr(logged_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS goal_schedule_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id INTEGER NOT NULL REFERENCES goals(id) ON DELETE CASCADE,
    task_id INTEGER NOT NULL UNIQUE REFERENCES tasks(id) ON DELETE RESTRICT,
    period_start TEXT NOT NULL
        CHECK (substr(period_start, -1) = 'Z' OR substr(period_start, -6) = '+00:00'),
    period_end TEXT NOT NULL
        CHECK (substr(period_end, -1) = 'Z' OR substr(period_end, -6) = '+00:00'),
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    planned_amount REAL NOT NULL CHECK (planned_amount > 0),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    cancelled_at TEXT CHECK (
        cancelled_at IS NULL OR substr(cancelled_at, -1) = 'Z'
        OR substr(cancelled_at, -6) = '+00:00'
    ),
    UNIQUE (goal_id, period_start, ordinal),
    CHECK (period_end > period_start),
    CHECK (julianday(period_start) IS NOT NULL AND instr(period_start, 'T') = 11),
    CHECK (julianday(period_end) IS NOT NULL AND instr(period_end, 'T') = 11),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11),
    CHECK (cancelled_at IS NULL OR (julianday(cancelled_at) IS NOT NULL AND instr(cancelled_at, 'T') = 11))
);

CREATE TABLE IF NOT EXISTS event_change_proposals (
    id TEXT PRIMARY KEY,
    operation TEXT NOT NULL CHECK (operation IN ('create', 'update')),
    payload TEXT NOT NULL CHECK (json_valid(payload) AND json_type(payload) = 'object'),
    conflicts TEXT NOT NULL CHECK (json_valid(conflicts) AND json_type(conflicts) = 'array'),
    created_at TEXT NOT NULL
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    expires_at TEXT NOT NULL
        CHECK (substr(expires_at, -1) = 'Z' OR substr(expires_at, -6) = '+00:00'),
    claimed_at TEXT CHECK (
        claimed_at IS NULL OR substr(claimed_at, -1) = 'Z'
        OR substr(claimed_at, -6) = '+00:00'
    ),
    claim_token TEXT,
    consumed_at TEXT CHECK (
        consumed_at IS NULL OR substr(consumed_at, -1) = 'Z'
        OR substr(consumed_at, -6) = '+00:00'
    ),
    CHECK (expires_at > created_at),
    CHECK (
        (claimed_at IS NULL AND claim_token IS NULL)
        OR (claimed_at IS NOT NULL AND claim_token IS NOT NULL)
    ),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11),
    CHECK (julianday(expires_at) IS NOT NULL AND instr(expires_at, 'T') = 11),
    CHECK (claimed_at IS NULL OR (julianday(claimed_at) IS NOT NULL AND instr(claimed_at, 'T') = 11)),
    CHECK (consumed_at IS NULL OR (julianday(consumed_at) IS NOT NULL AND instr(consumed_at, 'T') = 11))
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content TEXT NOT NULL,
    tool_calls TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(tool_calls) AND json_type(tool_calls) = 'array'),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    session_id TEXT NOT NULL,
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS daily_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE CHECK (date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
    brief_sent_at TEXT CHECK (
        brief_sent_at IS NULL OR substr(brief_sent_at, -1) = 'Z'
        OR substr(brief_sent_at, -6) = '+00:00'
    ),
    debrief_sent_at TEXT CHECK (
        debrief_sent_at IS NULL OR substr(debrief_sent_at, -1) = 'Z'
        OR substr(debrief_sent_at, -6) = '+00:00'
    ),
    planned TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(planned)),
    completed TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(completed)),
    notes TEXT,
    CHECK (brief_sent_at IS NULL OR (julianday(brief_sent_at) IS NOT NULL AND instr(brief_sent_at, 'T') = 11)),
    CHECK (debrief_sent_at IS NULL OR (julianday(debrief_sent_at) IS NOT NULL AND instr(debrief_sent_at, 'T') = 11))
);

CREATE TABLE IF NOT EXISTS reminders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message TEXT NOT NULL CHECK (length(trim(message)) > 0),
    remind_at TEXT NOT NULL
        CHECK (substr(remind_at, -1) = 'Z' OR substr(remind_at, -6) = '+00:00'),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'delivered', 'cancelled')),
    created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(created_at, -1) = 'Z' OR substr(created_at, -6) = '+00:00'),
    updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(updated_at, -1) = 'Z' OR substr(updated_at, -6) = '+00:00'),
    cancelled_at TEXT CHECK (
        cancelled_at IS NULL OR substr(cancelled_at, -1) = 'Z'
        OR substr(cancelled_at, -6) = '+00:00'
    ),
    delivered_at TEXT CHECK (
        delivered_at IS NULL OR substr(delivered_at, -1) = 'Z'
        OR substr(delivered_at, -6) = '+00:00'
    ),
    delivery_attempts INTEGER NOT NULL DEFAULT 0 CHECK (delivery_attempts >= 0),
    last_attempt_at TEXT CHECK (
        last_attempt_at IS NULL OR substr(last_attempt_at, -1) = 'Z'
        OR substr(last_attempt_at, -6) = '+00:00'
    ),
    next_attempt_at TEXT CHECK (
        next_attempt_at IS NULL OR substr(next_attempt_at, -1) = 'Z'
        OR substr(next_attempt_at, -6) = '+00:00'
    ),
    lease_token TEXT,
    lease_expires_at TEXT CHECK (
        lease_expires_at IS NULL OR substr(lease_expires_at, -1) = 'Z'
        OR substr(lease_expires_at, -6) = '+00:00'
    ),
    CHECK (julianday(remind_at) IS NOT NULL AND instr(remind_at, 'T') = 11),
    CHECK (julianday(created_at) IS NOT NULL AND instr(created_at, 'T') = 11),
    CHECK (julianday(updated_at) IS NOT NULL AND instr(updated_at, 'T') = 11),
    CHECK (cancelled_at IS NULL OR (julianday(cancelled_at) IS NOT NULL AND instr(cancelled_at, 'T') = 11)),
    CHECK (delivered_at IS NULL OR (julianday(delivered_at) IS NOT NULL AND instr(delivered_at, 'T') = 11)),
    CHECK (last_attempt_at IS NULL OR (julianday(last_attempt_at) IS NOT NULL AND instr(last_attempt_at, 'T') = 11)),
    CHECK (next_attempt_at IS NULL OR (julianday(next_attempt_at) IS NOT NULL AND instr(next_attempt_at, 'T') = 11)),
    CHECK (lease_expires_at IS NULL OR (julianday(lease_expires_at) IS NOT NULL AND instr(lease_expires_at, 'T') = 11)),
    CHECK (
        (lease_token IS NULL AND lease_expires_at IS NULL)
        OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)
    ),
    CHECK (
        (status = 'pending' AND cancelled_at IS NULL AND delivered_at IS NULL
            AND next_attempt_at IS NOT NULL)
        OR (status = 'delivered' AND delivered_at IS NOT NULL AND cancelled_at IS NULL
            AND next_attempt_at IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
        OR (status = 'cancelled' AND cancelled_at IS NOT NULL AND delivered_at IS NULL
            AND next_attempt_at IS NULL AND lease_token IS NULL AND lease_expires_at IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS facts_engine_evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id INTEGER NOT NULL REFERENCES facts(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    evidence TEXT NOT NULL,
    observed_at TEXT NOT NULL
        CHECK (substr(observed_at, -1) = 'Z' OR substr(observed_at, -6) = '+00:00'),
    observation_key TEXT,
    CHECK (julianday(observed_at) IS NOT NULL AND instr(observed_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS facts_engine_batches (
    observation_key TEXT PRIMARY KEY,
    processed_at TEXT NOT NULL
        CHECK (substr(processed_at, -1) = 'Z' OR substr(processed_at, -6) = '+00:00'),
    fact_ids TEXT NOT NULL DEFAULT '[]'
        CHECK (json_valid(fact_ids) AND json_type(fact_ids) = 'array'),
    CHECK (julianday(processed_at) IS NOT NULL AND instr(processed_at, 'T') = 11)
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    occurred_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
        CHECK (substr(occurred_at, -1) = 'Z' OR substr(occurred_at, -6) = '+00:00'),
    component TEXT NOT NULL CHECK (
        component IN ('agent_loop', 'session_summary', 'scheduler', 'facts')
    ),
    model TEXT NOT NULL,
    session_id TEXT,
    input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (input_tokens >= 0),
    cached_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    output_tokens INTEGER NOT NULL DEFAULT 0 CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER NOT NULL DEFAULT 0 CHECK (reasoning_tokens >= 0),
    total_tokens INTEGER NOT NULL DEFAULT 0 CHECK (total_tokens >= 0),
    estimated_cost_usd REAL,
    CHECK (julianday(occurred_at) IS NOT NULL AND instr(occurred_at, 'T') = 11)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled_start ON tasks(scheduled_start);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_id ON tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_tasks_series_history
    ON tasks(series_key, category, energy, status, completed_at);
CREATE INDEX IF NOT EXISTS idx_events_time_range ON events(start_time, end_time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_gcal_id
    ON events(gcal_event_id) WHERE gcal_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_facts_engine_evidence_fact_time
    ON facts_engine_evidence(fact_id, observed_at, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_facts_engine_evidence_observation
    ON facts_engine_evidence(fact_id, kind, observation_key)
    WHERE observation_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_usage_events_time_component
    ON usage_events(occurred_at, component);
CREATE INDEX IF NOT EXISTS idx_schedule_decisions_task_time
    ON schedule_decisions(task_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_facts_active_category ON facts(active, category);
CREATE INDEX IF NOT EXISTS idx_goal_progress_goal_time ON goal_progress(goal_id, logged_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_goal_progress_task_once
    ON goal_progress(goal_id, task_id) WHERE task_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_goal_schedule_period
    ON goal_schedule_items(goal_id, period_start, period_end, ordinal);
CREATE INDEX IF NOT EXISTS idx_event_change_proposals_pending
    ON event_change_proposals(expires_at, consumed_at, claimed_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_reminders_due
    ON reminders(status, next_attempt_at, lease_expires_at, id);

CREATE TRIGGER IF NOT EXISTS validate_schedule_reasoning_insert
BEFORE INSERT ON schedule_decisions
WHEN NOT EXISTS (
    WITH RECURSIVE positions(index_) AS (
        SELECT 1
        UNION ALL
        SELECT index_ + 1 FROM positions WHERE index_ < length(NEW.reasoning)
    )
    SELECT 1
    FROM positions
    WHERE unicode(substr(NEW.reasoning, index_, 1)) NOT IN (
        9, 10, 11, 12, 13, 28, 29, 30, 31, 32,
        133, 160, 5760,
        8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
        8232, 8233, 8239, 8287, 12288
    )
)
BEGIN
    SELECT RAISE(ABORT, 'reasoning must contain a non-whitespace character');
END;

CREATE TRIGGER IF NOT EXISTS validate_schedule_reasoning_update
BEFORE UPDATE OF reasoning ON schedule_decisions
WHEN NOT EXISTS (
    WITH RECURSIVE positions(index_) AS (
        SELECT 1
        UNION ALL
        SELECT index_ + 1 FROM positions WHERE index_ < length(NEW.reasoning)
    )
    SELECT 1
    FROM positions
    WHERE unicode(substr(NEW.reasoning, index_, 1)) NOT IN (
        9, 10, 11, 12, 13, 28, 29, 30, 31, 32,
        133, 160, 5760,
        8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, 8201, 8202,
        8232, 8233, 8239, 8287, 12288
    )
)
BEGIN
    SELECT RAISE(ABORT, 'reasoning must contain a non-whitespace character');
END;

CREATE TRIGGER IF NOT EXISTS validate_schedule_decision_facts_insert
BEFORE INSERT ON schedule_decisions
WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.facts_used) AS item
    WHERE item.type != 'integer'
       OR NOT EXISTS (SELECT 1 FROM facts WHERE facts.id = item.value)
)
BEGIN
    SELECT RAISE(ABORT, 'facts_used must contain existing integer fact ids');
END;

CREATE TRIGGER IF NOT EXISTS validate_schedule_decision_facts_update
BEFORE UPDATE OF facts_used ON schedule_decisions
WHEN EXISTS (
    SELECT 1
    FROM json_each(NEW.facts_used) AS item
    WHERE item.type != 'integer'
       OR NOT EXISTS (SELECT 1 FROM facts WHERE facts.id = item.value)
)
BEGIN
    SELECT RAISE(ABORT, 'facts_used must contain existing integer fact ids');
END;

CREATE TRIGGER IF NOT EXISTS protect_referenced_schedule_facts_delete
BEFORE DELETE ON facts
WHEN EXISTS (
    SELECT 1
    FROM schedule_decisions, json_each(schedule_decisions.facts_used) AS item
    WHERE item.type = 'integer' AND item.value = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'fact is referenced by a schedule decision');
END;

CREATE TRIGGER IF NOT EXISTS protect_referenced_schedule_facts_id_update
BEFORE UPDATE OF id ON facts
WHEN NEW.id != OLD.id AND EXISTS (
    SELECT 1
    FROM schedule_decisions, json_each(schedule_decisions.facts_used) AS item
    WHERE item.type = 'integer' AND item.value = OLD.id
)
BEGIN
    SELECT RAISE(ABORT, 'fact is referenced by a schedule decision');
END;

PRAGMA user_version = 5;
