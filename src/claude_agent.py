"""Claude API integration for natural language processing."""

import json
import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any

from anthropic import Anthropic

from config import config
from utils import (
    format_datetime_for_display,
    format_events_list,
    format_tasks_list,
    get_current_time,
)

logger = logging.getLogger(__name__)


class ActionType(Enum):
    """Types of actions the agent can perform."""

    ADD_TASK = "ADD_TASK"
    ADD_EVENT = "ADD_EVENT"
    COMPLETE_TASK = "COMPLETE_TASK"
    DELETE_TASK = "DELETE_TASK"
    DELETE_EVENT = "DELETE_EVENT"
    MODIFY_TASK = "MODIFY_TASK"
    MODIFY_EVENT = "MODIFY_EVENT"
    QUERY = "QUERY"


@dataclass
class AgentResponse:
    """Structured response from the Claude agent."""

    action: ActionType
    params: dict[str, Any]
    message: str
    raw_response: str


class ClaudeAgent:
    """Claude-powered agent for processing natural language requests."""

    def __init__(self, api_key: str | None = None):
        """Initialize the Claude agent.

        Args:
            api_key: Anthropic API key. Uses config default if None.
        """
        self.client = Anthropic(api_key=api_key or config.ANTHROPIC_API_KEY)
        self.model = "claude-sonnet-4-20250514"

    def build_system_prompt(
        self,
        current_time: str | None = None,
        tasks: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        gcal_events: list[dict[str, Any]] | None = None,
    ) -> str:
        """Build the system prompt with current context.

        Args:
            current_time: Current datetime string for display.
            tasks: List of pending tasks from database.
            events: List of upcoming events from database.
            gcal_events: List of events from Google Calendar.

        Returns:
            Complete system prompt string.
        """
        if current_time is None:
            now = get_current_time()
            current_time = format_datetime_for_display(now)

        tasks_formatted = format_tasks_list(tasks or [])
        events_formatted = format_events_list(events or [])
        gcal_events_formatted = format_events_list(gcal_events or [])

        return f"""You are Dharvis, Dhanush's personal task and calendar assistant. You communicate via Telegram text messages.

## Current Context
- Current date/time: {current_time}
- User timezone: {config.USER_TIMEZONE}

## Google Calendar Events (Next 7 Days)
{gcal_events_formatted}

## Bot Events (Next 7 Days)
{events_formatted}

## Pending Tasks from Database
{tasks_formatted}

## Your Capabilities
1. ADD_TASK: Create a new task with a deadline
2. ADD_EVENT: Create a new event with a specific time
3. COMPLETE_TASK: Mark a task as done
4. DELETE_TASK: Remove a task
5. DELETE_EVENT: Remove an event
6. MODIFY_TASK: Change task details (deadline, title, priority)
7. MODIFY_EVENT: Change event details (time, title, location)
8. QUERY: Answer questions about schedule/tasks

## Response Format
IMPORTANT: Always respond with valid JSON only. No text before or after the JSON.

For actions, respond with JSON:
{{
  "action": "ADD_TASK" | "ADD_EVENT" | "COMPLETE_TASK" | "DELETE_TASK" | "DELETE_EVENT" | "MODIFY_TASK" | "MODIFY_EVENT" | "QUERY",
  "params": {{ ... action-specific parameters ... }},
  "message": "Conversational response to send to user"
}}

## Action Parameter Schemas

ADD_TASK:
{{
  "title": "task title",
  "deadline": "ISO 8601 datetime string",
  "priority": "low" | "medium" | "high",
  "description": "optional description"
}}

ADD_EVENT:
{{
  "title": "event title",
  "start_time": "ISO 8601 datetime string",
  "end_time": "optional ISO 8601 datetime string",
  "location": "optional location",
  "description": "optional description"
}}

COMPLETE_TASK:
{{
  "task_id": number or null,
  "task_title": "title to fuzzy match if no ID"
}}

DELETE_TASK:
{{
  "id": number or null,
  "title": "title to fuzzy match if no ID"
}}

DELETE_EVENT:
{{
  "id": number or null,
  "title": "title to fuzzy match if no ID"
}}

MODIFY_TASK:
{{
  "task_id": number or null,
  "task_title": "title to match if no ID",
  "new_title": "optional new title",
  "new_deadline": "optional new ISO datetime",
  "new_priority": "optional new priority"
}}

MODIFY_EVENT:
{{
  "event_id": number or null,
  "event_title": "title to match if no ID",
  "new_title": "optional new title",
  "new_start_time": "optional new ISO datetime",
  "new_end_time": "optional new ISO datetime",
  "new_location": "optional new location"
}}

QUERY (for informational responses):
{{}}

## Style Guidelines
- Keep responses concise - this is texting
- Be conversational, not robotic
- Confirm actions clearly
- When listing items, keep it scannable but not overly formatted
- Proactively mention upcoming deadlines when relevant
- For today queries, include both calendar events and due tasks
- Use minimal emoji (checkmark for confirmations is fine)

## Important Notes
- When the user refers to times like "tomorrow", "Friday", "next week", calculate the correct ISO datetime based on the current time shown above
- Default task deadline time is 11:59pm if no specific time given
- Default event duration is 1 hour if no end time specified
- If input is ambiguous and you find multiple matches, ask for clarification in your message and use QUERY action
- If no matching task/event is found, explain this in your message and use QUERY action"""

    def process_message(
        self,
        user_message: str,
        tasks: list[dict[str, Any]] | None = None,
        events: list[dict[str, Any]] | None = None,
        gcal_events: list[dict[str, Any]] | None = None,
    ) -> AgentResponse:
        """Process a user message and return structured response.

        Args:
            user_message: The user's natural language input.
            tasks: Current pending tasks for context.
            events: Current upcoming events for context.
            gcal_events: Current Google Calendar events for context.

        Returns:
            AgentResponse with action type, parameters, and message.
        """
        system_prompt = self.build_system_prompt(
            tasks=tasks, events=events, gcal_events=gcal_events
        )

        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=1024,
                system=system_prompt,
                messages=[{"role": "user", "content": user_message}],
            )

            raw_response = response.content[0].text
            return self._parse_response(raw_response)

        except Exception as e:
            logger.error(f"Claude API error: {e}")
            return AgentResponse(
                action=ActionType.QUERY,
                params={},
                message="Sorry, I'm having trouble processing that right now. Try again in a moment?",
                raw_response=str(e),
            )

    def _parse_response(self, raw_response: str) -> AgentResponse:
        """Parse Claude's JSON response into structured format.

        Args:
            raw_response: Raw text response from Claude.

        Returns:
            Parsed AgentResponse.
        """
        try:
            # Try to extract JSON from the response
            response_text = raw_response.strip()

            # Handle case where response might have markdown code blocks
            if response_text.startswith("```"):
                lines = response_text.split("\n")
                json_lines = []
                in_json = False
                for line in lines:
                    if line.startswith("```") and not in_json:
                        in_json = True
                        continue
                    elif line.startswith("```") and in_json:
                        break
                    elif in_json:
                        json_lines.append(line)
                response_text = "\n".join(json_lines)

            data = json.loads(response_text)

            action_str = data.get("action", "QUERY")
            try:
                action = ActionType(action_str)
            except ValueError:
                action = ActionType.QUERY

            params = data.get("params", {})
            message = data.get("message", "I processed your request.")

            return AgentResponse(
                action=action,
                params=params,
                message=message,
                raw_response=raw_response,
            )

        except json.JSONDecodeError as e:
            logger.warning(f"Failed to parse Claude response as JSON: {e}")
            # If parsing fails, treat it as a query response with the raw text
            return AgentResponse(
                action=ActionType.QUERY,
                params={},
                message=raw_response,
                raw_response=raw_response,
            )


async def create_agent() -> ClaudeAgent:
    """Create and return a configured ClaudeAgent instance.

    Returns:
        Configured ClaudeAgent.
    """
    return ClaudeAgent()
