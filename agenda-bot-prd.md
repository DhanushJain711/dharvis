# Product Requirements Document: Claude-Powered Agenda Bot

## Overview

A text-based personal assistant that manages tasks, events, and calendar integration through Telegram messaging. The bot intelligently tracks scheduled events and deadline-based tasks, integrates with Google Calendar, and provides daily prioritized recommendations.

---

## Problem Statement

Managing tasks and calendar events across multiple systems creates friction. The user wants a unified, conversational interface where they can:
- Add events and tasks via natural language text
- Query their schedule without opening multiple apps
- Receive intelligent daily recommendations that combine calendar events with task deadlines

---

## User Persona

- **Primary User:** Dhanush (college student at UT Austin)
- **Use Pattern:** Texts throughout the day to add/query tasks and events
- **Existing Tools:** Google Calendar for some scheduled events
- **Platform:** Telegram on mobile and desktop

---

## Core Features

### 1. Natural Language Input Processing

The bot must understand and correctly classify user input into:

**Events** (time-specific):
- "Hang out with friends at 9pm Thursday"
- "Meeting with professor tomorrow at 2pm"
- "Dinner with family Saturday 7pm at Olive Garden"

**Tasks** (deadline-based):
- "Finish math problem set by Friday morning"
- "Submit fellowship application by January 15th"
- "Read chapter 5 before Monday's class"

**Queries**:
- "What do I have today?"
- "What's on my schedule this week?"
- "When is my math pset due?"
- "Am I free Thursday evening?"

**Modifications**:
- "Cancel dinner on Saturday"
- "Move the professor meeting to 3pm"
- "Mark math pset as done"
- "Push the fellowship deadline to next week"

### 2. Data Storage

**Tasks Table:**
```
- id (primary key)
- title (string)
- description (string, optional)
- deadline (datetime)
- priority (low/medium/high, default medium)
- status (pending/completed)
- created_at (datetime)
- completed_at (datetime, nullable)
```

**Events Table:**
```
- id (primary key)
- title (string)
- description (string, optional)
- start_time (datetime)
- end_time (datetime, optional)
- location (string, optional)
- created_at (datetime)
- source (bot/gcal)
```

**Conversation Context Table (optional, for multi-turn):**
```
- id (primary key)
- user_message (string)
- bot_response (string)
- timestamp (datetime)
```

### 3. Google Calendar Integration

**Read Operations:**
- Fetch today's events
- Fetch upcoming events (next 7 days)
- Check availability for a specific time slot

**Write Operations (optional for MVP, can add later):**
- Create events in Google Calendar from bot commands
- Sync bot-created events to Google Calendar

**Authentication:**
- OAuth 2.0 flow with refresh tokens
- Store credentials securely (environment variables or encrypted file)

### 4. Intelligent Daily Briefing

When queried "What do I have today?" or similar, the bot should return:

1. **Scheduled Events** (from both bot storage and Google Calendar)
   - Sorted by time
   - Include location if available

2. **Tasks Due Today**
   - Any task with deadline = today

3. **Upcoming Task Warnings**
   - Tasks due tomorrow or day after
   - Weighted by estimated effort (if a task seems big, mention it earlier)

4. **Proactive Suggestions**
   - "You have a light morning - good time to work on [task due Friday]"
   - "Heads up: [big task] is due in 2 days"

### 5. Telegram Bot Interface

**Commands (optional, natural language is primary):**
- `/start` - Welcome message and quick tutorial
- `/today` - Today's briefing
- `/week` - Week overview
- `/tasks` - List pending tasks
- `/help` - Usage guide

**Interaction Style:**
- Respond conversationally, not robotically
- Keep responses concise (this is texting, not email)
- Use minimal formatting (no excessive bullet points in simple responses)
- Confirm actions clearly: "Got it - added 'Math pset' due Friday 9am"

---

## Technical Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────────┐
│                        AWS Lightsail                            │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    Python Application                      │  │
│  │  ┌─────────────┐  ┌─────────────┐  ┌──────────────────┐   │  │
│  │  │  Telegram   │  │   Claude    │  │  Google Calendar │   │  │
│  │  │  Bot Handler│  │   API       │  │      API         │   │  │
│  │  └──────┬──────┘  └──────┬──────┘  └────────┬─────────┘   │  │
│  │         │                │                   │             │  │
│  │         └────────────────┼───────────────────┘             │  │
│  │                          │                                 │  │
│  │                   ┌──────┴──────┐                          │  │
│  │                   │   SQLite    │                          │  │
│  │                   │   Database  │                          │  │
│  │                   └─────────────┘                          │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

### Tech Stack

| Component | Technology |
|-----------|------------|
| Language | Python 3.11+ |
| Telegram | `python-telegram-bot` library (v20+) |
| Database | SQLite with `aiosqlite` for async |
| Google Calendar | `google-api-python-client` + `google-auth` |
| Claude API | `anthropic` Python SDK |
| Hosting | AWS Lightsail (Ubuntu) |
| Process Manager | `systemd` service |

### File Structure

```
agenda-bot/
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point, bot initialization
│   ├── config.py               # Environment variables, settings
│   ├── database.py             # SQLite models and queries
│   ├── telegram_handler.py     # Telegram bot handlers
│   ├── claude_agent.py         # Claude API integration + prompt logic
│   ├── calendar_service.py     # Google Calendar API wrapper
│   └── utils.py                # Date parsing, helpers
├── tests/
│   ├── test_database.py
│   ├── test_claude_agent.py
│   └── test_calendar_service.py
├── scripts/
│   └── setup_gcal_auth.py      # One-time OAuth setup script
├── requirements.txt
├── .env.example
├── README.md
└── systemd/
    └── agenda-bot.service      # systemd service file for deployment
```

### Environment Variables

```
# .env
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key
GOOGLE_CALENDAR_CREDENTIALS_PATH=/path/to/credentials.json
GOOGLE_CALENDAR_TOKEN_PATH=/path/to/token.json
DATABASE_PATH=/path/to/agenda.db
USER_TIMEZONE=America/Chicago
```

---

## Claude Agent Design

### System Prompt Structure

```
You are Dhanush's personal task and calendar assistant. You communicate via Telegram text messages.

## Current Context
- Current date/time: {current_datetime}
- User timezone: America/Chicago (CST/CDT)

## Today's Google Calendar Events
{gcal_events_formatted}

## Pending Tasks from Database
{tasks_formatted}

## Your Capabilities
1. ADD_TASK: Create a new task with a deadline
2. ADD_EVENT: Create a new event with a specific time
3. COMPLETE_TASK: Mark a task as done
4. DELETE_TASK: Remove a task
5. DELETE_EVENT: Remove an event
6. MODIFY_TASK: Change task details (deadline, title)
7. MODIFY_EVENT: Change event details (time, title)
8. QUERY: Answer questions about schedule/tasks

## Response Format
For actions, respond with JSON:
{
  "action": "ADD_TASK" | "ADD_EVENT" | "COMPLETE_TASK" | "DELETE_TASK" | "DELETE_EVENT" | "MODIFY_TASK" | "MODIFY_EVENT" | "QUERY",
  "params": { ... action-specific parameters ... },
  "message": "Conversational response to send to user"
}

For queries with no action needed:
{
  "action": "QUERY",
  "params": {},
  "message": "Your conversational response with the requested information"
}

## Style Guidelines
- Keep responses concise - this is texting
- Be conversational, not robotic
- Confirm actions clearly
- When listing items, keep it scannable but not overly formatted
- Proactively mention upcoming deadlines when relevant
```

### Action Parameter Schemas

```python
# ADD_TASK
{
  "title": str,
  "deadline": str (ISO format),
  "priority": "low" | "medium" | "high",
  "description": str | None
}

# ADD_EVENT
{
  "title": str,
  "start_time": str (ISO format),
  "end_time": str | None (ISO format),
  "location": str | None,
  "description": str | None
}

# COMPLETE_TASK
{
  "task_id": int | None,
  "task_title": str | None  # fuzzy match if no ID
}

# DELETE_TASK / DELETE_EVENT
{
  "id": int | None,
  "title": str | None  # fuzzy match if no ID
}

# MODIFY_TASK
{
  "task_id": int | None,
  "task_title": str | None,
  "new_title": str | None,
  "new_deadline": str | None,
  "new_priority": str | None
}

# MODIFY_EVENT
{
  "event_id": int | None,
  "event_title": str | None,
  "new_title": str | None,
  "new_start_time": str | None,
  "new_end_time": str | None,
  "new_location": str | None
}
```

---

## User Flows

### Flow 1: Adding a Task

```
User: "finish math pset by friday morning"

Bot Processing:
1. Receive message via Telegram webhook
2. Build context (current time, existing tasks, calendar events)
3. Send to Claude API with system prompt
4. Claude returns: {
     "action": "ADD_TASK",
     "params": {
       "title": "Finish math pset",
       "deadline": "2024-01-19T09:00:00-06:00",
       "priority": "medium"
     },
     "message": "Got it - math pset added, due Friday 9am ✓"
   }
5. Execute: Insert task into SQLite
6. Send message to user

User sees: "Got it - math pset added, due Friday 9am ✓"
```

### Flow 2: Daily Briefing

```
User: "what do i have today"

Bot Processing:
1. Fetch Google Calendar events for today
2. Fetch tasks due today + upcoming tasks
3. Send to Claude with full context
4. Claude generates briefing

User sees:
"Today (Thursday Jan 18):

📅 2pm - Meeting with advisor (Zoom)
📅 7pm - Dinner with roommates

📋 Due today:
- Submit lab report (by 11:59pm)

⚠️ Coming up:
- Math pset due tomorrow morning
- Fellowship app due in 3 days

You've got a free morning - good time to knock out that pset."
```

### Flow 3: Checking Availability

```
User: "am i free saturday afternoon"

Bot Processing:
1. Check Google Calendar for Saturday 12pm-6pm
2. Check bot events for same window
3. Claude responds based on findings

User sees: "Saturday afternoon looks clear - nothing scheduled between 12-6pm. Want me to block some time?"
```

### Flow 4: Completing a Task

```
User: "done with the math pset"

Bot Processing:
1. Claude identifies task via fuzzy match
2. Returns COMPLETE_TASK action
3. Bot marks task complete in database

User sees: "Nice! Marked 'Finish math pset' as complete ✓"
```

---

## Error Handling

### Ambiguous Input
```
User: "cancel the meeting"
Bot: "I see two meetings this week - which one?
1. Meeting with advisor (Thu 2pm)
2. Study group meeting (Sat 10am)"
```

### No Matches Found
```
User: "mark chemistry homework done"
Bot: "Hmm, I don't see a chemistry homework task. Did you mean something else, or should I add it as a new task?"
```

### API Failures
- **Claude API down:** "Sorry, having trouble processing that right now. Try again in a minute?"
- **Google Calendar API down:** Serve from local cache if available, note that calendar might not be current
- **Database error:** Log error, respond with generic failure message

---

## Security Considerations

1. **Single User:** This bot is for personal use only. Validate Telegram user ID matches expected user.
2. **API Keys:** Store in environment variables, never in code
3. **Google OAuth:** Store refresh token securely, use minimal scopes (calendar.readonly for MVP)
4. **No Sensitive Data Logging:** Don't log full message contents in production

---

## MVP Scope (v1.0)

### Included
- [x] Telegram bot with webhook
- [x] Add/complete/delete tasks via natural language
- [x] Add/delete events via natural language
- [x] Google Calendar read integration
- [x] Daily briefing query
- [x] SQLite persistence
- [x] Single-user auth (Telegram ID check)
- [x] Deployment on AWS Lightsail

### Deferred to v1.1
- [ ] Google Calendar write (create events)
- [ ] Recurring tasks
- [ ] Task priority auto-adjustment based on deadline proximity
- [ ] Weekly review/summary
- [ ] Natural language time modifications ("push it back 2 hours")

### Deferred to v2.0
- [ ] Multi-user support
- [ ] Notion/other integrations
- [ ] Voice message support
- [ ] Proactive notifications (morning briefing push)

---

## Deployment Instructions (AWS Lightsail)

### 1. Create Lightsail Instance
- OS: Ubuntu 22.04 LTS
- Plan: $3.50/month (512MB RAM, 1 vCPU)
- Enable static IP

### 2. Initial Server Setup
```bash
# SSH into instance
ssh -i your-key.pem ubuntu@your-ip

# Update system
sudo apt update && sudo apt upgrade -y

# Install Python and dependencies
sudo apt install python3.11 python3.11-venv python3-pip git -y

# Clone repo
git clone https://github.com/your-username/agenda-bot.git
cd agenda-bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### 3. Configure Environment
```bash
# Create .env file
cp .env.example .env
nano .env
# Fill in all required values
```

### 4. Setup Google Calendar OAuth
```bash
# Run one-time auth script (requires browser, may need to do locally first)
python scripts/setup_gcal_auth.py
# Copy generated token.json to server
```

### 5. Create systemd Service
```bash
sudo cp systemd/agenda-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable agenda-bot
sudo systemctl start agenda-bot

# Check status
sudo systemctl status agenda-bot

# View logs
sudo journalctl -u agenda-bot -f
```

### 6. Setup Telegram Webhook
```bash
# Set webhook URL (run once)
curl "https://api.telegram.org/bot<YOUR_BOT_TOKEN>/setWebhook?url=https://your-domain.com/webhook"
```

Note: For webhook, you'll need either:
- A domain with SSL (use Let's Encrypt)
- Or use polling mode instead (simpler for MVP, slightly less efficient)

---

## Testing Plan

### Unit Tests
- Database CRUD operations
- Date parsing utilities
- Claude response parsing

### Integration Tests
- End-to-end message flow (mock Telegram, real Claude)
- Google Calendar API calls (use test calendar)

### Manual Testing Checklist
- [ ] Add task with various date formats
- [ ] Add event with time and location
- [ ] Query today's schedule
- [ ] Complete a task
- [ ] Delete a task
- [ ] Delete an event
- [ ] Handle ambiguous input
- [ ] Handle no-match scenarios
- [ ] Verify Google Calendar events appear in briefing

---

## Success Metrics

1. **Reliability:** Bot responds to 99%+ of messages within 5 seconds
2. **Accuracy:** Claude correctly classifies intent 95%+ of the time
3. **Usability:** User (Dhanush) uses bot daily for 2+ weeks
4. **Cost:** Monthly API costs stay under $10

---

## Appendix: Example Conversations

### Adding Various Items
```
User: coffee with jake tomorrow 3pm
Bot: Added ☕ Coffee with Jake tomorrow (Fri) at 3pm

User: need to email professor chen about research position by end of week
Bot: Task added - Email Professor Chen about research position, due Sunday 11:59pm

User: haircut saturday 11am at supercuts
Bot: Added 💇 Haircut at Supercuts, Saturday 11am
```

### Queries
```
User: what's due this week
Bot: This week's deadlines:
- Math pset (Fri 9am)
- Email Professor Chen (Sun 11:59pm)
- Lab report revision (Sun 11:59pm)

User: anything tomorrow afternoon
Bot: Tomorrow afternoon:
- 3pm: Coffee with Jake

No tasks due tomorrow, but heads up - math pset is due Friday morning.
```

### Modifications
```
User: actually move coffee with jake to 4pm
Bot: Done - Coffee with Jake moved to 4pm

User: cancel the haircut
Bot: Removed haircut appointment on Saturday ✓

User: math pset is actually due saturday not friday
Bot: Updated - Math pset now due Saturday 9am
```
