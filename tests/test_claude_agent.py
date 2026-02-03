"""Tests for the Claude agent module."""

import json
from unittest.mock import MagicMock, patch

import pytest

from src.claude_agent import ActionType, AgentResponse, ClaudeAgent


class TestClaudeAgent:
    """Tests for the ClaudeAgent class."""

    def test_init(self):
        """Test agent initialization."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")
            assert agent.model == "claude-sonnet-4-20250514"

    def test_build_system_prompt_empty_context(self):
        """Test building system prompt with no context."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")
            prompt = agent.build_system_prompt(
                current_time="Mon Jan 20 at 2pm",
                tasks=[],
                events=[],
                gcal_events=[],
            )

            assert "Dharvis" in prompt
            assert "Mon Jan 20 at 2pm" in prompt
            assert "No pending tasks" in prompt
            assert "No scheduled events" in prompt

    def test_build_system_prompt_with_tasks(self):
        """Test building system prompt with tasks."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")
            tasks = [
                {
                    "id": 1,
                    "title": "Math Homework",
                    "deadline": "2024-01-20T12:00:00-06:00",
                    "priority": "high",
                    "status": "pending",
                },
            ]
            prompt = agent.build_system_prompt(tasks=tasks)

            assert "Math Homework" in prompt
            assert "!!!" in prompt  # High priority marker

    def test_build_system_prompt_with_events(self):
        """Test building system prompt with events."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")
            events = [
                {
                    "id": 1,
                    "title": "Team Meeting",
                    "start_time": "2024-01-20T14:00:00-06:00",
                    "location": "Conference Room",
                    "source": "bot",
                },
            ]
            prompt = agent.build_system_prompt(events=events)

            assert "Team Meeting" in prompt
            assert "Conference Room" in prompt

    def test_build_system_prompt_with_gcal_events(self):
        """Test building system prompt with Google Calendar events."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")
            gcal_events = [
                {
                    "id": "gcal-123",
                    "title": "Doctor Appointment",
                    "start_time": "2024-01-20T10:00:00-06:00",
                    "source": "gcal",
                },
            ]
            prompt = agent.build_system_prompt(gcal_events=gcal_events)

            assert "Doctor Appointment" in prompt
            assert "[gcal]" in prompt


class TestResponseParsing:
    """Tests for response parsing."""

    def test_parse_valid_json_response(self):
        """Test parsing a valid JSON response."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")

            raw_response = json.dumps({
                "action": "ADD_TASK",
                "params": {
                    "title": "Test Task",
                    "deadline": "2024-01-20T12:00:00-06:00",
                    "priority": "medium",
                },
                "message": "Got it - task added!",
            })

            result = agent._parse_response(raw_response)

            assert result.action == ActionType.ADD_TASK
            assert result.params["title"] == "Test Task"
            assert result.message == "Got it - task added!"

    def test_parse_query_response(self):
        """Test parsing a QUERY response."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")

            raw_response = json.dumps({
                "action": "QUERY",
                "params": {},
                "message": "You have a meeting at 2pm today.",
            })

            result = agent._parse_response(raw_response)

            assert result.action == ActionType.QUERY
            assert result.params == {}
            assert "meeting at 2pm" in result.message

    def test_parse_markdown_wrapped_json(self):
        """Test parsing JSON wrapped in markdown code blocks."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")

            raw_response = """```json
{
    "action": "COMPLETE_TASK",
    "params": {"task_title": "homework"},
    "message": "Marked as done!"
}
```"""

            result = agent._parse_response(raw_response)

            assert result.action == ActionType.COMPLETE_TASK
            assert result.params["task_title"] == "homework"

    def test_parse_invalid_json_fallback(self):
        """Test that invalid JSON falls back to QUERY with raw text."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")

            raw_response = "I couldn't understand that. Can you rephrase?"

            result = agent._parse_response(raw_response)

            assert result.action == ActionType.QUERY
            assert result.message == raw_response

    def test_parse_unknown_action_fallback(self):
        """Test that unknown action types fall back to QUERY."""
        with patch("src.claude_agent.Anthropic"):
            agent = ClaudeAgent(api_key="test-key")

            raw_response = json.dumps({
                "action": "UNKNOWN_ACTION",
                "params": {},
                "message": "Some message",
            })

            result = agent._parse_response(raw_response)

            assert result.action == ActionType.QUERY


class TestProcessMessage:
    """Tests for message processing."""

    def test_process_message_success(self):
        """Test successful message processing."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(
                text=json.dumps({
                    "action": "ADD_TASK",
                    "params": {"title": "Test", "deadline": "2024-01-20T12:00:00-06:00"},
                    "message": "Added task!",
                })
            )
        ]
        mock_client.messages.create.return_value = mock_response

        with patch("src.claude_agent.Anthropic", return_value=mock_client):
            agent = ClaudeAgent(api_key="test-key")
            result = agent.process_message("add task test by friday")

            assert result.action == ActionType.ADD_TASK
            assert result.message == "Added task!"

    def test_process_message_api_error(self):
        """Test handling of API errors."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = Exception("API Error")

        with patch("src.claude_agent.Anthropic", return_value=mock_client):
            agent = ClaudeAgent(api_key="test-key")
            result = agent.process_message("test message")

            assert result.action == ActionType.QUERY
            assert "trouble processing" in result.message.lower()


class TestActionTypes:
    """Tests for ActionType enum."""

    def test_all_action_types_exist(self):
        """Test that all expected action types exist."""
        expected_actions = [
            "ADD_TASK",
            "ADD_EVENT",
            "COMPLETE_TASK",
            "DELETE_TASK",
            "DELETE_EVENT",
            "MODIFY_TASK",
            "MODIFY_EVENT",
            "QUERY",
        ]

        for action in expected_actions:
            assert hasattr(ActionType, action)
            assert ActionType[action].value == action


class TestAgentResponse:
    """Tests for AgentResponse dataclass."""

    def test_agent_response_creation(self):
        """Test creating an AgentResponse."""
        response = AgentResponse(
            action=ActionType.ADD_TASK,
            params={"title": "Test"},
            message="Task added!",
            raw_response='{"action": "ADD_TASK"}',
        )

        assert response.action == ActionType.ADD_TASK
        assert response.params == {"title": "Test"}
        assert response.message == "Task added!"
        assert response.raw_response == '{"action": "ADD_TASK"}'
