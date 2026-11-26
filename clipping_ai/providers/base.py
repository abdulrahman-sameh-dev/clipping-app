"""Provider interfaces. Business logic depends on these, not on SDKs."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from clipping_ai.audio import AudioChunk
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.registry import ModelEntry, ProviderConfig
from clipping_ai.schemas import TranscriptResult


@dataclass
class TextCompletion:
    text: str
    provider: str
    model: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost: float | None = None
    latency_ms: int | None = None
    raw: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ConnectionResult:
    ok: bool
    provider: str
    model: str | None
    message: str
    latency_ms: int | None = None
    details: dict[str, Any] = field(default_factory=dict)


class BaseTextProvider(ABC):
    provider_id: str

    def __init__(self, provider_id: str, config: ProviderConfig) -> None:
        self.provider_id = provider_id
        self.config = config

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """Return (usable, reason). A missing key means this provider is unavailable."""

    @abstractmethod
    def complete(
        self,
        entry: ModelEntry,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.2,
        max_tokens: int | None = None,
        response_format: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> TextCompletion: ...

    @abstractmethod
    def test_connection(self, entry: ModelEntry) -> ConnectionResult: ...


class BaseSpeechProvider(ABC):
    provider_id: str

    def __init__(self, provider_id: str, config: ProviderConfig) -> None:
        self.provider_id = provider_id
        self.config = config

    @abstractmethod
    def available(self) -> tuple[bool, str]: ...

    @abstractmethod
    def transcribe(
        self,
        entry: ModelEntry,
        audio_path: Path,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> TranscriptResult: ...

    @property
    def supports_prepared_chunks(self) -> bool:
        """Whether ``transcribe_chunk`` can process an externally prepared chunk."""
        return False

    def transcribe_chunk(
        self,
        entry: ModelEntry,
        chunk: AudioChunk,
        *,
        language: str | None = None,
        word_timestamps: bool = True,
        prompt: str | None = None,
        timeout: float | None = None,
    ) -> TranscriptResult:
        """Transcribe one prepared chunk, applying its global offset.

        Providers that cannot accept a caller-owned chunk keep using
        ``transcribe`` and return ``supports_prepared_chunks = False``.
        """
        raise AIError(
            ErrorCategory.UNSUPPORTED_CAPABILITY,
            f"Provider '{self.provider_id}' does not support prepared audio chunks.",
            provider=self.provider_id,
            retryable=False,
            details={"reason": "prepared_chunks_unsupported"},
        )

    @abstractmethod
    def test_connection(self, entry: ModelEntry | None = None) -> ConnectionResult: ...


_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"gsk_[A-Za-z0-9_\-]{8,}"),
    re.compile(r"AIza[A-Za-z0-9_\-]{20,}"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"),
    re.compile(r"(?i)(api[_\- ]?key[\"']?\s*[:=]\s*)[\"']?[A-Za-z0-9._\-]{8,}"),
]


def sanitize_message(message: str) -> str:
    """Defensive redaction for provider error strings before logging/returning."""
    cleaned = message
    for pattern in _SECRET_PATTERNS:
        cleaned = pattern.sub("***redacted***", cleaned)
    return cleaned
