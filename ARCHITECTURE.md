# Architecture

## Overview

`clipping-ai` is a standalone AI integration layer, local video preparation engine,
and transcription/highlight-suggestion pipeline with source-preserving clip rendering.
Its jobs are to answer one question safely: **which approved free provider/model
should execute this AI task, and how should the request, validation, fallback, and
usage tracking work?** It also turns a local video or URL into persistent metadata,
timestamped audio chunks, transcripts, evidence-backed highlight suggestions, and
rendered clips while preserving the original source media and audio.

The system is designed for a single local user. It has no hosted control plane, no
shared database, and no provider-side proxy. Configuration is file-based and all
state is local.

```
CLI / FastAPI
      |
      +--> ProviderManager
      |      |-- Router ----------> registry + free-only policy
      |      |-- FreeOnlyPolicy --> hard allow-list checks
      |      |-- Structured ------> prompt, JSON extraction, Pydantic validation
      |      |-- Throttle / retry
      |      |-- UsageTracker ----> data/usage.db
      |      |
      |      v
      |    Provider adapters
      |      |-- LitellmTextProvider --> Gemini / OpenRouter / Groq text
      |      |-- GroqSpeechProvider  --> Groq Whisper speech-to-text
      |      |-- WhisperCppProvider  --> optional local whisper.cpp
      |
      +--> VideoService
      |      |-- VideoJobStore ---> data/jobs.db
      |      |-- YtDlpDownloader --> URL ingestion
      |      |-- ffprobe ----------> normalised metadata
      |      |-- ffmpeg -----------> full audio + timestamped chunks
      |      |-- VideoWorker ------> one local background thread
      |
      +--> AnalysisService
      |      |-- ProviderManager --> free-only transcription
      |      |-- VideoJobStore ---> transcript + highlight persistence
      |      |-- Signals ----------> ffmpeg audio energy + scene + motion
      |      |-- Classification ---> confidence-gated content type
      |      |-- Candidates -------> weighted transcript/signal windows
      |      |-- LLM refinement ---> optional highlight_detection task
      |      |-- Deterministic fallback
      |
      +--> ClipService
             |-- ClipJobStore ----> clip_jobs table in data/jobs.db
             |-- Captions --------> ASS + libass (Arabic RTL/shaping)
             |-- FFmpeg ----------> crop/pad/blur, H.264/AAC, atomic MP4
             |-- ffprobe ---------> output stream/duration verification
             |-- ClipWorker ------> one local background thread
```

## Components

### Settings (`clipping_ai/settings.py`)

Loads `.env` and environment variables. Secrets are held as `SecretStr` where
possible and are never logged. A missing key makes only the affected provider
unavailable.

### Registry (`clipping_ai/registry.py`)

Loads and validates:

- `config/providers.json`
- `config/models.json`
- `config/routing.json`
- `config/policy.json`
- optional overrides under `config/local/`

The model registry is an explicit allow-list. A model is not usable unless it is
present, enabled, and marked `free: true`. Local verification updates are written
to `config/local/models.json`; provider enable toggles are written to
`config/local/providers.json`. Gateway model entries may declare
`underlying_provider`/`underlying_model` so quota cooldown applies to both routes.

### FreeOnlyPolicy (`clipping_ai/policy.py`)

Evaluates a model for a task. It rejects disabled models, paid models, models that
do not support the requested task, and (optionally) unverified free models. It also
decides whether a particular error category may fall back to another free model.

### Router (`clipping_ai/routing.py`)

Resolves task routing from configuration. It filters candidates by provider
enablement, registry membership, task support, and policy. It supports explicit
model/provider overrides only when the model is already registered. It never
accepts an arbitrary provider model string from an API caller.

### ProviderManager (`clipping_ai/manager.py`)

The central orchestration layer:

- builds provider adapters from configuration;
- lists providers/models/status;
- executes text and speech tasks;
- applies local daily limits, throttling, retries, and fallback;
- records usage;
- runs connection tests;
- verifies free pricing where a provider exposes it.

### OpenAI-compatible provider (`clipping_ai/providers/openai_compatible.py`)

A generic adapter for explicitly configured free gateways or local proxies. It posts
OpenAI-style chat completions to a configured `base_url`, sends a bearer key from the
configured environment variable, and normalises errors through the shared mapping.
It never infers a URL or model. The NVIDIA entry uses this adapter with
`https://integrate.api.nvidia.com/v1`. `clipping_ai/providers/nvidia_catalog.py`
discovers endpoint resources from NVIDIA's structured catalog, keeps only Free
Endpoint resources, classifies text capability from catalog labels, cross-checks the
public `/v1/models` list, and caches metadata in `config/local/nvidia-catalog.json`.
Catalog eligibility does not mark a model Ready: a bounded smoke test must succeed
before `verification_status` becomes `verified`.

### Text providers (`clipping_ai/providers/litellm_text.py`)

A single LiteLLM adapter serves Gemini, OpenRouter, and Groq text models. The model
string is derived from the approved registry entry. The adapter sets `num_retries=0`
and uses the internal retry loop so retries and fallback remain observable and
policy-controlled.

### Speech providers

- `GroqSpeechProvider`: official Groq SDK, verbose JSON timestamps, optional ffmpeg
  chunking, and a SQLite chunk cache for resume.
- `WhisperCppProvider`: optional subprocess adapter for an existing whisper.cpp
  binary and GGML model. No model is downloaded automatically. Health inspection
  reports precise states: `ready`, `not_installed`, `model_missing`, `misconfigured`,
  `error`, `disabled`, and `unavailable`.

### Structured output (`clipping_ai/structured.py`)

Builds deterministic prompts, includes a JSON Schema, extracts JSON from tolerant
model output, validates with Pydantic, and retries with a repair prompt when the
model returns invalid JSON or schema-invalid data.

### Video engine (`clipping_ai/video/`)

- `VideoService`: validates uploads/URLs, writes safe per-job directories, probes
  metadata, extracts audio, creates timestamped chunks, and updates job state.
- `VideoJobStore`: SQLite jobs with status, stage, progress, source, paths, metadata,
  audio artifacts, transcripts, highlight results, timestamps, and structured errors.
  It shares `data/jobs.db` with the transcription chunk cache and migrates older
  databases by adding the analysis columns.
- `VideoWorker`: one daemon thread that claims the oldest queued job, processes it,
  and recovers interrupted `processing` jobs on startup.
- `config/video.json`: size/duration limits, allowed containers, ffmpeg/ffprobe
  binaries, audio codec/rate/channels, chunk size/overlap, and URL download settings.

Video jobs move through `queued`, `processing`, `ready`, `failed`, and `cancelled`.
Processing stages are `queued`, `ingesting`, `metadata`, `audio`, `finalizing`, and
`ready`. A failed job keeps its input and partial outputs so it can be retried. Temp
downloads are removed after each attempt.

### Analysis and highlights (`clipping_ai/analysis/`, `clipping_ai/highlights/`)

- `AnalysisService`: orchestrates transcription and highlight detection for prepared
  jobs, persists results, normalises/clamps transcript timestamps, and supports
  `transcribe`, `highlights`, `analyze`, `get_transcript`, and `config_dict`.
- `signals.py`: streams PCM audio from ffmpeg into fixed windows, computes RMS/dBFS
  energy and silence, parses ffmpeg scene metadata, and samples lightweight motion
  from `signalstats` `YDIF`. Audio-only media is supported; missing streams or motion
  failures produce notes instead of failures.
- `classification.py`: confidence-gated classification from transcript coverage,
  active audio, silence, peaks, scene rate, motion, duration, and filename/URL
  source hints. Returns `podcast`, `educational`, `general`, `gaming`, `live`,
  `mixed`, or `unknown` with scores and reasons; explicit content types override it.
- `candidates.py`: builds short transcript windows and longer semantic windows from
  transcript gaps, plus signal candidates from audio events, peaks, silence
  transitions, scene changes, and motion. It merges nearby events, clamps to media
  duration, aligns windows to transcript segments, applies configurable weights,
  records per-candidate detection signals, and dedupes by overlap ratio.
- `semantic.py`: expands highlight boundaries to transcript sentences and consolidates
  related fragments across small or bridged gaps. Relatedness uses transcript
  continuity markers and lexical overlap; unrelated nearby ideas remain separate.
  Merged highlights retain the complete transcript range and get a completeness
  signal. `split_long_highlights` splits only moments beyond the preferred target
  plus tolerance, always at transcript boundaries, preserving timestamps and coverage.
- `llm.py`: optional structured `highlight_detection` refinement through
  `ProviderManager`; validates model output and clamps suggestions to media and
  transcript bounds.
- `engine.py`: deterministic selection is always available. LLM refinement is used
  only when enabled and a manager/model is available; failures fall back to
  deterministic candidates. Both paths run the same semantic consolidation, then
  dedupe and keep the highest-confidence highlights before sorting by start time.
- `config/analysis.json`: transcription language/timestamp settings and highlight
  clip lengths, confidence, signal thresholds, and LLM limits.

Highlights are suggestions with start/end times, titles, reasons, confidence, and
transcript context. Highlight detection does not modify or replace source media.

### Clip rendering (`clipping_ai/clips/`)

- `ClipService`: creates clip jobs from stored highlights, renders from the original
  `VideoJob.input_path`, builds optional captions, and handles retry, cancellation,
  recovery, and download paths. Configured storage roots are resolved to absolute
  paths at startup and clip/job paths are absolute. Successful renders write
  `render_diagnostics.json` beside the output.
- `ClipJobStore`: persists clip jobs in a `clip_jobs` table in the shared
  `data/jobs.db`, including source job/highlight, source timestamps, format, fit,
  quality, caption state, output path, size, status/stage/progress, errors,
  publishing metadata, manual published status, and soft-deletion time. Source jobs
  persist manual `all_clips_published` completion and its timestamp.
- `editing.py`: builds an `EditPlan` from transcript words, highlight confidence,
  detection signals, and optional media signals. It groups short caption phrases,
  scores restrained important-word emphasis, and selects at most a small number of
  visual effects. No effect is planned when evidence is below the configured
  threshold.
- `composition/`: `VisualSettings` validates per-clip enhancement/intensity/category
  choices; `build_composition_plan` reuses the existing `EditPlan`, scales and caps
  effects, and derives at most one restrained top-safe-area callout from the
  highlight title. `write_overlay_ass` renders callouts as ASS so libass performs
  Arabic shaping instead of manual reversal.
- `renderer.py`: resolves source, output, caption, and overlay paths, creates the
  output parent, probes the source and output, builds an FFmpeg filter graph for
  crop, pad, or blurred-background fit, then optionally chains a smoothstep-eased
  zoompan, timeline emphasis pulse, and boundary fades before burning caption and
  callout ASS files. Source
  audio is mapped unchanged in content, output is written to an absolute `.part.mp4`,
  verified with ffprobe, then atomically replaced. Timeouts, free-disk checks,
  cancellation polling, and cleanup are enforced. The result includes the selected
  effects and full filter graph for diagnostics.
- `captions.py`: generates ASS events clipped to the requested range. With word
  timestamps it emits per-word events for current-word accent sync and short phrase
  captions; without word timestamps it keeps the segment-based fallback. The default
  style uses a resolution-scaled font, a background box with a semi-transparent
  back colour, and `Encoding=-1` so libass performs whole-text Arabic shaping and
  bidirectional layout; text is never manually reversed.
- `metadata.py`: `ClipMetadataService` generates title, description, hashtags, and
  language from clip-range transcript/highlight context through the
  `title_generation` route. It batches clips by source, validates every response item
  by clip ID, splits failed batches, and falls back to deterministic local metadata
  when AI is unavailable. Scheduler threads coalesce duplicate source work and retry
  after new clips arrive. Successful values use `generated`, local values use
  `local_fallback`, and quota-only deferrals use `deferred_quota`. Manual edits set
  `manually_edited` and require explicit force before regeneration.
- `ClipWorker`: one daemon thread that claims the oldest queued clip, processes it,
  recovers interrupted `processing` jobs, and resumes pending source metadata batches
  on startup.
- `config/clips.json`: output dimensions, quality/CRF/preset, fit, FPS, dynamic
  caption phrase limits and styling, automatic effect thresholds, auto vertical fit,
  metadata generation/limits, FFmpeg/ffprobe timeouts, disk guard, and worker polling.

Clip jobs move through `queued`, `processing`, `ready`, `failed`, and `cancelled`.
Stages are `queued`, `captions`, `rendering`, `verifying`, `ready`, `failed`, and
`cancelled`. Captions are optional per clip. If captions are requested without a
transcript, creation fails rather than synthesising text. The original source video
and audio are never modified, replaced, or synthesised. Source-level publishing
completion is manual, persists across refresh/trash/restore, and resets when a new
clip is created. Counts exclude trashed clips and never auto-complete the source.

### Skills and tools (`clipping_ai/skills/`)

- `SkillManifest`/`SkillRecord` validate safe IDs, declared tools, permissions,
  timeout, and concurrency.
- `SkillRegistry` discovers built-in and optional external manifests from
  `config/skills/`, persists enable/review state in `config/local/skills.json`, and
  rejects unknown tools. External skills are disabled until explicitly reviewed.
- `ToolRegistry` contains the only executable handlers. Skills cannot provide code.
- `SkillExecutor` enforces enabled/reviewed state and declared permissions, then runs
  declared built-in tools with a timeout. Current tools plan captions, bounded visual
  emphasis, one callout, or conservative educational explanation.

### Trash and deletion (`clipping_ai/trash.py`)

- `TrashService` combines soft-deleted source videos and clips into one list with
  kind, title, deletion time, status, duration, format, and publishing state.
- Restore and permanent deletion accept mixed batches and return per-item results, so
  one invalid target does not block the rest.
- Normal deletion is always soft (`deleted_at`). Permanent deletion is only allowed
  for trashed items. Source deletion removes only the source job directory; rendered
  clips remain independent. Clip deletion removes only files inside the clip's owned
  directory, then deletes the clip row.
- Active video/analysis/clip creation guards reject trashed sources; metadata,
  publishing, and download guards reject trashed clips.

### Storage

- `data/usage.db`: local usage events and aggregates.
- `data/jobs.db`: transcription chunk cache, video job records, transcripts,
  highlight results, and clip job records.
- `data/videos/jobs/<job_id>/`: original input, `audio/audio.*`, and `audio/chunks/*`.
- `data/clips/jobs/<clip_id>/`: rendered MP4, optional `captions.ass`, and
  `render_diagnostics.json` for successful renders.
- Soft-deleted source/clip rows stay in `data/jobs.db` with their files until restored
  or permanently deleted. Trash has no automatic expiry.
- `config/local/`: local model, video, analysis, and clip overrides plus verification
  results.

SQLite is used because this is a single-user local service. There is no Redis,
PostgreSQL, Celery, or external queue.

## Text request flow

1. Validate the task and required arguments.
2. Resolve candidates through routing.
3. Filter by free-only policy, task support, provider availability, and JSON support.
4. Build messages and schema.
5. Apply local daily limits and throttling.
6. Call the provider.
7. Map provider exceptions to the internal error taxonomy.
8. Retry the same provider when safe.
9. On eligible errors, fall back to the next approved free candidate. Free-quota
   exhaustion blocks that provider for its cooldown window (provider `retry_after`,
   next UTC midnight for daily quotas, or one hour) and is never routed to paid
   models.
10. Parse and validate structured output, with bounded repair retries.
11. Record usage.
12. Return data plus provider/model/attempt metadata.

## Speech request flow

1. Validate the free speech model and input file.
2. If the file is over the provider limit, require ffmpeg and split it into
   timestamped chunks.
3. Check the SQLite cache for each completed chunk.
4. Transcribe missing chunks with the Groq SDK.
5. Persist each successful chunk.
6. Offset and merge timestamps.
7. Record audio seconds and request metadata.

If a chunk fails, the error includes completed/total chunk counts so a retry can
resume from the cache.

## Video preparation flow

1. Validate the source type, filename/extension, URL scheme, and size limit.
2. For uploads, stream to a temporary file, hash it, and detect duplicates. For URLs,
   enqueue a job and let the worker download with `yt-dlp` under a timeout.
3. Probe the input with ffprobe and normalise duration, size, container, bit rate,
   and audio/video stream details.
4. Extract one audio track with ffmpeg using the configured codec, sample rate, and
   channel count.
5. Split the audio into timestamped chunks with explicit offsets and durations.
6. Persist paths and progress, mark the job ready, and remove temporary files.

No transcription, highlight detection, clip selection, or rendering happens in this
flow. Those are separate analysis steps over the prepared job.

## Transcription and highlight flow

1. Require a prepared job in `ready` or `processing` state.
2. Transcribe the prepared full audio through the free-only provider manager.
3. Validate the transcript, clamp timestamps to the media duration, optionally filter
   low-probability words, and persist provider/model/error metadata.
4. Detect highlights from transcript segments plus ffmpeg audio-energy, scene-change,
   and motion signals. Audio-only jobs skip scene and motion analysis with a note.
5. Classify content with confidence gating and source hints. Build weighted candidate
   windows from audio events, peaks, silence transitions, scene changes, motion, and
   transcript evidence; dedupe by configurable overlap; filter by clip length and
   confidence.
6. Optionally refine candidates through the `highlight_detection` structured task,
   preserving per-highlight detection signals when supplied. The prompt asks for
   complete explanations rather than isolated short fragments and treats candidate
   windows as hints, not limits.
7. Fall back to deterministic candidates if LLM refinement is disabled or fails.
8. Expand boundaries and consolidate related fragments with the same semantic logic
   for standalone `highlights` and full `analyze`; then dedupe and rank.
9. Persist highlight suggestions, classification confidence/scores/reasons, signals,
   and errors. `analyze` continues to highlights if transcription fails, using
   signal-only evidence.
10. Never modify or replace the original source video or speaker audio.

## Clip rendering flow

1. Require a ready source job with a stored highlight result and an original input
   file inside the configured data directory.
2. Validate each highlight's start/end, minimum/maximum duration, and source bounds.
3. Create one clip job per highlight and requested format. Captions are rejected when
   the source job has no transcript.
4. Build a base edit plan from the stored highlight, transcript word timestamps, and
   available signals. For caption jobs, group short phrases, offset timestamps, mark
   one confidence-gated important word per cue, and write an ASS file. libass handles
   Arabic shaping and RTL.
5. Apply the persisted `VisualSettings`: scale/cap effects by intensity, honour
   category toggles, and optionally derive one top-safe callout from the highlight
   title. Write callouts to a separate ASS file.
6. Create the job output directory, resolve all paths, then render with FFmpeg from
   the original source using input seek, a format-specific filter graph, optional
   restrained effects, caption/callout ASS chaining, the source audio stream, and a
   temporary `.part.mp4`.
7. Poll cancellation, enforce the render timeout and disk guard, and terminate FFmpeg
   on cancellation or timeout.
8. Probe the temporary output for video, source audio, dimensions, and duration.
9. Write `render_diagnostics.json` with the edit plan, composition plan, effect
   filters, full filter graph, caption style, overlay path, and output metadata.
10. Atomically move the verified file to its final path and mark the clip ready.
11. Metadata is normally scheduled as a non-blocking source batch when clips are
    created, before rendering. It uses only stored transcript/highlight context;
    failures are recorded on each clip without changing render status.
12. On failure or cancellation, remove partial/temporary files unless configured to
    keep failed artifacts. A failed or cancelled clip can be retried.

## Concurrency and limits

This is intentionally a single-user system. Throttling and the SQLite locks are
in-process. The local daily counters are guards, not provider quota reports. Do not
treat them as exact remaining quota. The video engine and clip engine each start with
exactly one worker; FFmpeg/ffprobe, URL downloads, and clip rendering use subprocess
timeouts. The clip service also serialises renders in-process so API and worker calls
cannot render the same job concurrently.

## Extension points

- Add a new text provider by implementing `BaseTextProvider` and adding a provider
  `type` in configuration. OpenAI-compatible free gateways use the generic adapter
  with an explicit `base_url`.
- Add a skill as a JSON manifest that declares only registered tools; external
  manifests stay disabled until reviewed.
- Add a new speech provider by implementing `BaseSpeechProvider`.
- Add a task by adding a Pydantic schema in `schemas.py`, task instructions in
  `structured.py`, and routing candidates.
- Add a model by editing `config/models.json`; it remains unusable until explicitly
  marked `free: true`, enabled, and routed.
- Add a video container by editing `config/video.json` and ensuring ffmpeg/ffprobe
  support it.
- Add a clip output format by editing `config/clips.json` and extending the renderer
  profile handling if the new format needs a different filter graph.
- Add caption styling or fonts in `config/clips.json`; keep libass responsible for
  shaping and bidirectional layout.

## Non-goals

- No paid usage, automatic billing, or paid fallback.
- No LiteLLM Proxy Server.
- No Docker, Redis, PostgreSQL, Celery, or hosted queue.
- No multi-clip timeline composition, arbitrary overlays, or user-authored
  transitions. Automatic editing is limited to the restrained plan described above.
- No synthetic media, replacement voices, avatars, or AI-generated speech.
- No automatic caption translation in the clip renderer; captions use the source
  transcript language.
- No transcription inside the video preparation worker; analysis runs explicitly on
  prepared jobs.
- No DRM bypass or private-content scraping; unsupported URLs fail normally.
- No automatic whisper.cpp model downloads.
- No claim that local counters equal provider-side remaining quota.
