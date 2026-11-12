from __future__ import annotations

import pytest
from pydantic import BaseModel

from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.structured import (
    build_messages,
    extract_json,
    parse_and_validate,
    repair_messages,
    response_format_for,
)


class Example(BaseModel):
    value: int


def test_extract_json_handles_plain_json():
    assert extract_json('{"value": 1}') == {"value": 1}


def test_extract_json_handles_markdown_fence():
    assert extract_json('```json\n{"value": 2}\n```') == {"value": 2}


def test_extract_json_handles_preamble_and_nested_braces():
    text = 'Here you go: {"value": 3, "nested": {"x": "}"}} trailing'
    assert extract_json(text) == {"value": 3, "nested": {"x": "}"}}


def test_extract_json_rejects_empty_or_invalid():
    with pytest.raises(ValueError):
        extract_json("")
    with pytest.raises(ValueError):
        extract_json("not json")


def test_parse_and_validate_returns_model():
    assert parse_and_validate('{"value": 4}', Example).value == 4


def test_parse_and_validate_maps_invalid_json():
    with pytest.raises(AIError) as exc:
        parse_and_validate("nope", Example)
    assert exc.value.category == ErrorCategory.INVALID_RESPONSE


def test_parse_and_validate_maps_schema_failure():
    with pytest.raises(AIError) as exc:
        parse_and_validate('{"value": "not-an-int"}', Example)
    assert exc.value.category == ErrorCategory.SCHEMA_VALIDATION_FAILED


def test_build_messages_contains_schema_and_payload():
    messages = build_messages("content_analysis", {"text": "hello"}, schema=Example)
    assert messages[0]["role"] == "system"
    assert "JSON Schema" in messages[1]["content"]
    assert "hello" in messages[1]["content"]


def test_repair_messages_appends_assistant_and_user():
    messages = [{"role": "user", "content": "x"}]
    repaired = repair_messages(messages, "bad", "error")
    assert [item["role"] for item in repaired] == ["user", "assistant", "user"]


def test_response_format_only_when_json_is_supported():
    assert response_format_for(Example, True) == {"type": "json_object"}
    assert response_format_for(Example, False) is None
    assert response_format_for(None, True) is None
