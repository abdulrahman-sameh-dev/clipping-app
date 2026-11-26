# Acceptance Testing

## Purpose

This document describes the repeatable end-to-end acceptance test for Clipping AI.
It verifies the product against real local media, not only unit fixtures:

- local and URL video ingestion;
- Groq Whisper transcription with real speech;
- automatic content classification and highlight detection;
- vertical, square, and landscape clip rendering;
- Arabic and English ASS captions;
- source-audio preservation;
- API upload, transcription, highlight, clip, download, cancellation, retry, and
  worker-recovery paths;
- expected failures for invalid or unsupported input.

The test is local-only. It does not deploy, upload media to a third party beyond the
configured free transcription provider, or replace the original speaker audio.

## Environment used

| Item | Value |
| --- | --- |
| OS | EndeavourOS, Arch-based Linux |
| Python | `3.14.8` |
| Package manager | `uv 0.11.25` |
| FFmpeg | `n9.0.2` with `libass`, `libfribidi`, `libharfbuzz`, `libx264`, `aac` |
| CPU / RAM | 8 vCPU / 13 GiB |
| Free disk | approximately `55 GB` |
| API keys | `GEMINI_API_KEY`, `GROQ_API_KEY`, `OPENROUTER_API_KEY` in `.env` |
| Optional local speech | `whisper_cpp` disabled; no binary or GGML model installed |
| OCR | `tesseract` installed with `afr` and `osd` only; no `eng` or Arabic data |

Never print or commit `.env`. The acceptance commands below assume the keys are
already configured.

## Prerequisites

```bash
uv sync --extra dev
uv run clipping-ai status
uv run clipping-ai providers --json
uv run clipping-ai models --json
```

`ffmpeg` and `ffprobe` must be on `PATH`. The default test suite is offline; this
acceptance test is not, because it calls the configured free providers.

Use an isolated data directory so acceptance jobs do not mix with normal data:

```bash
export CLIPPING_AI_DATA_DIR=/tmp/opencode/cliping-acceptance/data
mkdir -p /tmp/opencode/cliping-acceptance/{samples,logs,outputs,api,failures}
```

## Real media samples

The verified run used local, user-owned media. Do not commit these files.

| Sample | Source | Extraction |
| --- | --- | --- |
| `arabic_spoken.mp4` | `/home/abdulrahman/Videos/Fun/الحفرة_الموسم_الثاني_الحلقة_1.mp4` | 90s from `300s` |
| `english_spoken.mp4` | `/home/abdulrahman/Videos/Fun/Oldest/The Quine/The.Queens.Gambit.S01E01.mp4` | 90s from `300s` |
| `educational_spoken.mp4` | `/home/abdulrahman/Desktop/My Career/Courses/Programming_Courses/Mobile_Apps/Lec15/Part3.mp4` | 90s from `10s` |
| `api_smoke.mp4` | `english_spoken.mp4` | 20s from `30s` |

Example extraction:

```bash
ffmpeg -y -ss 300 -i "/path/to/source.mp4" -t 90 -c copy \
  /tmp/opencode/cliping-acceptance/samples/arabic_spoken.mp4

ffmpeg -y -ss 300 -i "/path/to/source.mp4" -t 90 -c copy \
  /tmp/opencode/cliping-acceptance/samples/english_spoken.mp4

ffmpeg -y -ss 10 -i "/path/to/lecture.mp4" -t 90 -c copy \
  /tmp/opencode/cliping-acceptance/samples/educational_spoken.mp4

ffmpeg -y -ss 30 -i /tmp/opencode/cliping-acceptance/samples/english_spoken.mp4 \
  -t 20 -c copy /tmp/opencode/cliping-acceptance/samples/api_smoke.mp4
```

The educational sample used in the verified run was an Arabic mobile-development
lecture, not English. This matters when judging auto-classification.

## CLI acceptance

### 1. Provider readiness

```bash
uv run clipping-ai status --json
uv run clipping-ai providers --json
uv run clipping-ai models --json

uv run clipping-ai test-connection groq --model groq-gpt-oss-20b --json
uv run clipping-ai test-connection openrouter \
  --model openrouter-free-router --json
uv run clipping-ai test-connection gemini \
  --model gemini-3.5-flash-lite --json
```

The default `gemini-3.8-flash` model may return HTTP `503` during demand spikes.
That is an upstream availability condition, not a configuration failure. The task
router must fall back to OpenRouter or Groq.

`--json` output must be valid JSON on stdout. Provider logs belong on stderr.

### 2. Ingest and prepare

```bash
uv run clipping-ai prepare \
  /tmp/opencode/cliping-acceptance/samples/arabic_spoken.mp4 --json
uv run clipping-ai prepare \
  /tmp/opencode/cliping-acceptance/samples/english_spoken.mp4 --json
uv run clipping-ai prepare \
  /tmp/opencode/cliping-acceptance/samples/educational_spoken.mp4 --json
```

Record each `id`, then verify `status` is `ready` and that `input.mp4`, metadata,
full audio, and audio chunks exist under `data/videos/jobs/<job_id>/`.

URL ingestion uses the same path:

```bash
python -m http.server 8770 --bind 127.0.0.1 \
  --directory /tmp/opencode/cliping-acceptance/samples \
  > /tmp/opencode/cliping-acceptance/logs/http-sample.log 2>&1 &
echo $! > /tmp/opencode/cliping-acceptance/http.pid

uv run clipping-ai prepare http://127.0.0.1:8770/api_smoke.mp4 --json
```

### 3. Transcribe

```bash
uv run clipping-ai transcribe-job <job_id> \
  --provider groq_speech --model groq-whisper-turbo --json
uv run clipping-ai transcript <job_id> --json
```

Verify the transcript is non-empty, language is detected or supplied correctly, and
segments have monotonic timestamps. Arabic text must remain Arabic script.

### 4. Detect highlights and classify

```bash
uv run clipping-ai highlights <job_id> --no-llm --refresh --json
```

Verify `highlight_result.highlights` is non-empty and each item has `start_time`,
`end_time`, `confidence`, `title`, and `reason`. Check
`highlight_result.classification.content_type` and the evidence in
`classification.reasons`.

Run the LLM path separately with an explicit free model:

```bash
uv run clipping-ai highlights <job_id> \
  --provider groq --model groq-gpt-oss-120b --refresh --json
```

The LLM path must either return refined highlights or fall back to deterministic
candidates with a note. It must not fail the whole job when the provider is
temporarily unavailable.

### 5. Render clips

```bash
uv run clipping-ai render-clips <job_id> \
  --highlight-index 0 --format vertical --quality draft \
  --captions --caption-language ar --json
```

Render a no-caption reference clip for the same highlight and settings. The
reference is used to prove the caption pixels are actually burned in:

```bash
uv run clipping-ai render-clips <job_id> \
  --highlight-index 0 --format vertical --quality draft --json
```

Test the other formats and fit modes:

```bash
uv run clipping-ai render-clips <job_id> \
  --highlight-index 0 --format square --fit pad --quality draft --json

uv run clipping-ai render-clips <job_id> \
  --highlight-index 0 --format landscape --fit blur --quality draft --json
```

Record each clip `id`, `output_path`, `caption_path`, dimensions, and status.

For automatic editing, verify:

- a landscape source rendered to vertical without an explicit `--fit` uses the
  configured blurred background;
- an explicit `--fit crop` or `--fit pad` still wins;
- the ASS file contains short timed phrase events, Arabic shaping, a current-word
  accent, and restrained important-word emphasis;
- Arabic and mixed lines use whole-text bidi (`Encoding=-1`); never manually reverse
  the text;
- the caption style uses a resolution-scaled font, `BorderStyle=3`, a
  semi-transparent `BackColour`, and a visible background box;
- rendered Arabic pixels place the first word on the right and later words to its
  left; the current-word accent can be used as the pixel marker;
- the render succeeds when the plan selects no visual effects;
- captions remain inside the configured bottom safe area on sampled frames;
- `render_diagnostics.json` exists and lists the selected effects, effect filters,
  full filter graph, caption style, and output metadata;
- effect scaling is measurable in the rendered frames. For blurred vertical fit, use
  the sharp foreground's top/bottom edges: the default Arabic acceptance clip
  measured `540px` at `0s`, `592px` at the `4.4s` zoom peak, and `615px` at the
  `1.5s` punch peak.

### 6. Validate output

Use the repository validation script. It checks streams, dimensions, FPS, pixel
format, duration tolerance, full decode, audio correlation, ASS metadata, caption
pixels, A/B caption difference, optional OCR, and contact sheets.

```bash
uv run python scripts/acceptance_validate.py \
  --source /tmp/opencode/cliping-acceptance/samples/arabic_spoken.mp4 \
  --output /tmp/opencode/cliping-acceptance/data/clips/jobs/<clip_id>/clip_vertical_draft.mp4 \
  --start <highlight_start> --end <highlight_end> \
  --width 1080 --height 1920 --fps 30 --audio \
  --ass /tmp/opencode/cliping-acceptance/data/clips/jobs/<clip_id>/captions.ass \
  --caption-language ar \
  --caption-reference /tmp/opencode/cliping-acceptance/data/clips/jobs/<no_caption_clip_id>/clip_vertical_draft.mp4 \
  --contact-sheet /tmp/opencode/cliping-acceptance/outputs/arabic/contact.png \
  --json
```

For English, use `--caption-language en` and `--ocr-english`. If `eng` traineddata is
missing, the OCR check is reported as skipped rather than failed.

The audio correlation threshold is `0.8`. A pass proves the output audio still
matches the original source segment after an offset search.

### 7. Failure cases

```bash
uv run clipping-ai prepare /tmp/opencode/cliping-acceptance/failures/not_video.txt --json
uv run clipping-ai prepare /tmp/opencode/cliping-acceptance/failures/corrupt.mp4 --json
uv run clipping-ai prepare /tmp/opencode/cliping-acceptance/failures/no_audio.mp4 --json
uv run clipping-ai prepare /tmp/opencode/cliping-acceptance/failures/missing.mp4 --json
uv run clipping-ai render-clips <job_id> --highlight-index 99 --json
uv run clipping-ai transcribe-job <job_id> \
  --provider whisper_cpp --model local-whisper --refresh --json
```

Expected behavior:

- unsupported extension and missing file fail before creating a job;
- corrupt media and media without audio create a failed job record with a useful
  message;
- invalid highlight index fails without rendering;
- disabled local Whisper fails with `provider_unavailable`, not a crash.

### 8. Cancellation, retry, and recovery

Create a queued clip with `--queue`, cancel it, retry it, and render it:

```bash
uv run clipping-ai render-clips <job_id> --highlight-index 0 \
  --format square --fit pad --quality draft --queue --json
uv run clipping-ai cancel-clip <clip_id> --json
uv run clipping-ai retry-clip <clip_id> --json
uv run clipping-ai render-clip <clip_id> --json
```

For recovery, start a high-quality queued render, kill the API process with
`SIGKILL` while it is `processing`, restart the API, and verify the worker recovers
the interrupted job. A `.part` file and FFmpeg log may remain after the crash; both
must be cleaned after recovery.

## API acceptance

Start the API on an isolated port:

```bash
uv run clipping-ai serve --host 127.0.0.1 --port 8766 \
  > /tmp/opencode/cliping-acceptance/logs/server.log 2>&1 &
echo $! > /tmp/opencode/cliping-acceptance/server.pid
```

### Endpoint smoke

```bash
curl -s http://127.0.0.1:8766/health
curl -s http://127.0.0.1:8766/api/ai/status
curl -s http://127.0.0.1:8766/api/ai/providers
curl -s http://127.0.0.1:8766/api/ai/models
curl -s http://127.0.0.1:8766/api/ai/usage
curl -s http://127.0.0.1:8766/api/video/config
curl -s http://127.0.0.1:8766/api/analysis/config
curl -s http://127.0.0.1:8766/api/clips/config
curl -s http://127.0.0.1:8766/api/video/jobs
curl -s http://127.0.0.1:8766/api/clip-jobs
```

### Upload and process

```bash
curl -s -F "file=@/tmp/opencode/cliping-acceptance/samples/api_smoke.mp4" \
  http://127.0.0.1:8766/api/video/upload
curl -s -X POST http://127.0.0.1:8766/api/video/jobs/<job_id>/transcribe
curl -s -X POST http://127.0.0.1:8766/api/video/jobs/<job_id>/highlights
```

Upload the same file again and verify `duplicate: true` plus the existing job ID.

### Create and download a clip

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"highlight_indices":[0],"format":"vertical","quality":"draft","captions":true,"caption_language":"en"}' \
  http://127.0.0.1:8766/api/video/jobs/<job_id>/clips

curl -s -o /tmp/opencode/cliping-acceptance/api/download.mp4 \
  http://127.0.0.1:8766/api/clip-jobs/<clip_id>/download
```

`render:true` is synchronous and may block for the full FFmpeg render. Use
`render:false` or the CLI `--queue` path to exercise the background worker.

### Metadata, publishing, and trash

After a clip reaches `ready`:

```bash
curl -s -X PATCH -H 'Content-Type: application/json'   -d '{"title":"Edited title","description":"Edited description","hashtags":["#shorts"],"language":"en"}'   http://127.0.0.1:8766/api/clip-jobs/<clip_id>/metadata
curl -s -X POST -H 'Content-Type: application/json' -d '{"force":true}'   http://127.0.0.1:8766/api/clip-jobs/<clip_id>/metadata/regenerate
curl -s -X POST -H 'Content-Type: application/json' -d '{"published":true}'   http://127.0.0.1:8766/api/clip-jobs/<clip_id>/publish
curl -s -X DELETE http://127.0.0.1:8766/api/clip-jobs/<clip_id>
curl -s http://127.0.0.1:8766/api/trash
curl -s -X POST -H 'Content-Type: application/json'   -d '{"items":[{"kind":"clip","id":"<clip_id>"}]}'   http://127.0.0.1:8766/api/trash/restore
```

Verify manual edits survive a reload and are marked `manually_edited`; regeneration
requires `force:true` to overwrite them. Publishing sets `published_at`. Soft delete
removes the item from active lists but keeps it in `/api/trash`. Source deletion must
not remove rendered clips. Permanent deletion only succeeds for trashed items.

### API error cases

```bash
curl -s http://127.0.0.1:8766/api/clip-jobs/not-a-valid-id
curl -s http://127.0.0.1:8766/api/clip-jobs/00000000000000000000000000000000
```

Expected: malformed ID -> HTTP `400` with `invalid_request`; valid but missing
32-hex ID -> HTTP `404` with `not_found`.

## Cleanup

```bash
kill "$(cat /tmp/opencode/cliping-acceptance/server.pid)" 2>/dev/null || true
kill "$(cat /tmp/opencode/cliping-acceptance/http.pid)" 2>/dev/null || true
```

Verify no `clipping-ai serve` or `http.server 8770` processes remain. Acceptance
media and logs under `/tmp/opencode/cliping-acceptance/` are disposable.

## Pass criteria

| Area | Pass condition |
| --- | --- |
| Ingestion | Local file and HTTP URL reach `ready`; metadata and audio artifacts exist |
| Transcription | Real speech produces non-empty, timestamped text |
| Classification | Evidence is plausible; explicit override always wins |
| Highlights | Non-empty, in-range, ordered, and persisted |
| Rendering | All requested formats reach `ready`; no partial outputs |
| Dimensions | Vertical `1080x1920`, square `1080x1080`, landscape `1920x1080` |
| Audio | Output has audio and correlation with source is at least `0.8` |
| Captions | ASS exists, font is appropriate, pixels are burned in, Arabic RTL pixel order is correct, background box is visible |
| Effects | Diagnostics exist; real frame measurements show non-zero zoom/punch scaling |
| Provider health | whisper.cpp reports a precise status without attempting a download |
| Metadata | Generated fields use clip context; manual edits are protected; failures are recorded without failing the render |
| Publishing | Manual checkbox state and timestamp persist; no upload/OAuth occurs |
| Deletion | Normal delete is soft; restore works; permanent delete requires trash and respects file ownership |
| Trash | Mixed source/clip list, restore, purge, and empty work without deleting unrelated clips |
| API | All smoke endpoints return `200`; upload/transcribe/highlights/clip/metadata/trash/download work |
| Errors | Invalid input fails clearly; failed jobs are recorded when applicable |
| Recovery | Interrupted `processing` job recovers and cleans temporary files |
| Cancellation | Queued clip cancels and retries successfully |
| Test suite | `pytest`, Ruff, mypy, and `uv build` pass |

## Known limitations

- Auto-classification is heuristic. Arabic keywords are limited, and a lecture with
  no recognised keywords may be classified as `general` rather than `educational`.
  Use `--content-type educational` when the correct category is known.
- Subjective caption placement, readability, and aesthetic quality require human
  review. Contact sheets are generated for that purpose. Quantitative checks now
  cover caption size, background-box contrast, RTL pixel order, safe-area placement,
  diagnostics, and effect scaling, but not taste or full-motion polish.
- Arabic OCR is unavailable in the verified environment because tesseract has no
  Arabic traineddata.
- Local `whisper.cpp` is optional and was not available in the verified acceptance
  run. See `TESTING.md` for the historical manual local-provider verification.
- No real podcast/interview or gaming sample was available. Those categories are
  covered by unit fixtures and must not be reported as end-to-end verified.
- `yt-dlp` is a Python dependency; a separate `yt-dlp` binary is not required.
- Publishing is a local manual status only; no YouTube/Facebook upload, OAuth,
  scheduling, or analytics is implemented.
- Trash has no automatic expiry. Permanent deletion is explicit and irreversible.
- Subjective visual/interaction review of clip detail, metadata forms, publishing,
  and Trash at desktop/mobile widths remains a human task.
