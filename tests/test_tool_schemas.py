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


def test_event_color_contract_uses_only_google_event_palette_ids() -> None:
    add_item = TOOLS_BY_NAME["add_event"]["function"]["parameters"]["properties"]["events"]["items"]
    add_color = add_item["properties"]["color_id"]
    assert "color_id" in add_item["required"]
    assert add_color["type"] == ["string", "null"]
    assert add_color["enum"] == [str(value) for value in range(1, 12)] + [None]
    assert "calendar_color_id" in add_color["description"]
    assert "never copy" in add_color["description"]

    update = TOOLS_BY_NAME["update_event"]["function"]["parameters"]
    update_color = update["properties"]["color_id"]
    assert update_color["enum"] == add_color["enum"]
    assert "null to leave" in update_color["description"]
    assert "color_id" in update["properties"]["clear_fields"]["items"]["enum"]
    assert "inherit" in update["properties"]["clear_fields"]["description"]

    palette = add_color["description"]
    for label in (
        "1 lavender", "2 sage", "3 grape", "4 flamingo", "5 banana",
        "6 tangerine", "7 peacock", "8 graphite", "9 blueberry",
        "10 basil", "11 tomato",
    ):
        assert label in palette
    query_description = TOOLS_BY_NAME["query_schedule"]["function"]["description"]
    assert "metadata.color_id" in query_description
    assert "metadata.calendar_color_id" in query_description
    assert "must never be passed" in query_description


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
