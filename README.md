# Dharvis

A stateful, OpenAI-powered Telegram personal assistant foundation for tasks,
calendar events, scheduling decisions, facts, goals, and proactive planning.

The public contracts for parallel implementation work are indexed in
[`CONTRACTS.md`](CONTRACTS.md). The OpenAI function schemas in `src/tools.py`
are the authoritative tool definitions, and `src/schema.sql` is the only
canonical database schema.

## Features

- **Natural Language Processing**: Add tasks, events, and query your schedule using conversational text
- **Task Management**: Create, complete, delete, and modify tasks with deadlines and priorities
- **Safe Event Management**: Conflicting fixed events are warned about first and require an explicit later confirmation before they are created or changed
- **Goal Sessions**: Scheduling-enabled weekly/monthly goals materialize paced, task-backed work sessions and reschedule missed automatic sessions
- **Google Calendar Integration**: Reads visible calendars for availability and briefs; writes only owned `Kalendra` events with consistent category/kind colors
- **Daily Briefings**: Get summaries of local commitments, external Google events, due tasks, scheduled work, and goal pace
- **Learning From Outcomes**: Reuse observed durations only for matching completed task families; explicit estimates always take precedence

## Quick Start

### 1. Install Dependencies

```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials and local-time policy:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ALLOWED_USER_ID=your_telegram_user_id
OPENAI_API_KEY=your_openai_api_key
AGENT_MODEL_ID=gpt-5.6-terra
SUMMARY_MODEL_ID=gpt-5.6-luna
SCHEDULER_MODEL_ID=gpt-5.6-terra
FACTS_MODEL_ID=gpt-5.6-luna
OPENAI_REASONING_EFFORT=medium
USER_TIMEZONE=America/Chicago
QUIET_HOURS_START=22:00
QUIET_HOURS_END=07:00
DAILY_BRIEF_TIME=07:30
DAILY_DEBRIEF_TIME=20:30
REASONING_VERBOSITY=brief
KALENDRA_CALENDAR_NAME=Kalendra
```

Additional deployment and tuning settings are centralized in `src/config.py`,
including Google Calendar credential/token paths and IDs, database path,
Telegram polling timeout, history limits, and scheduler defaults.

### 3. Create Telegram Bot

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to your `.env` file

### 4. Get Your Telegram User ID

Message [@userinfobot](https://t.me/userinfobot) to get your user ID, then add it to `ALLOWED_USER_ID`.

### 5. Run the Bot

Local development uses polling by default (`RUN_MODE=polling`):

```bash
python -m src.main
```

To validate configuration, initialize the schema, and build the Telegram
application without starting network polling:

```bash
python -m src.main --check
```

## Usage Examples

### Adding Tasks
```
finish essay by friday
add task: complete homework by tomorrow 5pm
need to email professor by end of week
```

### Adding Events
```
coffee with Jake tomorrow 3pm
meeting with advisor friday 2pm at office
dinner saturday 7pm at Olive Garden
```

### Queries
```
what do I have today?
what's due this week?
am I free saturday afternoon?
```

### Modifications
```
mark essay as done
cancel the meeting
move coffee with Jake to 4pm
push the deadline to next week
```

### Commands
- `/start` - Welcome message
- `/help` - Usage guide
- `/cost` - Today's and month-to-date agent/background cost and cache rate

## Google Calendar Setup (Optional)

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a new project
3. Enable the Google Calendar API
4. Create OAuth 2.0 credentials (Desktop application)
5. Download `credentials.json` to the project root
6. Run the setup script:

```bash
python scripts/setup_gcal_auth.py
```

This will open a browser for authentication and save `token.json`.

By default, credentials, the refreshed OAuth token, SQLite database, APScheduler
store, and backups live under `DATA_DIR` (`./data`). Keep them together by setting
only `DATA_DIR` on a persistent disk. `GOOGLE_CALENDAR_CREDENTIALS_PATH`,
`GOOGLE_CALENDAR_TOKEN_PATH`, `DATABASE_PATH`, and
`APSCHEDULER_DATABASE_PATH` are optional explicit overrides for unusual layouts.

Dharvis creates and updates only events it owns on its secondary `Kalendra` calendar. Owned metadata uses the canonical kinds `fixed-event`, `task-block`, and `goal-session`; it does not edit events on your primary or other visible calendars.

## Running Tests

```bash
pytest tests/ -v
```

## Project Structure

```
dharvis/
├── src/
│   ├── __init__.py
│   ├── main.py              # Entry point
│   ├── config.py            # Environment configuration
│   ├── schema.sql           # Canonical database schema
│   ├── migrate.py           # Idempotent schema runner
│   ├── store.py             # Stateful persistence contract
│   ├── tools.py             # Authoritative OpenAI tool schemas
│   ├── timeutil.py          # Timezone-aware date handling
│   ├── agent.py             # Stateful agent contract
│   ├── telegram_handler.py  # Telegram bot handlers
│   ├── calendar_service.py  # Google Calendar API
│   └── ...                  # Scheduler, jobs, facts, history, free/busy
├── tests/
│   ├── test_database.py
│   ├── test_claude_agent.py
│   └── test_calendar_service.py
├── scripts/
│   └── setup_gcal_auth.py
├── CONTRACTS.md             # Public implementation signatures
├── pyproject.toml           # Package and console entrypoint
├── requirements.txt
├── .env.example
└── README.md
```

## Railway deployment

Railway continues to use polling: the included `railway.json` starts
`python -m src.main`, checks `/healthz`, and restarts failed processes. Attach a
Railway persistent volume at `/data`, then set only:

```text
DATA_DIR=/data
```

Supply `GOOGLE_CALENDAR_TOKEN_BASE64` for the first boot or copy `token.json`
onto the volume. Refreshed OAuth credentials, SQLite state, Telegram checklist
state, the Kalendra calendar id, and APScheduler's job store then survive
restarts. `SQLAlchemy` is included so APScheduler uses the persistent job store;
daily-log occurrence markers and startup catch-up remain a second line of
defense after downtime.

All application logs are JSON lines. In polling mode the health endpoint returns
200 only when SQLite and job configuration are ready.

## Azure App Service deployment

Azure uses Telegram webhooks rather than polling. Configure the App Service
startup command as:

```bash
uvicorn src.web:app --host 0.0.0.0 --port 8000
```

Set `RUN_MODE=webhook`, `DATA_DIR=/home/data`, the normal Telegram/OpenAI/Google
settings, and these App Settings:

```text
PUBLIC_BASE_URL=https://<your-app>.azurewebsites.net
TELEGRAM_WEBHOOK_PATH=<generated-url-safe-path>
TELEGRAM_WEBHOOK_SECRET=<generated-url-safe-secret>
```

Generate the final two values before setting them in Azure:

```bash
python scripts/gen_webhook_secrets.py
```

The script prints an `az webapp config appsettings set` command with fresh,
independent values. Treat both as secrets; the URL path is intentionally hard to
guess and Telegram also supplies the header secret for each update. `PUBLIC_BASE_URL`
must be the HTTPS origin only—without a path. Azure's public TLS endpoint is used
directly; do not run a local tunnel for this production setup.

App Service should probe `GET /healthz`, which returns `{"ok": true}` while the
ASGI app is live. The webhook route accepts only the configured path, requires
Telegram's `X-Telegram-Bot-Api-Secret-Token` to match, rejects invalid JSON or
updates, then queues valid updates for the Telegram application. On lifespan
startup it registers the Telegram webhook and starts proactive jobs; on shutdown
it stops jobs and cleanly stops and shuts down the Telegram application.

With `DATA_DIR=/home/data`, the application keeps the SQLite database, scheduler
store, OAuth token, and `backups/` directory on App Service persistent storage.
It creates a consistent SQLite backup at 03:00 in the configured user timezone
and retains dated backups for 14 days. A nightly facts fallback also extracts
persisted day evidence when no debrief is submitted.

## Evaluation and audits

These commands use real OpenAI responses but replace tools with harmless
recorders or temporary databases:

```bash
python scripts/eval_agent.py
python scripts/audit_scheduler.py
python scripts/audit_tone.py
```

The agent regression set contains 45 messages across multi-item creation,
relative dates, ambiguous references, corrections, multi-turn context, and
cross-domain queries. Reports are written under `evals/`. An
`OPENAI_API_KEY` is required; the scripts fail fast instead of reporting fake
offline scores.

The repository's offline checks use fake calendar/Telegram clients and do not exercise live Google Calendar, Telegram delivery, or paid OpenAI calls.

## License

MIT
