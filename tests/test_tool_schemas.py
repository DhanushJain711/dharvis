"""Contract checks for strict OpenAI tool schemas."""

from src.tools import TOOLS, TOOLS_BY_NAME


def _assert_strict_object(schema: dict) -> None:
    assert "uniqueItems" not in schema
    if schema.get("type") == "object":
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
        for child in schema["properties"].values():
            _assert_strict_object(child)
    if schema.get("type") == "array":
        _assert_strict_object(schema["items"])


def test_all_tools_are_strict_recursive_closed_schemas() -> None:
    assert len(TOOLS) == 24
    for tool in TOOLS:
        function = tool["function"]
        assert function["strict"] is True
        _assert_strict_object(function["parameters"])


def test_batch_create_and_required_schedule_reasoning() -> None:
    assert TOOLS_BY_NAME["add_task"]["function"]["parameters"]["properties"]["tasks"]["type"] == "array"
    assert TOOLS_BY_NAME["add_event"]["function"]["parameters"]["properties"]["events"]["type"] == "array"
    schedule = TOOLS_BY_NAME["schedule_task"]["function"]["parameters"]
    assert "reasoning" in schedule["required"]
    assert "one-sentence" in schedule["properties"]["reasoning"]["description"]


def test_reminder_tools_are_separate_from_calendar_scheduling() -> None:
    add = TOOLS_BY_NAME["add_reminder"]["function"]
    assert add["parameters"]["properties"]["reminders"]["type"] == "array"
    assert "never becomes a task, Google Calendar event, free/busy block" in add["description"]

    update = TOOLS_BY_NAME["update_reminder"]["function"]["parameters"]
    assert update["properties"]["message"]["type"] == ["string", "null"]
    assert update["properties"]["remind_at"]["type"] == ["string", "null"]

    query = TOOLS_BY_NAME["query_reminders"]["function"]["parameters"]
    assert set(query["properties"]["status"]["enum"]) == {
        "pending", "delivered", "cancelled", None,
    }
