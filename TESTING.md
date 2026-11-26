# Testing

## Setup

```bash
uv sync --extra dev
uv run pytest
```

The default suite is offline. It uses fake text/speech providers and does not need
API keys or network access. Video tests use locally generated media through ffmpeg
and are skipped when ffmpeg is unavailable. Clip tests generate real local media,
render actual MP4s, and probe the outputs; they are also skipped without ffmpeg. The
suite currently covers about 85% of `clipping_ai` (`215 passed, 3 skipped` in the
verified environment). The latest run also rendered a real Arabic source clip with
dynamic captions and automatic effects, verified the MP4 with ffprobe, measured
caption pixels and foreground scaling, and wrote render diagnostics.

For real-media end-to-end acceptance, see `ACCEPTANCE-TESTING.md` and the recorded
evidence in `TEST-RESULTS.md`.

## Commands

```bash
uv run pytest -q
uv run pytest -q -m live
uv run pytest --cov=clipping_ai --cov-report=term-missing
uv run ruff check clipping_ai tests
uv run mypy clipping_ai
```

Frontend polling behavior is covered by Node's built-in test runner (no install
needed):

```bash
npm run test:frontend
```

Frontend smoke tests use Playwright and a temporary local backend:

```bash
npm install
npx playwright install chromium
npm run test:e2e
```

The Playwright script starts its own `clipping-ai serve` process on a free loopback
port with a temporary data directory. Set `STUDIO_BASE_URL` to target an already
running instance, or `PLAYWRIGHT_CHROMIUM_EXECUTABLE` to use a specific Chromium
binary. The smoke test covers routing, language/RTL switching, dialogs, empty states,
mobile navigation, browser console/page errors, and verifies an idle Clips page does
not keep polling clip jobs.

`-m live` runs optional real-provider tests. They are skipped unless explicitly
enabled:

```bash
export CLIPPING_AI_LIVE_TESTS=1
# Keys may also be supplied through .env.
export GEMINI_API_KEY=...
export OPENROUTER_API_KEY=...
export GROQ_API_KEY=...
# Required for the Groq transcription test:
export CLIPPING_AI_LIVE_AUDIO=/absolute/path/audio.mp3
uv run pytest -q -m live
```

Live tests can consume free-tier quota. They are never required for normal
development. The Gemini test tries the registered free Gemini models in order and
fails only if none succeed. The Groq transcription test requires the response to
contain usable segment timestamps.

## Test layout

- `tests/test_policy.py`: free-only eligibility and fallback rules.
- `tests/test_routing.py`: routing, explicit overrides, filtering, candidate limits.
- `tests/test_manager.py`: execution, fallback, quota behavior, schema repair,
  missing keys, usage recording, speech execution, OpenRouter verification, and
  health-check policy enforcement.
- `tests/test_structured.py`: JSON extraction, schema validation, repair prompts.
- `tests/test_providers.py`: Groq speech parsing/chunking/cache, whisper.cpp parsing,
  and whisper.cpp health states (`not_installed`, `model_missing`, `ready`,
  `misconfigured`, `disabled`, execution `error`).
- `tests/test_skills_composition.py`: visual settings defaults/scaling, effect
  capping, callout generation, Arabic ASS overlay rendering, OpenAI-compatible
  gateway requests/availability, external skill review/enable, permission and
  timeout enforcement, provider toggle persistence, and skills API routes.
- `tests/test_errors.py`: provider exception mapping, retry parsing, secret redaction.
- `tests/test_throttle.py`: local rate/interval throttling and limit errors.
- `tests/test_audio.py`: hashing, ffmpeg chunk splitting (skipped if ffmpeg is absent).
- `tests/test_api.py`: FastAPI endpoints and error-to-HTTP mapping.
- `tests/test_frontend.py`: packaged studio mount, root redirect, index shell, static
  assets, missing-asset behavior, and static integration of clip detail, metadata,
  publishing, selection, delete, trash routes, and scoped polling.
- `tests/frontend/polling.test.mjs`: active/idle polling, visibility pause/resume,
  overlap prevention, cleanup/abort, and failure backoff.
- `tests/test_registry.py`: real config loading, settings including relative storage
  path resolution, free verification persistence.
- `tests/test_cli.py`: CLI parser and JSON output.
- `tests/test_video_config.py`: video config defaults, overrides, validation.
- `tests/test_video_store.py`: job persistence, claim/recovery, retry/cancel,
  duplicate lookup, and path validation.
- `tests/test_video_media.py`: ffprobe metadata, audio extraction, chunk offsets
  (skipped if ffmpeg is absent).
- `tests/test_video_ingest.py`: filename/URL validation, size limits, hashing,
  yt-dlp success/failure/timeout/redaction.
- `tests/test_video_service.py`: uploads, URL jobs, duplicates, retry, recovery,
  worker lifecycle, and path traversal.
- `tests/test_video_api.py`: video upload/list/status/retry/cancel/error endpoints.
- `tests/test_video_cli.py`: video CLI parser and command output.
- `tests/test_highlights.py`: audio/scene/motion signal parsing, short and semantic
  transcript candidates, confidence-gated content classification (gaming, live,
  mixed, unknown), detection signals, overlap deduplication, semantic boundary
  expansion, related-fragment consolidation, long-explanation preservation,
  confidence-first max truncation, LLM refinement, and deterministic fallback.
- `tests/test_analysis_service.py`: transcript persistence/normalisation, error
  handling, analyze continuation, and store migration.
- `tests/test_analysis_api.py`: prepared-job transcription, transcript, highlights,
  and analyze endpoints.
- `tests/test_analysis_cli.py`: analysis CLI parsing, options, JSON/human output,
  and missing-transcript handling.
- `tests/test_clips.py`: clip config, ASS caption generation and Arabic RTL text,
  real FFmpeg rendering for all formats, source audio presence, captions on/off,
  Arabic-caption export, relative output paths, relative data roots with cwd changes,
  missing job directories, concurrent exports, partial cleanup, retry-to-success,
  invalid timestamps, missing transcripts, failure cleanup, cancellation, API
  endpoints, and store recovery.
- `tests/test_editing.py`: dynamic word-timestamp captions, current-word accent sync,
  mixed Arabic/Latin fonts, phrase grouping, safe-area placement, long-cue wrapping,
  confidence-gated important words, effect activation/fallback, non-stacking effects,
  filter-graph composition, automatic vertical fit, render diagnostics, and real
  FFmpeg renders with dynamic English and Arabic captions plus effects. Rendered
  Arabic tests verify right-to-left word order and the caption background box; a
  synthetic FFmpeg render measures actual foreground scaling so zoom and punch cannot
  regress into configuration-only no-ops.
- `tests/test_metadata_trash.py`: metadata generation, manual-edit protection,
  regeneration, publishing timestamps, deleted-state guards, source/clip soft delete,
  restore, permanent deletion and file ownership, unified trash list/restore/empty,
  metadata/trash/delete API responses, and target-aware long-highlight splitting.
- `tests/test_metadata_reliability.py`: batch response mapping by clip ID,
  idempotency, recursive batch splitting, omitted-item local fallback, quota fallback
  without raising, no-transcript failure, manual-edit preservation, source publish
  completion/timestamps/counts, reset on new clips, and trash/restore persistence.
- `tests/test_clips_cli.py`: clip CLI parsing, queued rendering, JSON/human output,
  retry/cancel, and config output.
- `tests/test_live.py`: optional real-provider smoke tests.
- `tests/e2e/studio-smoke.mjs`: Playwright studio smoke test against a temporary
  local backend.

## Key invariants covered

- A paid model cannot execute even when explicitly requested.
- A gateway model without an explicit `base_url` or key remains unavailable, and
  gateway requests use the configured endpoint and bearer key.
- External skills stay disabled until reviewed; unknown tools and missing declared
  permissions are rejected; tool timeouts become bounded errors.
- Visual settings disable effects independently from captions, intensity scales and
  caps effects, and callouts use libass-compatible Arabic ASS.
- A paid model cannot be reached by fallback.
- Free-quota exhaustion falls back to another approved free model, blocks the
  exhausted provider for its cooldown, and never reaches paid models.
- Rate limits can fall back to another approved free model.
- Batch metadata is mapped only by clip ID; failures split, then local fallback keeps
  generation non-blocking.
- Missing keys produce `missing_api_key`, not a crash or paid request.
- Structured output is validated with Pydantic and repaired with bounded retries.
- Usage is recorded locally and does not claim to be provider quota truth.
- Provider SDK errors are normalized and secrets are redacted from messages.
- Speech chunk offsets are preserved and completed chunks are cached.
- Video uploads and URLs are validated before filesystem or subprocess work.
- Job IDs cannot traverse paths, and duplicate sources reuse the existing job.
- Interrupted `processing` jobs are recovered as `queued`.
- Audio chunk offsets and durations are explicit and monotonic.
- Failed jobs retain their input for retry; temp downloads are cleaned up.
- Transcript timestamps are clamped to media duration and low-probability words are
  filtered when configured.
- Highlight candidates are clamped to media duration and filtered by clip length and
  confidence.
- Auto classification is confidence-gated; explicit content types override it.
- Gaming/live candidates can be selected from audio peaks, silence transitions,
  scene changes, and motion without transcript text.
- Detection signals and classification evidence are persisted with the result.
- Overlap deduplication uses the configured ratio.
- Semantic candidates can extend beyond the short candidate window when the
  transcript supports a longer complete idea.
- Related fragments merge across small gaps or related bridge text; unrelated nearby
  ideas and self-contained short clips remain separate.
- Standalone `highlights` and full `analyze` use the same semantic consolidation.
- Maximum-highlight truncation keeps the highest-confidence highlights, not merely the
  earliest ones.
- The preferred 30-45s target is soft: long moments split only at transcript
  boundaries, preserve original timestamps/coverage, and within-target moments remain
  unchanged.
- LLM highlight refinement is optional; invalid or failed refinement falls back to
  deterministic candidates.
- Analysis continues with signal-only highlights when transcription fails.
- Clips are rendered from the original source file, not the transcription audio.
- Rendered clips keep an audio stream and are verified with ffprobe before `ready`.
- All three aspect ratios produce the configured even dimensions.
- Arabic captions preserve source text, render the first word on the right, and use
  libass shaping/RTL support with `Encoding=-1`.
- Caption styles use a resolution-scaled font and a visible semi-transparent
  background box.
- Dynamic captions only use word timestamps when they exist; otherwise the segment
  fallback preserves the prior caption behavior.
- Current-word accents follow word timestamps, and important-word emphasis is limited
  to one confidence-gated word per cue.
- Automatic effects are skipped below the evidence threshold and do not stack
  consecutive punch/emphasis effects.
- Zoom and punch are measurable in actual rendered frames, not merely present in the
  filter graph.
- Successful renders write `render_diagnostics.json` with effects, filters, caption
  style, and output metadata.
- Landscape sources use the configured blurred vertical fit unless the caller passes
  an explicit fit.
- Captions can be disabled per clip and are never invented without a transcript.
- Invalid ranges and out-of-bounds highlights are rejected before rendering.
- Failed and cancelled renders clean partial files and can be retried.
- Clip exports resolve storage, source, output, and caption paths before FFmpeg, so
  relative configured paths and later backend cwd changes cannot break rendering.
- Failed renders remove incomplete `.part.mp4` files without deleting unrelated job
  artifacts, and a retry can complete successfully.
- Concurrent clip exports use separate per-job directories.
- Metadata is generated only from clip-range transcript/highlight context, never by
  re-transcribing or re-analysing the original media.
- Metadata provider failures are recorded on the clip without failing the render.
- Manual metadata edits are marked `manually_edited` and cannot be overwritten without
  explicit force; clip and source publishing are manual and store timestamps.
- Source publish completion clears on uncheck, persists through refresh/trash/restore,
  reports published/total counts, and resets when new clips are created.
- Normal deletion is soft; permanent deletion requires trash and only removes files
  owned by that source or clip.
- Deleting a source never deletes its rendered clips. Trashed items can be restored
  or purged individually, in batches, or by emptying trash.
- API job responses expose public fields without internal filesystem paths.
- Original source video and speaker audio are never replaced or synthesised.
- The packaged frontend is served by FastAPI at `/ui/`; `/` redirects there.
- The studio switches English/Arabic direction and survives mobile navigation.
- UI smoke tests fail on browser console errors, page errors, and broken empty states.

## Manual whisper.cpp verification

The optional local provider was verified on 2026-11-10 with a temporary local build,
not with a mock. No model is downloaded by this project or by the test suite. The
temporary binary and model are not installed in the current acceptance environment,
where `whisper_cpp` is disabled.

```bash
git clone --depth 1 https://github.com/ggml-org/whisper.cpp.git /tmp/opencode/whisper.cpp
cmake -S /tmp/opencode/whisper.cpp -B /tmp/opencode/whisper.cpp/build \
  -DCMAKE_BUILD_TYPE=Release -DWHISPER_BUILD_TESTS=OFF
cmake --build /tmp/opencode/whisper.cpp/build -j"$(nproc)" --target whisper-cli
cd /tmp/opencode/whisper.cpp
sh ./models/download-ggml-model.sh tiny.en
sh ./models/download-ggml-model.sh tiny
```

English verification used the bundled `samples/jfk.wav` and produced:

```text
And so my fellow Americans ask not what your country can do for you
ask what you can do for your country.
```

The `WhisperCppProvider` adapter returned 2 segments, 26 words, language `en`, and
duration `10.76`. Arabic verification used a 60-second Wikimedia Commons news clip
with the multilingual `tiny` model and `language="ar"`; it returned 11 segments and
364 words of Arabic-script text. The tiny model is fast but low-accuracy; use a
larger model for production quality.

To configure the local provider, set `config/providers.json`:

```json
{
  "whisper_cpp": {
    "type": "whisper_cpp",
    "enabled": true,
    "binary": "/path/to/whisper.cpp/build/bin/whisper-cli",
    "model_path": "/path/to/whisper.cpp/models/ggml-tiny.en.bin",
    "timeout_seconds": 1800
  }
}
```

Then add or enable a matching `whisper_cpp` model in `config/models.json` with
`tasks: ["transcription"]`, `free: true`, and `status: "enabled"`. The binary and
model paths are local machine settings; do not commit them.


## NVIDIA catalog discovery

Deterministic tests in `tests/test_nvidia_catalog.py` cover structured Free Endpoint
classification, partner/download exclusion, API-list matching, cache reuse, registry
merge, policy blocking, dynamic routing, and bounded smoke-test verification. No test
uses a live NVIDIA inference request.

Live catalog-only refresh:

```bash
uv run python - <<'PY'
from pathlib import Path
from clipping_ai.providers import nvidia_catalog
print(nvidia_catalog.discover(Path("config"), force=True))
PY
```
