# Dharvis

A Claude-powered Telegram bot for managing tasks and calendar events through natural language.

## Features

- **Natural Language Processing**: Add tasks, events, and query your schedule using conversational text
- **Task Management**: Create, complete, delete, and modify tasks with deadlines and priorities
- **Event Management**: Create and manage events with times and locations
- **Google Calendar Integration**: Sync with your Google Calendar for a unified view
- **Daily Briefings**: Get summaries of your day including events, due tasks, and upcoming deadlines

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

Edit `.env` with your credentials:

```
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
ANTHROPIC_API_KEY=your_anthropic_api_key
ALLOWED_USER_ID=your_telegram_user_id
USER_TIMEZONE=America/Chicago
```

### 3. Create Telegram Bot

1. Message [@BotFather](https://t.me/botfather) on Telegram
2. Send `/newbot` and follow the prompts
3. Copy the bot token to your `.env` file

### 4. Get Your Telegram User ID

Message [@userinfobot](https://t.me/userinfobot) to get your user ID, then add it to `ALLOWED_USER_ID`.

### 5. Run the Bot

```bash
python -m src.main
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
- `/today` - Today's briefing
- `/week` - Week overview
- `/tasks` - List pending tasks
- `/help` - Usage guide

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
│   ├── database.py          # SQLite persistence
│   ├── telegram_handler.py  # Telegram bot handlers
│   ├── claude_agent.py      # Claude API integration
│   ├── calendar_service.py  # Google Calendar API
│   └── utils.py             # Date/time utilities
├── tests/
│   ├── test_database.py
│   ├── test_claude_agent.py
│   └── test_calendar_service.py
├── scripts/
│   └── setup_gcal_auth.py
├── requirements.txt
├── .env.example
└── README.md
```

## Deployment

For production deployment on AWS Lightsail or similar:

1. Set up a Linux server (Ubuntu recommended)
2. Clone the repository
3. Install Python 3.11+ and dependencies
4. Configure `.env`
5. Copy `token.json` from local OAuth setup
6. Create a systemd service for the bot
7. (Optional) Set up webhook mode with SSL

## License

MIT
