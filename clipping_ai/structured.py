"""Prompt construction, controlled JSON extraction and Pydantic validation."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ValidationError

from clipping_ai.errors import AIError, ErrorCategory

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)

TASK_INSTRUCTIONS: dict[str, str] = {
    "content_analysis": (
        "Analyse the supplied transcript. Summarise it, list the key points and topics, "
        "classify the sentiment, and identify the content type. Use the transcript's language."
    ),
    "highlight_detection": (
        "Identify the strongest complete moments in the transcript. For each highlight return "
        "start_time and end_time in seconds, a title, summary, hook, reasoning, a score from "
        "0 to 100, a category, and the exact transcript excerpt. Prefer complete explanations "
        "over isolated fragments. When adjacent moments belong to the same example or argument, "
        "return one combined range covering the full idea. Do not split a continuous explanation "
        "into multiple highlights, and do not merge unrelated ideas. Duration should follow the "
        "meaning of the moment: a self-contained short statement may stay short, while a longer "
        "explanation may span a minute or more. Candidate windows are hints, not limits."
    ),
    "clip_scoring": (
        "Evaluate the supplied clip transcript. Score hook_strength, context_completeness, "
        "informational_value, emotional_engagement, standalone_quality and overall_score "
        "from 0 to 100."
    ),
    "title_generation": (
        "Generate a compelling but accurate title, a description, hashtags and the language "
        "for the supplied clip. Return all four fields."
    ),
    "description_generation": (
        "Generate a useful description, title, hashtags and language for the supplied clip. "
        "Keep the description factual and based only on the provided content."
    ),
    "translation": (
        "Translate the supplied text into the requested target_language. Preserve meaning, "
        "tone and any timestamps. Return translated_text, source_language, target_language "
        "and optional notes."
    ),
    "caption_refinement": (
        "Refine the supplied captions for readability while preserving their start/end times "
        "and meaning. Return the full captions array and language."
    ),
}


def extract_json(text: str) -> Any:
    """Extract a JSON value from model output, tolerating markdown fences/preamble."""
    if text is None:
        raise ValueError("empty response")
    candidate = text.strip()
    if not candidate:
        raise ValueError("empty response")

    fence = _FENCE_RE.search(candidate)
    if fence:
        candidate = fence.group(1).strip()

    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass

    # Fall back to the first balanced object/array in the text.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = candidate.find(opener)
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            char = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif char == "\\":
                    escape = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    snippet = candidate[start : index + 1]
                    try:
                        return json.loads(snippet)
                    except json.JSONDecodeError:
                        break
    raise ValueError("no valid JSON found in response")


def build_messages(
    task: str,
    payload: dict[str, Any],
    *,
    schema: type[BaseModel] | None = None,
    instructions: str | None = None,
) -> list[dict[str, str]]:
    system = (
        "You are a precise content-analysis assistant for a personal AI clipping studio. "
        "Return exactly one valid JSON value and nothing else. Do not wrap it in markdown "
        "fences and do not add commentary. Follow the provided JSON Schema exactly."
    )
    parts = [f"Task: {task}", TASK_INSTRUCTIONS.get(task, "")]
    if schema is not None:
        schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False, indent=2)
        parts.append("JSON Schema:\n" + schema_json)
    if instructions:
        parts.append("Additional instructions:\n" + instructions.strip())
    parts.append("Input JSON:\n" + json.dumps(payload, ensure_ascii=False, indent=2))
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": "\n\n".join(part for part in parts if part)},
    ]


def repair_messages(
    messages: list[dict[str, str]],
    bad_output: str,
    error: str,
) -> list[dict[str, str]]:
    return [
        *messages,
        {"role": "assistant", "content": bad_output[:8000]},
        {
            "role": "user",
            "content": (
                "That response was not valid for the required JSON Schema. "
                f"Error: {error}\n"
                "Return ONLY a corrected JSON value that satisfies the schema. "
                "No markdown fences, no explanation."
            ),
        },
    ]


def parse_and_validate(text: str, schema: type[BaseModel]) -> BaseModel:
    try:
        data = extract_json(text)
    except ValueError as exc:
        raise AIError(
            ErrorCategory.INVALID_RESPONSE,
            f"Model output was not valid JSON: {exc}",
            retryable=True,
            details={"raw_excerpt": text[:2000]},
            cause=exc,
        ) from exc
    try:
        return schema.model_validate(data)
    except ValidationError as exc:
        raise AIError(
            ErrorCategory.SCHEMA_VALIDATION_FAILED,
            f"Model output did not match {schema.__name__}: {exc.error_count()} validation error(s).",
            retryable=True,
            details={
                "errors": exc.errors(include_url=False)[:20],
                "raw_excerpt": text[:2000],
            },
            cause=exc,
        ) from exc


def response_format_for(schema: type[BaseModel] | None, supports_json: bool) -> dict[str, Any] | None:
    if schema is None or not supports_json:
        return None
    return {"type": "json_object"}
