# Configuration

## Files

| File | Purpose |
| --- | --- |
| `config/providers.json` | Provider enablement, key env names, timeouts, throttles, speech limits |
| `config/models.json` | Explicit model allow-list and free-verification metadata |
| `config/routing.json` | Task-to-model priority lists |
| `config/policy.json` | Free-only, fallback, retry, and schema-retry settings |
| `config/video.json` | Video limits, formats, ffmpeg, audio chunking, worker |
| `config/analysis.json` | Transcription defaults and highlight-detection thresholds |
| `config/clips.json` | Clip formats, quality, captions, visual composition, FFmpeg limits, worker |
| `config/skills/` | Optional external skill manifests; data-only and disabled until reviewed |
| `config/local/` | Generated local overrides and skill state; ignored by git |

Configuration is loaded at startup. `POST /api/ai/reload` or restarting the service
reloads it.

## Environment

Copy `.env.example` to `.env`.

```bash
GEMINI_API_KEY=
GROQ_API_KEY=
OPENROUTER_API_KEY=
NVIDIA_API_KEY=
```

Optional settings:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CLIPPING_AI_CONFIG_DIR` | `config` | Config directory |
| `CLIPPING_AI_DATA_DIR` | `data` | SQLite and media data directory; relative values resolve at startup |
| `CLIPPING_AI_HOST` | `127.0.0.1` | API bind host |
| `CLIPPING_AI_PORT` | `8765` | API port |
| `CLIPPING_AI_TIMEOUT_SECONDS` | `120` | Default timeout |
| `CLIPPING_AI_MAX_RETRIES` | `2` | Default same-provider retries |
| `CLIPPING_AI_REQUIRE_VERIFIED_FREE` | unset | Override policy to require verified free models |
| `CLIPPING_AI_WHISPER_CPP_BINARY` | unset | Optional whisper.cpp binary |
| `CLIPPING_AI_WHISPER_CPP_MODEL_PATH` | unset | Optional whisper.cpp model |
| `CLIPPING_AI_LOG_LEVEL` | `INFO` | Log level |

Keep the bind host on loopback unless you intentionally want other machines to
reach the service.

Relative `CLIPPING_AI_CONFIG_DIR` and `CLIPPING_AI_DATA_DIR` values are resolved to
absolute paths once at startup. Clip exports therefore do not depend on the process
working directory; restart the service after changing either path.

## Video configuration

`config/video.json` controls the local video preparation engine. The defaults accept
common containers up to 2 GiB and 6 hours.

```json
{
  "video": {
    "enabled": true,
    "max_upload_bytes": 2147483648,
    "max_duration_seconds": 21600,
    "allowed_extensions": [
      ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v",
      ".mpeg", ".mpg", ".ts", ".flv", ".wmv"
    ],
    "duplicate_detection": true,
    "ffmpeg_binary": "ffmpeg",
    "ffprobe_binary": "ffprobe",
    "metadata_timeout_seconds": 60,
    "download_timeout_seconds": 1800,
    "extract_timeout_seconds": 3600,
    "chunk_timeout_seconds": 300,
    "audio_sample_rate": 16000,
    "audio_channels": 1,
    "audio_codec": "flac",
    "chunk_seconds": 600,
    "chunk_overlap_seconds": 0,
    "yt_dlp_format": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "url_schemes": ["http", "https"],
    "cleanup_temp": true,
    "worker_poll_seconds": 1
  }
}
```

Notes:

- `allowed_extensions` is an allow-list. Unsupported containers are rejected.
- `chunk_overlap_seconds` must be smaller than `chunk_seconds`.
- Audio chunks are written with explicit `offset` and `duration` values so they can
  be merged later.
- `audio_codec` accepts `flac`, `wav`/`pcm_s16le`, `mp3`/`libmp3lame`, `aac`/`m4a`,
  and `opus`/`libopus`.
- Only `http` and `https` URLs are accepted. DRM bypass is not implemented.
- Uploads and URL jobs are stored under `data/videos/jobs/<job_id>/`.
- `data/jobs.db` now contains both the transcription cache and video jobs.

## Analysis configuration

`config/analysis.json` controls transcription normalisation and highlight
suggestions. Defaults:

```json
{
  "transcription": {
    "default_language": null,
    "word_timestamps": true,
    "clamp_timestamps": true,
    "max_transcript_chars": 200000,
    "min_word_probability": 0.0
  },
  "highlights": {
    "enabled": true,
    "min_clip_seconds": 12,
    "max_clip_seconds": 180,
    "target_clip_seconds_min": 30,
    "target_clip_seconds_max": 45,
    "target_duration_tolerance_seconds": 15,
    "max_highlights": 10,
    "candidate_window_seconds": 60,
    "candidate_stride_seconds": 30,
    "min_confidence": 0.25,
    "audio_window_seconds": 0.5,
    "audio_timeout_seconds": 300,
    "audio_event_min_energy": 0.2,
    "audio_peak_delta": 0.12,
    "scene_timeout_seconds": 300,
    "scene_threshold": 0.25,
    "analyze_motion": true,
    "motion_fps": 2,
    "motion_scale_width": 160,
    "motion_score_scale": 24,
    "motion_event_threshold": 0.18,
    "motion_timeout_seconds": 300,
    "silence_dbfs": -45,
    "spike_percentile": 85,
    "event_merge_seconds": 8,
    "pre_roll_seconds": 2,
    "post_roll_seconds": 3,
    "signal_base_score": 0.2,
    "audio_weight": 0.22,
    "peak_weight": 0.12,
    "scene_weight": 0.14,
    "motion_weight": 0.18,
    "silence_transition_weight": 0.08,
    "transcript_signal_weight": 0.25,
    "content_type_signal_penalty": 0.15,
    "dedupe_overlap_ratio": 0.55,
    "semantic_consolidation": true,
    "semantic_gap_seconds": 2,
    "semantic_merge_gap_seconds": 3,
    "semantic_bridge_gap_seconds": 20,
    "semantic_topic_overlap": 0.2,
    "semantic_boundary_context_seconds": 1,
    "semantic_completeness_bonus": 0.05,
    "classification_min_confidence": 0.35,
    "classification_margin": 0.12,
    "align_to_transcript": true,
    "use_llm": true,
    "llm_max_transcript_chars": 24000,
    "llm_max_candidates": 12
  }
}
```

Notes:

- `default_language` is passed to the speech provider when a request omits a language.
- `clamp_timestamps` limits transcript segments and words to the media duration.
- `max_transcript_chars` caps the stored transcript text length; segment and word
  timestamps are not truncated.
- `min_word_probability` filters low-confidence words when providers supply
  probabilities.
- `min_clip_seconds`/`max_clip_seconds` bound suggested clips. The default maximum
  is 180s so a complete explanation is not truncated solely to fit a short-form
  target. Short jobs can still return no highlights.
- `target_clip_seconds_min`/`target_clip_seconds_max` define the preferred 30-45s
  short-form range. This is a soft target: sentence completeness wins, and overly
  long moments are split only at transcript boundaries when they exceed the target
  plus `target_duration_tolerance_seconds`. Set both target values or neither.
- `candidate_stride_seconds` must be less than or equal to
  `candidate_window_seconds`.
- `audio_event_min_energy` and `audio_peak_delta` control sustained audio events and
  sudden peaks; `spike_percentile` adapts the threshold to each recording.
- `analyze_motion` enables lightweight FFmpeg `signalstats` motion sampling.
  `motion_fps`, `motion_scale_width`, and `motion_score_scale` control cost and
  sensitivity; `motion_event_threshold` sets the candidate floor.
- `signal_base_score` and the `*_weight` values combine audio, peak, scene, motion,
  silence-transition, and transcript evidence. `content_type_signal_penalty` reduces
  pure signal candidates for spoken formats.
- `classification_min_confidence` and `classification_margin` gate auto
  classification. Close spoken/action evidence becomes `mixed`; low confidence becomes
  `unknown`. Explicit `content_type` values override auto classification.
- `dedupe_overlap_ratio` controls how much overlap is allowed before a lower-ranked
  candidate is dropped.
- `semantic_consolidation` expands candidates to transcript sentence boundaries and
  merges related fragments when the gap is small or the bridge text shares the same
  topic. `semantic_gap_seconds` controls semantic candidate grouping;
  `semantic_merge_gap_seconds` and `semantic_bridge_gap_seconds` control adjacent and
  bridged merging; `semantic_topic_overlap` controls lexical relatedness;
  `semantic_boundary_context_seconds` allows nearby incomplete sentences to complete
  a thought; `semantic_completeness_bonus` boosts merged highlights. Unrelated nearby
  ideas stay separate, and self-contained short clips are preserved.
- `use_llm` enables optional `highlight_detection` refinement. Set it to `false` for
  deterministic transcript/signal selection.
- `config/local/analysis.json` deep-merges over the base file.

## Clip configuration

`config/clips.json` controls source-preserving clip rendering. Defaults:

```json
{
  "clips": {
    "enabled": true,
    "ffmpeg_binary": "ffmpeg",
    "ffprobe_binary": "ffprobe",
    "formats": {
      "vertical": { "width": 1080, "height": 1920 },
      "square": { "width": 1080, "height": 1080 },
      "landscape": { "width": 1920, "height": 1080 }
    },
    "qualities": {
      "draft": { "crf": 28, "preset": "veryfast", "audio_bitrate": "128k" },
      "standard": { "crf": 23, "preset": "medium", "audio_bitrate": "192k" },
      "high": { "crf": 18, "preset": "slow", "audio_bitrate": "256k" }
    },
    "default_formats": ["vertical"],
    "default_quality": "standard",
    "default_fit": "crop",
    "default_fps": 30,
    "min_clip_seconds": 1,
    "max_clip_seconds": 300,
    "render_timeout_seconds": 1800,
    "ffprobe_timeout_seconds": 60,
    "min_free_disk_bytes": 1000000000,
    "x264_threads": 4,
    "pixel_format": "yuv420p",
    "audio_codec": "aac",
    "faststart": true,
    "keep_failed_artifacts": false,
    "worker_poll_seconds": 1,
    "blur_sigma": 20,
    "blur_steps": 2,
    "caption_font_arabic": "Noto Sans Arabic",
    "caption_font_latin": "Noto Sans",
    "caption_font_size_ratio": 0.062,
    "caption_margin_v_ratio": 0.055,
    "caption_outline": 3,
    "caption_shadow": 1,
    "caption_border_style": 3,
    "caption_box_padding_ratio": 0.008,
    "caption_encoding": -1,
    "caption_max_chars_per_line": 30,
    "caption_max_lines": 2,
    "caption_min_duration": 0.6,
    "caption_max_duration": 6,
    "caption_primary_colour": "&H00FFFFFF",
    "caption_outline_colour": "&H00000000",
    "caption_back_colour": "&H50000000",
    "caption_accent_colour": "&H0000E5FF",
    "caption_emphasis_colour": "&H0000D7FF",
    "caption_emphasis_scale": 112,
    "caption_safe_area_ratio": 0.1,
    "caption_shaping": "complex",
    "dynamic_captions": true,
    "caption_phrase_max_words": 4,
    "caption_phrase_max_chars": 20,
    "caption_phrase_max_seconds": 3.2,
    "caption_break_gap_seconds": 0.6,
    "caption_importance_threshold": 0.5,
    "effects_enabled": true,
    "effects_min_importance": 0.45,
    "effects_max_visual": 2,
    "effects_zoom_min_duration": 6.0,
    "effects_zoom_duration": 4.0,
    "effects_zoom_intensity": 0.09,
    "effects_punch_intensity": 0.12,
    "effects_punch_duration": 0.9,
    "effects_emphasis_intensity": 0.06,
    "effects_emphasis_duration": 1.0,
    "effects_min_gap_seconds": 0.6,
    "effects_transitions": true,
    "effects_transition_duration": 0.3,
    "auto_vertical_fit": true,
    "vertical_landscape_fit": "blur",
    "metadata_auto_generate": true,
    "metadata_batch_size": 6,
    "metadata_min_hashtags": 2,
    "metadata_max_hashtags": 4,
    "metadata_title_max_chars": 120,
    "metadata_description_max_chars": 2200
  }
}
```

Notes:

- `formats` must define all three built-in formats: `vertical`, `square`, and
  `landscape`. Width and height must be even for `yuv420p`.
- `default_formats` lists the formats created when a request omits `formats`.
- `default_fit` accepts `crop`, `pad`, or `blur`. `blur` uses a blurred scaled copy
  as the background and the full frame centered on top.
- `min_clip_seconds`/`max_clip_seconds` validate stored highlight ranges before jobs
  are created.
- `render_timeout_seconds` bounds each FFmpeg render; `ffprobe_timeout_seconds`
  bounds source and output probes.
- `min_free_disk_bytes` refuses rendering when the output filesystem is too full.
- Clip job directories and output parents are created before FFmpeg runs. Source,
  output, and caption paths are resolved to absolute paths, and output is written to
  `<name>.part.mp4` before atomic replacement, so relative data roots and later cwd
  changes cannot break exports.
- `keep_failed_artifacts` keeps FFmpeg logs/partial files for debugging. By default
  failed and cancelled renders are cleaned.
- Captions are generated as ASS and rendered by libass. `caption_font_arabic` is
  used for Arabic; `caption_shaping: "complex"` enables Arabic shaping.
  `caption_encoding: -1` enables whole-text bidirectional layout for Arabic and mixed
  Arabic/Latin lines. Do not reverse Arabic text manually.
- `caption_font_size_ratio` is relative to output height: the default `0.062`
  produces `119px` at `1080x1920`. `caption_border_style: 3` draws a
  background box; `caption_back_colour: "&H50000000"` makes it semi-transparent.
  `caption_box_padding_ratio` scales box padding with output resolution.
- `dynamic_captions` groups transcript words into short phrases using punctuation,
  pauses, and the `caption_phrase_*` limits. When word timestamps exist, each word
  gets a timed ASS event so the current word can be accented; transcripts without
  word timestamps keep the previous segment-based caption path.
- `caption_accent_colour` styles the current word. `caption_emphasis_colour` and
  `caption_emphasis_scale` apply restrained emphasis to at most one evidence-backed
  important word per cue. Emphasis is confidence-gated by
  `caption_importance_threshold`.
- `caption_safe_area_ratio` reserves bottom space for platform UI. Captions remain
  bottom-centered and use `caption_font_*`, `caption_outline`, and `caption_shadow`.
- `effects_enabled` allows automatic visual decisions. Effects are skipped when
  highlight confidence and available signals fall below `effects_min_importance`.
  Long clips may receive one visible but restrained zoom, important words may receive
  one controlled punch-in and/or emphasis pulse, and sentence boundaries may receive
  short fades. Zoom and punch use smoothstep easing. `effects_max_visual` and
  `effects_min_gap_seconds` prevent stacked or repetitive effects.
- `auto_vertical_fit` changes the default vertical fit to `vertical_landscape_fit`
  when the source is landscape. An explicit `fit` request always wins; square and
  landscape outputs keep the configured default fit.
- Successful renders write `render_diagnostics.json` beside the clip. It records the
  selected effects, effect filters, full filter graph, caption style, and output
  information for troubleshooting and acceptance checks.
- `metadata_auto_generate` schedules non-blocking title/description/hashtag batches
  when clips are created. Generation uses only clip-range transcript and highlight
  context, so it can finish before rendering. `metadata_batch_size` bounds clips per
  AI request (default `6`, maximum `12`); failed batches split recursively. Provider
  or quota failures fall back to local metadata when transcript context exists, or
  remain `failed`/`deferred_quota`; they never fail rendering. Successful values use
  `generated`, local values use `local_fallback`. Manual edits set
  `manually_edited` and are protected from automatic overwrite.
- `metadata_min_hashtags`/`metadata_max_hashtags` bound generated hashtags.
  Manual edits keep exactly the hashtags supplied (no padding).
- `metadata_title_max_chars`/`metadata_description_max_chars` truncate stored
  metadata to fit common publishing fields.
- `config/local/clips.json` deep-merges over the base file.

## Provider fields

```json
{
  "type": "litellm",
  "enabled": true,
  "api_key_env": "GEMINI_API_KEY",
  "litellm_prefix": "gemini/",
  "base_url": null,
  "gateway": false,
  "timeout_seconds": 90,
  "max_retries": 2,
  "min_interval_seconds": 1.0,
  "requests_per_minute": 15,
  "requests_per_day": 0,
  "extra_headers": {},
  "notes": "..."
}
```

An OpenAI-compatible gateway uses `type: "openai_compatible"` and requires an explicit
`base_url`, for example `https://gateway.example/v1`. The adapter posts to
`/chat/completions` with a bearer key from `api_key_env`. It never infers a URL or a
model ID. `gateway: true` identifies a model aggregator whose underlying provider is
declared on the model entry. The bundled NVIDIA provider uses
`base_url: https://integrate.api.nvidia.com/v1` and discovers models from NVIDIA's
structured catalog. Only resources labelled `Free Endpoint` are approved; partner
and download-only resources are ignored. Discovery metadata is cached at
`config/local/nvidia-catalog.json` for seven days by default. Presence in NVIDIA's
public `/v1/models` list is not a successful inference test, so discovered models
remain Not Ready until a bounded smoke test succeeds.

Refresh discovery from the Providers screen or with:

```bash
curl -X POST "http://127.0.0.1:8000/api/ai/providers/nvidia/discover?force=true"
```

Speech-specific fields:

- `max_audio_bytes`
- `chunk_seconds`
- `chunk_overlap_seconds`
- `ffmpeg_binary`

whisper.cpp-specific fields:

- `binary`
- `model_path`
- `extra_args`

## Model fields

```json
{
  "id": "gemini-3.8-flash",
  "provider": "gemini",
  "provider_model_id": "gemini-3.8-flash",
  "litellm_model": null,
  "underlying_provider": null,
  "underlying_model": null,
  "tasks": ["content_analysis", "highlight_detection"],
  "free": true,
  "free_verified": true,
  "verified_at": "2026-11-09",
  "verification_source": "https://ai.google.dev/gemini-api/docs/pricing",
  "access_type": "unknown",
  "pricing_status": "free",
  "api_available": "unknown",
  "verification_status": "verified",
  "discovered": false,
  "catalog_source": null,
  "context_window": 1000000,
  "max_output_tokens": 8192,
  "supports_json": true,
  "supports_word_timestamps": false,
  "daily_request_limit": 0,
  "status": "enabled",
  "notes": "..."
}
```

Important:

- `free` defaults to `false`. A model is never free by accident.
- `status` must be `enabled` to execute.
- `tasks` must include the requested task.
- `supports_json` must be true for structured text tasks.
- `daily_request_limit` is a local guard, not a provider quota report.
- Gateway models declare `underlying_provider`/`underlying_model` so cooldown from a
  quota-exhausted underlying provider also blocks the gateway route.
- Discovered NVIDIA models set `discovered: true`, `access_type: free_endpoint`,
  `pricing_status: free`, and an `api_available` value of `true`, `false`, or
  `"unknown"`. Only `verification_status: verified` models are Ready.
- Catalog-derived overrides preserve local verification fields when discovery is
  refreshed. Stale discovered models disappear unless they were manually added.

## Routing

```json
{
  "tasks": {
    "content_analysis": [
      { "provider": "gemini", "model": "gemini-3.8-flash" },
      { "provider": "openrouter", "model": "openrouter-free-router" },
      { "provider": "groq", "model": "groq-gpt-oss-120b" }
    ]
  }
}
```

Candidates are filtered by policy and availability. The first eligible candidate is
tried first; fallback only happens for error categories allowed by policy. Free-quota
exhaustion is retryable across free providers by default. The manager records a
cooldown for the exhausted provider (provider `retry_after`, otherwise the next UTC
midnight for daily-quota messages, or one hour), skips it on subsequent calls, and
never falls through to a paid model.

## Skills

Skill manifests are JSON data only. Built-in manifests live in
`clipping_ai/skills/manifests/`; optional external manifests can be placed in
`config/skills/`. External skills are discovered disabled and must be reviewed before
they can be enabled. Skill state is stored in `config/local/skills.json`.

A manifest declares an ID, tools, permissions, timeout, and concurrency. Only tools
already registered by the application can run. Unknown tools are rejected, external
manifests cannot supply executable code, and network/filesystem/subprocess access is
denied unless the manifest declares the matching permission. Current built-in tools
are `composition.caption_plan`, `composition.moment_plan`,
`composition.callout_plan`, and `analysis.explainer_plan`.

## Policy

```json
{
  "policy": {
    "require_verified_free": false,
    "allow_unverified_free_if_manually_approved": true,
    "fallback_on_rate_limit": true,
    "fallback_on_quota_exhausted": true,
    "fallback_on_provider_unavailable": true,
    "fallback_on_model_unavailable": true,
    "fallback_on_timeout": true,
    "fallback_on_invalid_response": true,
    "fallback_on_schema_validation": true,
    "schema_validation_retries": 2,
    "max_fallback_candidates": 3
  },
  "retry": {
    "base_delay_seconds": 1.0,
    "max_delay_seconds": 30.0,
    "jitter_seconds": 0.5,
    "max_retry_after_seconds": 60.0
  }
}
```

Policy cannot enable paid models. It can only make the free-only rules stricter.

## Local overrides

`config/local/providers.json`, `models.json`, `routing.json`, `policy.json`,
`video.json`, `analysis.json`, and `clips.json` are deep-merged over the base files.
Verification and status changes are written to `config/local/models.json`.

## API

AI endpoints are under `/api/ai`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/providers` | Provider status and capabilities |
| GET | `/models?task=` | Models and per-task eligibility |
| GET | `/status?include_health=` | Overall readiness and policy |
| GET | `/usage?days=&recent_limit=` | Local usage aggregates |
| POST | `/test-connection` | Test one provider/model |
| POST | `/analyze` | Run a structured text task |
| POST | `/transcribe` | Transcribe a local audio path |
| POST | `/verify-free/{model_id}` | Check free pricing where supported |
| POST | `/reload` | Reload config and rebuild providers |
| PATCH | `/providers/{provider_id}` | Persist a provider enable/disable override |
| GET | `/api/skills` | List skills, registered tools, and external directory |
| PATCH | `/api/skills/{skill_id}` | Enable or disable a skill |
| POST | `/api/skills/{skill_id}/review` | Mark an external skill reviewed |
| POST | `/api/skills/{skill_id}/execute` | Run declared built-in tools with a context object |

Video and analysis endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/video/config` | Effective video limits and formats |
| POST | `/api/video/upload` | Upload a local video (`multipart/form-data`, field `file`) |
| POST | `/api/video/url` | Queue a URL job with `{"url": "https://..."}` |
| GET | `/api/video/jobs?limit=&status=` | List recent jobs |
| GET | `/api/video/jobs/{job_id}` | Public job data, metadata, errors |
| GET | `/api/video/jobs/{job_id}/status` | Compact status/stage/progress |

Source job payloads include `all_clips_published`, `all_clips_published_at`, and
`clip_counts: {"total": n, "published": n}`. Publishing completion is manual; setting
it to `false` clears the timestamp. Creating new clips resets completion to `false`.
| POST | `/api/video/jobs/{job_id}/retry` | Requeue a failed or cancelled job |
| POST | `/api/video/jobs/{job_id}/cancel` | Cancel a queued job |
| POST | `/api/video/jobs/{job_id}/publish` | Set manual `all_clips_published` completion |
| POST | `/api/video/jobs/{job_id}/metadata/regenerate` | Retry/source-batch metadata generation |
| DELETE | `/api/video/jobs/{job_id}` | Move a source video to trash |
| POST | `/api/video/jobs/{job_id}/restore` | Restore a source video from trash |
| DELETE | `/api/video/jobs/{job_id}/permanent` | Permanently delete a trashed source and its owned files |
| GET | `/api/analysis/config` | Effective transcription/highlight settings |
| POST | `/api/video/jobs/{job_id}/transcribe` | Transcribe prepared job audio |
| GET | `/api/video/jobs/{job_id}/transcript` | Stored transcript and error metadata |
| POST | `/api/video/jobs/{job_id}/highlights` | Detect highlight suggestions |
| GET | `/api/video/jobs/{job_id}/highlights` | Stored highlight result and error metadata |
| POST | `/api/video/jobs/{job_id}/analyze` | Transcribe then detect highlights |
| GET | `/api/clips/config` | Effective clip formats, captions, and FFmpeg limits |
| POST | `/api/video/jobs/{job_id}/clips` | Create and optionally render highlight clips |
| GET | `/api/video/jobs/{job_id}/clips` | List clip jobs for a source video job |
| GET | `/api/clip-jobs?limit=&status=&source_job_id=&ids=` | List recent clip jobs; `ids` accepts comma-separated clip ids |
| GET | `/api/clip-jobs/{clip_id}` | Public clip job data, metadata, errors |
| GET | `/api/clip-jobs/{clip_id}/status` | Compact clip status/stage/progress |
| POST | `/api/clip-jobs/{clip_id}/render` | Render or resume one clip job |
| POST | `/api/clip-jobs/{clip_id}/retry` | Requeue a failed or cancelled clip |
| POST | `/api/clip-jobs/{clip_id}/cancel` | Cancel a queued clip or request cancellation |
| PATCH | `/api/clip-jobs/{clip_id}/visual` | Save visual settings and optionally requeue a ready clip |
| PATCH | `/api/clip-jobs/{clip_id}/metadata` | Save manually edited title, description, hashtags |
| POST | `/api/clip-jobs/{clip_id}/metadata/regenerate` | Generate or regenerate publishing metadata |
| POST | `/api/clip-jobs/{clip_id}/publish` | Set the manual published status |
| DELETE | `/api/clip-jobs/{clip_id}` | Move a clip to trash |
| POST | `/api/clip-jobs/{clip_id}/restore` | Restore a clip from trash |
| DELETE | `/api/clip-jobs/{clip_id}/permanent` | Permanently delete a trashed clip and its owned files |
| GET | `/api/clip-jobs/{clip_id}/download` | Download a ready MP4 |
| GET | `/api/trash` | List trashed source videos and clips |
| POST | `/api/trash/restore` | Restore selected trash items |
| POST | `/api/trash/permanent` | Permanently delete selected trash items |
| DELETE | `/api/trash` | Empty the trash |

Example analyze request:

```json
{
  "task": "content_analysis",
  "text": "Transcript text here",
  "provider": "gemini",
  "model": "gemini-3.8-flash"
}
```

Example transcribe request:

```json
{
  "audio_path": "/absolute/path/to/audio.mp3",
  "language": "en",
  "word_timestamps": true
}
```

The `/api/ai/transcribe` endpoint accepts a local path. URL ingestion is handled
separately by the video engine and its explicit `yt-dlp` job flow.

Example prepared-job transcription request:

```json
{
  "provider": null,
  "model": null,
  "language": "en",
  "word_timestamps": true,
  "prompt": null,
  "refresh": false
}
```

Example highlight request:

```json
{
  "content_type": "auto",
  "provider": null,
  "model": null,
  "use_llm": true,
  "refresh": false
}
```

`content_type` accepts `auto`, `podcast`, `educational`, `general`, `gaming`,
`live`, `mixed`, or `unknown`. `auto` classifies from media evidence; an explicit
value overrides classification. `refresh: true` recomputes and replaces the stored
result. `analyze` accepts transcription and highlight options together. If
transcription fails, analysis continues with signal-only highlights and records the
transcription error.

Example clip request:

```json
{
  "formats": ["vertical", "square"],
  "quality": "standard",
  "fit": "crop",
  "captions": true,
  "caption_language": "ar",
  "visual_settings": {
    "visual_enhancement": true,
    "intensity": "balanced",
    "captions": true,
    "zoom": true,
    "punch": true,
    "emphasis": true,
    "transitions": true,
    "callouts": true
  },
  "highlight_indices": [0, 1],
  "limit": 2,
  "render": true
}
```

Set `"render": false` to queue jobs for the background worker. Captions require a
stored transcript; the API returns `invalid_request` rather than inventing text.
`visual_enhancement: false` disables effects and callouts while leaving captions
available. Intensity scales effects (`subtle` 0.6, `balanced` 1.0, `strong` 1.25) and
sets the default maximum visual effects to 1, 2, or 3. Upserted settings persist on
the clip; re-rendering a ready clip is explicit.

## CLI

```bash
uv run clipping-ai status
uv run clipping-ai status --health --json
uv run clipping-ai providers
uv run clipping-ai models --task speech_to_text
uv run clipping-ai usage --days 7
uv run clipping-ai analyze content_analysis --text "Hello"
uv run clipping-ai analyze translation --text "Hello" --target-language ar
uv run clipping-ai transcribe /path/audio.mp3 --language en
uv run clipping-ai test-connection gemini --model gemini-3.8-flash
uv run clipping-ai verify-free openrouter-free-router
uv run clipping-ai inspect /path/video.mp4
uv run clipping-ai prepare /path/video.mp4
uv run clipping-ai prepare "https://example.com/video"
uv run clipping-ai jobs --limit 20 --status ready
uv run clipping-ai job <job_id>
uv run clipping-ai retry <job_id>
uv run clipping-ai cancel <job_id>
uv run clipping-ai transcribe-job <job_id> --language en
uv run clipping-ai transcript <job_id>
uv run clipping-ai highlights <job_id> --content-type auto
uv run clipping-ai analyze-job <job_id> --language en
uv run clipping-ai analysis-config
uv run clipping-ai render-clips <job_id>
uv run clipping-ai render-clips <job_id> --format vertical --format square
uv run clipping-ai render-clips <job_id> --captions --caption-language ar
uv run clipping-ai render-clips <job_id> --highlight-index 0 --limit 1
uv run clipping-ai render-clips <job_id> --queue
uv run clipping-ai clip-jobs <job_id>
uv run clipping-ai clip-job <clip_id>
uv run clipping-ai render-clip <clip_id>
uv run clipping-ai retry-clip <clip_id>
uv run clipping-ai cancel-clip <clip_id>
uv run clipping-ai clips-config
uv run clipping-ai serve
```
