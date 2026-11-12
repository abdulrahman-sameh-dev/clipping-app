"""Optional real-provider smoke tests.

Run with:
    CLIPPING_AI_LIVE_TESTS=1 uv run pytest -m live
Keys may be supplied through .env or the environment.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from clipping_ai.errors import AIError
from clipping_ai.manager import ProviderManager
from clipping_ai.settings import Settings

pytestmark = pytest.mark.live

LIVE_ENABLED = os.getenv("CLIPPING_AI_LIVE_TESTS") == "1"


def _has_key(name: str) -> bool:
    return Settings().has_secret(name)


@pytest.mark.skipif(not LIVE_ENABLED, reason="set CLIPPING_AI_LIVE_TESTS=1 to run")
def test_live_gemini_content_analysis():
    if not _has_key("GEMINI_API_KEY"):
        pytest.skip("GEMINI_API_KEY is not set")
    manager = ProviderManager.from_config()
    last_error: AIError | None = None
    result = None
    for model in ("gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-3.8-flash"):
        try:
            result = manager.analyze(
                "content_analysis",
                text="This is a short test transcript about a person learning to cook.",
                model=model,
            )
            break
        except AIError as exc:
            last_error = exc
    if result is None:
        raise last_error or AssertionError("no Gemini model was tested")
    assert result["data"]["summary"]


@pytest.mark.skipif(not LIVE_ENABLED, reason="set CLIPPING_AI_LIVE_TESTS=1 to run")
def test_live_openrouter_content_analysis():
    if not _has_key("OPENROUTER_API_KEY"):
        pytest.skip("OPENROUTER_API_KEY is not set")
    manager = ProviderManager.from_config()
    result = manager.analyze(
        "content_analysis",
        text="This is a short test transcript about a person learning to cook.",
        provider="openrouter",
        model="openrouter-free-router",
    )
    assert result["data"]["summary"]


@pytest.mark.skipif(not LIVE_ENABLED, reason="set CLIPPING_AI_LIVE_TESTS=1 to run")
def test_live_groq_transcription():
    if not _has_key("GROQ_API_KEY"):
        pytest.skip("GROQ_API_KEY is not set")
    audio = os.getenv("CLIPPING_AI_LIVE_AUDIO")
    if not audio or not Path(audio).is_file():
        pytest.skip("set CLIPPING_AI_LIVE_AUDIO to an existing audio file")
    manager = ProviderManager.from_config()
    result = manager.transcribe(audio, provider="groq_speech")
    transcript = result["transcript"]
    assert transcript["text"].strip()
    segments = transcript.get("segments") or []
    assert segments, "Groq transcript did not include usable segment timestamps"
    assert all(segment["end"] >= segment["start"] for segment in segments)
    assert any(segment["end"] > segment["start"] for segment in segments)
