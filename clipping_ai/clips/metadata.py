"""Non-blocking publishing metadata generation for rendered clips."""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from clipping_ai.clips.config import ClipConfig
from clipping_ai.clips.models import ClipJob, ClipMetadataStatus, ClipStatus
from clipping_ai.clips.store import ClipJobStore
from clipping_ai.errors import AIError, ErrorCategory
from clipping_ai.schemas import GeneratedMetadataBatch, GeneratedMetadataItem
from clipping_ai.video.service import VideoService

if TYPE_CHECKING:
    from clipping_ai.manager import ProviderManager

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?؟])\s+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "because",
    "but",
    "by",
    "for",
    "from",
    "has",
    "have",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "was",
    "were",
    "with",
    "you",
    "your",
    "في",
    "من",
    "على",
    "إلى",
    "عن",
    "هذا",
    "هذه",
    "ذلك",
    "التي",
    "الذي",
    "كان",
    "هو",
    "هي",
    "مع",
    "كل",
    "بعد",
    "قبل",
    "ثم",
    "أو",
}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _clean_hashtag(value: str) -> str:
    token = str(value).strip().lstrip("#")
    token = re.sub(r"[^\w]+", "", token, flags=re.UNICODE)
    return token[:40]


def _trim(value: str, max_chars: int) -> str:
    text = " ".join(value.split()).strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rstrip()
    if " " in clipped:
        clipped = clipped.rsplit(" ", 1)[0]
    return clipped.rstrip(" ,،.;؛") + "…"


def _first_sentence(text: str, *, max_chars: int) -> str:
    clean = " ".join(text.split()).strip()
    if not clean:
        return ""
    sentence = _SENTENCE_SPLIT_RE.split(clean, maxsplit=1)[0]
    if len(sentence) > max_chars:
        return _trim(sentence, max_chars)
    return sentence.strip()


def _sentence_summary(text: str, *, max_chars: int, sentences: int = 2) -> str:
    clean = " ".join(text.split()).strip()
    if not clean:
        return ""
    parts = _SENTENCE_SPLIT_RE.split(clean)
    return _trim(" ".join(parts[:sentences]), max_chars)


def _detect_language(text: str, fallback: str) -> str:
    if fallback and fallback != "und":
        return fallback
    if re.search(r"[\u0600-\u06FF]", text):
        return "ar"
    if re.search(r"[A-Za-z]", text):
        return "en"
    return "und"


def _local_metadata(
    payload: dict[str, Any], config: ClipConfig
) -> tuple[str, str, list[str], str]:
    text = str(payload.get("transcript") or "").strip()
    highlight = payload.get("highlight") or {}
    source_title = str(payload.get("source_title") or "").strip()
    language = _detect_language(
        text, str(payload.get("language") or payload.get("source_language") or "und")
    )
    title = (
        str(highlight.get("title") or "").strip()
        or _first_sentence(text, max_chars=config.metadata_title_max_chars)
        or source_title
        or "Clip"
    )
    title = _trim(title, config.metadata_title_max_chars)
    description = (
        str(highlight.get("description") or "").strip()
        or _sentence_summary(text, max_chars=config.metadata_description_max_chars)
        or title
    )
    description = _trim(description, config.metadata_description_max_chars)

    candidates: list[Any] = []
    category = str(highlight.get("category") or "").strip()
    if category and category.casefold() not in {"unknown", "other", "general", "none"}:
        candidates.append(category)
    for word in _WORD_RE.findall(f"{title} {description} {text}"):
        if len(word) >= 3 and word.casefold() not in _STOPWORDS:
            candidates.append(word)
    hashtags = _normalise_hashtags(
        candidates,
        title=title,
        description=description,
        language=language,
        config=config,
        pad=False,
    )
    if len(hashtags) < config.metadata_min_hashtags:
        for word in _WORD_RE.findall(source_title):
            if len(word) >= 3 and word.casefold() not in _STOPWORDS:
                hashtags = _normalise_hashtags(
                    [*hashtags, word],
                    title=title,
                    description=description,
                    language=language,
                    config=config,
                    pad=False,
                )
                if len(hashtags) >= config.metadata_min_hashtags:
                    break
    if not hashtags:
        hashtags = _normalise_hashtags(
            [],
            title=title,
            description=description,
            language=language,
            config=config,
            pad=True,
        )
    return title, description, hashtags, language


def _normalise_hashtags(
    raw: Any,
    *,
    title: str,
    description: str,
    language: str,
    config: ClipConfig,
    pad: bool = True,
) -> list[str]:
    values: list[Any]
    if isinstance(raw, str):
        values = re.split(r"[\s,،]+", raw)
    elif isinstance(raw, list):
        values = raw
    else:
        values = []
    hashtags: list[str] = []
    seen: set[str] = set()
    for value in values:
        token = _clean_hashtag(value)
        key = token.casefold()
        if not token or key in seen:
            continue
        seen.add(key)
        hashtags.append(f"#{token}")
        if len(hashtags) >= config.metadata_max_hashtags:
            break

    if pad and len(hashtags) < config.metadata_min_hashtags:
        words = [
            word
            for word in _WORD_RE.findall(f"{title} {description}")
            if len(word) >= 3 and word.casefold() not in _STOPWORDS
        ]
        for word in words:
            token = _clean_hashtag(word)
            key = token.casefold()
            if not token or key in seen:
                continue
            seen.add(key)
            hashtags.append(f"#{token}")
            if len(hashtags) >= config.metadata_min_hashtags:
                break

    fallback = ["مقاطع", "شورتس"] if language.lower().startswith("ar") else ["shorts", "clips"]
    for token in fallback if pad else []:
        if len(hashtags) >= config.metadata_min_hashtags:
            break
        key = token.casefold()
        if key in seen:
            continue
        seen.add(key)
        hashtags.append(f"#{token}")

    return hashtags[: config.metadata_max_hashtags]


@dataclass(slots=True)
class _BatchEntry:
    job: ClipJob
    text: str
    context: str
    payload: dict[str, Any]


class ClipMetadataService:
    def __init__(
        self,
        store: ClipJobStore,
        video: VideoService,
        manager: ProviderManager | None,
        config: ClipConfig,
    ) -> None:
        self.store = store
        self.video = video
        self.manager = manager
        self.config = config
        self._lock = threading.Lock()

    def _clip_context(self, job: ClipJob) -> tuple[str, str, dict[str, Any]]:
        source = self.video.store.get(job.source_job_id)
        transcript = source.transcript if source is not None else None
        segments = []
        before: list[str] = []
        after: list[str] = []
        if transcript is not None:
            for segment in transcript.segments:
                if segment.end > job.start_time and segment.start < job.end_time:
                    segments.append(segment)
            for segment in transcript.segments:
                if segment.end <= job.start_time:
                    before.append(segment.text.strip())
                elif segment.start >= job.end_time:
                    after.append(segment.text.strip())
        text = " ".join(segment.text.strip() for segment in segments).strip()
        highlight = None
        if (
            source is not None
            and source.highlight_result is not None
            and job.highlight_index is not None
            and 0 <= job.highlight_index < len(source.highlight_result.highlights)
        ):
            highlight = source.highlight_result.highlights[job.highlight_index]
        if not text and highlight is not None:
            text = " ".join(
                part
                for part in (
                    highlight.title,
                    highlight.description,
                    highlight.selection_reason,
                )
                if part
            ).strip()
        language = (
            (transcript.language if transcript is not None else None)
            or (source.highlight_result.language if source and source.highlight_result else None)
            or "und"
        )
        payload = {
            "clip_id": job.id,
            "duration_seconds": round(job.duration, 3),
            "start_time": round(job.start_time, 3),
            "end_time": round(job.end_time, 3),
            "language": language,
            "source_title": source.display_title() if source is not None else None,
            "source_language": (
                transcript.language if transcript is not None else None
            ),
            "highlight": (
                {
                    "title": highlight.title,
                    "description": highlight.description,
                    "category": highlight.category,
                }
                if highlight is not None
                else None
            ),
            "transcript": text,
        }
        context = " ".join(
            part
            for part in (
                "Before: " + " ".join(before[-2:]) if before else "",
                "After: " + " ".join(after[:2]) if after else "",
            )
            if part
        )
        return text, context, payload

    def _fail(self, clip_id: str, message: str) -> ClipJob:
        return self.store.update(
            clip_id,
            metadata_status=ClipMetadataStatus.FAILED,
            metadata_error=message[:1000],
            metadata_at=_now(),
        )

    def _defer_quota(self, clip_id: str, message: str) -> ClipJob:
        return self.store.update(
            clip_id,
            metadata_status=ClipMetadataStatus.DEFERRED_QUOTA,
            metadata_error=message[:1000],
            metadata_at=_now(),
        )

    def _write_allowed(self, job: ClipJob, *, force: bool) -> bool:
        if job.deleted_at is not None:
            return False
        if job.metadata_status == ClipMetadataStatus.MANUALLY_EDITED and not force:
            return False
        protected = {ClipMetadataStatus.GENERATED, ClipMetadataStatus.LOCAL_FALLBACK}
        return force or job.metadata_status not in protected

    def _apply_ai(
        self,
        job: ClipJob,
        item: GeneratedMetadataItem | dict[str, Any],
        *,
        provider: str | None,
        model: str | None,
        force: bool,
    ) -> ClipJob:
        data = item.model_dump() if isinstance(item, GeneratedMetadataItem) else item
        current = self.store.require(job.id)
        if not self._write_allowed(current, force=force):
            return current
        title = str(data.get("title") or "").strip()
        if not title:
            return self._apply_local(
                current,
                job_payload=self._clip_context(current)[2],
                reason="AI metadata contained an empty title; used local metadata.",
                force=force,
            )
        description = str(data.get("description") or "").strip()
        language = str(data.get("language") or current.metadata_language or "und").strip()
        hashtags = _normalise_hashtags(
            data.get("hashtags"),
            title=title,
            description=description,
            language=language,
            config=self.config,
        )
        return self.store.update(
            current.id,
            title=title[: self.config.metadata_title_max_chars],
            description=description[: self.config.metadata_description_max_chars],
            hashtags=hashtags,
            metadata_language=language,
            metadata_status=ClipMetadataStatus.GENERATED,
            metadata_error=None,
            metadata_provider=provider,
            metadata_model=model,
            metadata_at=_now(),
        )

    def _apply_local(
        self,
        job: ClipJob,
        *,
        job_payload: dict[str, Any] | None = None,
        reason: str,
        force: bool = False,
    ) -> ClipJob:
        current = self.store.require(job.id)
        if not self._write_allowed(current, force=force):
            return current
        payload = job_payload or self._clip_context(current)[2]
        title, description, hashtags, language = _local_metadata(payload, self.config)
        if not title:
            return self._fail(current.id, reason)
        return self.store.update(
            current.id,
            title=title,
            description=description,
            hashtags=hashtags,
            metadata_language=language,
            metadata_status=ClipMetadataStatus.LOCAL_FALLBACK,
            metadata_error=reason[:1000],
            metadata_provider="local",
            metadata_model="deterministic",
            metadata_at=_now(),
        )

    def generate(
        self,
        clip_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        force: bool = False,
    ) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before generating metadata.",
                retryable=False,
            )
        if job.status != ClipStatus.READY:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Metadata is available after the clip finishes rendering.",
                retryable=False,
            )
        if job.metadata_status == ClipMetadataStatus.MANUALLY_EDITED and not force:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "This clip has manually edited metadata. Confirm regeneration to overwrite it.",
                retryable=False,
            )
        if job.metadata_status == ClipMetadataStatus.GENERATED and not force:
            return job
        if self.manager is None:
            return self._fail(clip_id, "Metadata generation is not configured.")

        with self._lock:
            job = self.store.require(clip_id)
            if job.deleted_at is not None:
                return job
            text, context, payload = self._clip_context(job)
            if not text:
                return self._fail(
                    clip_id,
                    "No transcript text is available for this clip range.",
                )
            self.store.update(clip_id, metadata_status=ClipMetadataStatus.PENDING)
            try:
                result = self.manager.analyze(
                    "title_generation",
                    text=text,
                    context=context or None,
                    payload=payload,
                    provider=provider,
                    model=model,
                    instructions=(
                        "Write in the dominant language of the supplied transcript. "
                        "Return an accurate title, a short description, and 2-4 relevant "
                        "hashtags without spaces. Do not invent facts."
                    ),
                )
            except AIError as exc:
                logger.warning("Metadata generation failed for %s: %s", clip_id, exc)
                return self._apply_local(
                    job,
                    job_payload=payload,
                    reason=f"AI metadata unavailable; used local fallback. {exc}",
                    force=force,
                )
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Metadata generation failed for %s: %s", clip_id, exc)
                return self._fail(clip_id, f"Unexpected metadata failure: {exc}")

            data = result.get("data")
            if not isinstance(data, dict):
                return self._apply_local(
                    job,
                    job_payload=payload,
                    reason="Metadata provider returned no usable data; used local fallback.",
                    force=force,
                )
            return self._apply_ai(
                job,
                data,
                provider=str(result.get("provider") or provider or "unknown"),
                model=str(result.get("model") or model or "unknown"),
                force=force,
            )

    def generate_batch(
        self,
        source_job_id: str,
        *,
        provider: str | None = None,
        model: str | None = None,
        force: bool = False,
        retry_failed: bool = False,
    ) -> list[ClipJob]:
        if self.manager is None:
            return []
        jobs = self.store.list(source_job_id=source_job_id, limit=500)
        eligible_statuses = {
            None,
            ClipMetadataStatus.PENDING,
            ClipMetadataStatus.DEFERRED_QUOTA,
        }
        if retry_failed:
            eligible_statuses.add(ClipMetadataStatus.FAILED)
        selected = [
            job
            for job in jobs
            if job.deleted_at is None
            and (force or job.metadata_status in eligible_statuses)
        ]
        if not selected:
            return []

        results: list[ClipJob] = []
        entries: list[_BatchEntry] = []
        for job in selected:
            text, context, payload = self._clip_context(job)
            if not text:
                results.append(
                    self._fail(
                        job.id,
                        "No transcript text is available for this clip range.",
                    )
                )
                continue
            entries.append(_BatchEntry(job=job, text=text, context=context, payload=payload))

        batch_size = max(1, self.config.metadata_batch_size)
        with self._lock:
            for start in range(0, len(entries), batch_size):
                chunk = entries[start : start + batch_size]
                results.extend(
                    self._generate_chunk(
                        chunk,
                        provider=provider,
                        model=model,
                        force=force,
                    )
                )
        return results

    def _generate_chunk(
        self,
        entries: list[_BatchEntry],
        *,
        provider: str | None,
        model: str | None,
        force: bool,
    ) -> list[ClipJob]:
        if not entries:
            return []
        if self.manager is None:
            return [
                self._fail(entry.job.id, "Metadata generation is not configured.")
                for entry in entries
            ]
        for entry in entries:
            self.store.update(entry.job.id, metadata_status=ClipMetadataStatus.PENDING)
        text = " ".join(entry.text for entry in entries)[:12000]
        context = "\n".join(
            f"Clip {entry.job.id}: {entry.context}"
            for entry in entries
            if entry.context
        )
        try:
            result = self.manager.analyze(
                "title_generation",
                text=text,
                context=context or None,
                payload={"clips": [entry.payload for entry in entries]},
                provider=provider,
                model=model,
                schema=GeneratedMetadataBatch,
                instructions=(
                    "Return exactly one item per supplied clip_id. Write each title, "
                    "description, and 2-4 hashtags in that clip's dominant language. "
                    "Use only the supplied transcript and highlight context. Never "
                    "assign metadata from one clip to another or invent facts."
                ),
            )
        except AIError as exc:
            logger.warning("Batch metadata failed for %d clips: %s", len(entries), exc)
            if len(entries) > 1:
                middle = len(entries) // 2
                return self._generate_chunk(
                    entries[:middle],
                    provider=provider,
                    model=model,
                    force=force,
                ) + self._generate_chunk(
                    entries[middle:],
                    provider=provider,
                    model=model,
                    force=force,
                )
            entry = entries[0]
            return [
                self._apply_local(
                    entry.job,
                    job_payload=entry.payload,
                    reason=f"AI metadata unavailable; used local fallback. {exc}",
                    force=force,
                )
            ]
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("Unexpected batch metadata failure: %s", exc)
            if len(entries) > 1:
                middle = len(entries) // 2
                return self._generate_chunk(
                    entries[:middle],
                    provider=provider,
                    model=model,
                    force=force,
                ) + self._generate_chunk(
                    entries[middle:],
                    provider=provider,
                    model=model,
                    force=force,
                )
            return [
                self._fail(
                    entries[0].job.id,
                    f"Unexpected metadata failure: {exc}",
                )
            ]

        data = result.get("data")
        items = data.get("items") if isinstance(data, dict) else None
        if not isinstance(items, list):
            return [
                self._apply_local(
                    entry.job,
                    job_payload=entry.payload,
                    reason="Metadata provider returned no usable data; used local fallback.",
                    force=force,
                )
                for entry in entries
            ]

        by_id: dict[str, dict[str, Any]] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            clip_id = str(item.get("clip_id") or "").strip()
            if clip_id and clip_id not in by_id:
                by_id[clip_id] = item

        applied: list[ClipJob] = []
        rendered_provider = str(result.get("provider") or provider or "unknown")
        rendered_model = str(result.get("model") or model or "unknown")
        for entry in entries:
            item = by_id.get(entry.job.id)
            if item is None:
                applied.append(
                    self._apply_local(
                        entry.job,
                        job_payload=entry.payload,
                        reason="Batch response omitted this clip; used local fallback.",
                        force=force,
                    )
                )
                continue
            applied.append(
                self._apply_ai(
                    entry.job,
                    item,
                    provider=rendered_provider,
                    model=rendered_model,
                    force=force,
                )
            )
        return applied

    def update(
        self,
        clip_id: str,
        *,
        title: str,
        description: str,
        hashtags: list[str] | str | None,
        language: str | None = None,
    ) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before editing metadata.",
                retryable=False,
            )
        clean_title = title.strip()
        if not clean_title:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "A title is required.",
                retryable=False,
            )
        clean_description = description.strip()
        resolved_language = (
            (language or job.metadata_language or "und").strip() or "und"
        )
        normalised = _normalise_hashtags(
            hashtags or [],
            title=clean_title,
            description=clean_description,
            language=resolved_language,
            config=self.config,
            pad=False,
        )
        return self.store.update(
            clip_id,
            title=clean_title[: self.config.metadata_title_max_chars],
            description=clean_description[: self.config.metadata_description_max_chars],
            hashtags=normalised,
            metadata_language=resolved_language,
            metadata_status=ClipMetadataStatus.MANUALLY_EDITED,
            metadata_error=None,
            metadata_edited_at=_now(),
            metadata_at=_now(),
        )

    def set_published(self, clip_id: str, published: bool) -> ClipJob:
        job = self.store.require(clip_id)
        if job.deleted_at is not None:
            raise AIError(
                ErrorCategory.INVALID_REQUEST,
                "Restore this clip before changing publishing status.",
                retryable=False,
            )
        if published == job.published:
            return job
        return self.store.update(
            clip_id,
            published=published,
            published_at=_now() if published else None,
        )
