PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    target_amount REAL NOT NULL CHECK (target_amount > 0),
    target_unit TEXT NOT NULL CHECK (target_unit IN ('hours', 'sessions')),
    period TEXT NOT NULL CHECK (period IN ('week', 'month')),
    category TEXT NOT NULL,
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
    CHECK (julianday(logged_at) IS NOT NULL AND instr(logged_at, 'T') = 11)
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

CREATE INDEX IF NOT EXISTS idx_tasks_status_deadline ON tasks(status, deadline);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled_start ON tasks(scheduled_start);
CREATE INDEX IF NOT EXISTS idx_tasks_goal_id ON tasks(goal_id);
CREATE INDEX IF NOT EXISTS idx_events_time_range ON events(start_time, end_time);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_gcal_id
    ON events(gcal_event_id) WHERE gcal_event_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_schedule_decisions_task_time
    ON schedule_decisions(task_id, decided_at);
CREATE INDEX IF NOT EXISTS idx_facts_active_category ON facts(active, category);
CREATE INDEX IF NOT EXISTS idx_goal_progress_goal_time ON goal_progress(goal_id, logged_at);
CREATE INDEX IF NOT EXISTS idx_messages_session_time ON messages(session_id, created_at);

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

PRAGMA user_version = 2;
