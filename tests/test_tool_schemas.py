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
    assert len(TOOLS) == 20
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
