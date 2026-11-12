# Clipping AI

Standalone, local-only AI integration layer, video preparation engine, and
transcription/highlight-suggestion pipeline for a personal AI Clipping Studio.

The system exposes a small FastAPI service and CLI over a configurable registry of
free-tier text and speech providers. It also ingests local videos or URLs, extracts
normalised metadata, and prepares timestamped audio chunks for transcription and
highlight analysis.
It never sends requests to paid models: a model must be explicitly registered with
`free: true`, enabled, and eligible for the task.

## Features

- Free-only allow-list policy with a last-line guard before every provider call.
- Text tasks through LiteLLM: content analysis, highlight detection, clip scoring,
  titles, descriptions, translation, and caption refinement.
- Speech-to-text through Groq Whisper, with automatic ffmpeg chunking and a resumable
  SQLite chunk cache.
- Optional local `whisper.cpp` speech provider. Nothing is downloaded automatically;
  an existing binary and GGML model must be configured. Provider health distinguishes
  `ready`, `not_installed`, `model_missing`, `misconfigured`, `error`, `disabled`, and
  `unavailable`.
- Prepared jobs can be transcribed and turned into timestamped highlight suggestions
  using transcript context plus ffmpeg audio, scene, and motion signals. Auto
  classification distinguishes podcast, educational, general, gaming, live, mixed,
  and unknown content with confidence-gated evidence. Highlights are suggestions
  only: original source video and speaker audio are preserved.
- Highlight clips can be rendered from the original source into vertical (9:16),
  square (1:1), and landscape (16:9) MP4 files. Captions are optional per clip and
  use ASS/libass with Arabic shaping and RTL support.
- Automatic editing builds short dynamic caption phrases from word timestamps,
  accents the current word, emphasizes at most one evidence-backed important word per
  cue, and adds visible but restrained smoothstep-eased zoom/punch/emphasis/boundary
  fades only when highlight confidence and signals support them. Captions use a
  resolution-scaled font and a semi-transparent background box; `Encoding=-1` lets
  libass handle whole-text Arabic bidi. Landscape sources default to a blurred
  vertical fit unless an explicit fit is requested.
- Task-based routing, bounded retries, rate-limit handling, fallback, and local usage
  tracking in SQLite.
- Video ingestion from local uploads or `yt-dlp` URLs, with safe filenames, size and
  format validation, duplicate detection, and persistent jobs.
- Normalised ffprobe metadata plus ffmpeg audio extraction and configurable,
  timestamped audio chunks. Transcription and highlight analysis consume these
  prepared artifacts.
- Source-preserving clip rendering with atomic outputs, ffprobe verification,
  per-clip captions, cancellation, retry, cleanup, and a second CPU-friendly worker.
- A single CPU-friendly background worker per job type, retry/cancel controls, and a
  local API bound to loopback by default, plus a CLI.
- A packaged vanilla studio UI at `/ui/` with dashboard, library, analysis, highlight
  selection, export, clip preview, jobs, and provider views; English/Arabic RTL and
  responsive mobile navigation with no frontend build step.

## Install on EndeavourOS

These steps assume an EndeavourOS (Arch-based) installation with `pacman`.

1. Install the system tools. `uv` is available from the Arch `extra` repository.

```bash
sudo pacman -Syu --needed python ffmpeg uv
```

`ffmpeg` is optional for text-only use, but required for video preparation, audio
chunking, and files larger than a provider's upload limit. `yt-dlp` is installed as
a Python dependency by `uv sync`; it is used only when a video URL is submitted.
`whisper.cpp` is optional and is not installed or downloaded by this project.

2. Enter the project directory.

```bash
cd /path/to/cliping
```

3. Create the local virtual environment and install development dependencies.

```bash
uv sync --extra dev
```

4. Create your local secret file and restrict its permissions.

```bash
cp .env.example .env
chmod 600 .env
```

5. Edit `.env` and add only the providers you use.

```bash
GEMINI_API_KEY=...
GROQ_API_KEY=...
OPENROUTER_API_KEY=...
```

A missing key disables only that provider. The service still starts. Never commit
`.env`; it is ignored by git.

6. Verify the local configuration before starting the API.

```bash
uv run clipping-ai status
uv run clipping-ai providers
uv run clipping-ai models
```

Python 3.11+ is required. Python 3.14 is supported and is the version used in the
tested EndeavourOS environment.

## Configure

- `config/providers.json`: provider runtime settings and API key environment names.
- `config/models.json`: the explicit free-model allow-list.
- `config/routing.json`: task-to-model priority lists.
- `config/policy.json`: free-only, fallback, and retry policy.
- `config/video.json`: video size, format, ffmpeg, audio, and worker settings.
- `config/analysis.json`: transcription and highlight-detection settings.
- `config/clips.json`: clip formats, quality, captions, FFmpeg limits, and worker.
- `config/local/`: generated local overrides, including verification updates.

API keys are read only from environment variables or `.env`:

- `GEMINI_API_KEY`
- `GROQ_API_KEY`
- `OPENROUTER_API_KEY`

Missing keys disable only the affected provider.

## Start locally

### Quick start

From the project root:

```bash
./scripts/start_local.sh
```

The helper starts the backend, keeps its logs visible, verifies `/health`, checks the
dashboard API endpoints, and verifies that `/ui/` and its assets are served. It names
the backend or frontend if a startup check fails. It refuses to start a second server
when one is already responding and stops the process it started when you press
`Ctrl+C`.

The helper respects `CLIPPING_AI_HOST` and `CLIPPING_AI_PORT` from your shell or
`.env`. It does not edit configuration or delete data.

### Manual startup

One terminal is enough because the backend serves the packaged frontend.

1. Install the Python environment if needed:

```bash
uv sync --extra dev
```

2. Optionally create the local secret file:

```bash
cp .env.example .env
chmod 600 .env
```

Add only the provider keys you use. A missing key disables only that provider; the
service still starts. Do not commit `.env`.

3. Start the service:

```bash
uv run clipping-ai serve
```

It listens on `http://127.0.0.1:8765` by default. Leave this terminal open.

4. Check that it is running:

```bash
curl -s http://127.0.0.1:8765/health
curl -s http://127.0.0.1:8765/api/ai/status | python -m json.tool
```

5. Open `http://127.0.0.1:8765/ui/` in your browser.

Stop the application with `Ctrl+C` in the terminal running `serve`.

Useful local commands:

```bash
uv run clipping-ai status
uv run clipping-ai providers
uv run clipping-ai models
uv run clipping-ai usage
uv run clipping-ai status --health
```

## Use the studio UI

Open `http://127.0.0.1:8765/ui/` in a browser. The same FastAPI process serves the
packaged vanilla frontend; there is no separate Node build or dev server. `/`
redirects to `/ui/`.

The frontend has no runtime npm dependencies. `package.json` exists only for the
Playwright end-to-end tests, so `npm install` is not required to use the studio.

The studio covers the complete local workflow:

- Dashboard: service/provider readiness, active pipeline state, and recent media.
- Video library: upload, URL import, search, status filters, retry, and cancel.
- Analysis: source metadata, transcript, transcript search, classification,
  highlight suggestions, and selection for export.
- Export: choose detected highlights, formats, quality, fit, and optional
  Arabic/English captions, then queue clip jobs.
- Clip library: preview rendered MP4s, download, render, retry, and cancel.
- Jobs monitor: combined video/clip job state, filters, and recovery actions.
- Providers: local provider/model inventory, readiness, usage, test connection, and
  configuration reload.

The UI is same-origin by default. It can read an alternate local API base from
`clipping.apiBase` in browser local storage before loading, but that origin must be
allowed by the backend CORS configuration (the default dev origins are
`localhost:3000` and `localhost:5173`). The language control switches English/Arabic
and the full interface direction (`ltr`/`rtl`); the choice persists locally.

Clip preview uses the existing `GET /api/clip-jobs/<clip_id>/download` endpoint.
There is no source-video streaming endpoint, and the UI does not add custom trim
ranges: clips use the detected highlight boundaries exposed by the API. The trim note
in analysis is informational, not a hidden trim control.

Frontend smoke tests use Playwright against a temporary local backend:

```bash
npm install
npx playwright install chromium
npm run test:e2e
```

The smoke test starts its own `clipping-ai serve` process with a temporary data
directory, exercises routing, language/RTL switching, dialogs, empty states, mobile
navigation, and fails on browser console or page errors. Set `STUDIO_BASE_URL` to
test an already-running instance instead.

## Prepare a video

The video engine accepts common containers (`.mp4`, `.mkv`, `.mov`, `.webm`, `.avi`,
`.m4v`, `.mpeg`, `.mpg`, `.ts`, `.flv`, `.wmv`) up to 2 GiB and 6 hours by default.
It stores the input, normalised metadata, one full audio file, and timestamped audio
chunks under `data/videos/jobs/<job_id>/`.

Prepare a local file or URL from the CLI:

```bash
uv run clipping-ai inspect /path/to/video.mp4
uv run clipping-ai prepare /path/to/video.mp4
uv run clipping-ai prepare "https://example.com/video"
uv run clipping-ai jobs --limit 20
uv run clipping-ai job <job_id>
```

The API uses the same job store and worker:

```bash
curl -s -F "file=@/path/to/video.mp4" http://127.0.0.1:8765/api/video/upload
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"url":"https://example.com/video"}' http://127.0.0.1:8765/api/video/url
curl -s http://127.0.0.1:8765/api/video/jobs
curl -s http://127.0.0.1:8765/api/video/jobs/<job_id>
```

Video jobs use the existing `data/jobs.db` SQLite file and `data/videos/`. There is
no Redis, PostgreSQL, Docker, or external queue. Interrupted `processing` jobs are
requeued when the service starts. Only one worker processes jobs at a time.

To use a different port:

```bash
uv run clipping-ai serve --port 8766
```

Keep the bind address on loopback. The API has no authentication and is intended
for one local user. Do not expose it to a network or the public internet.

## Transcribe and detect highlights

Transcription and highlight detection operate on an already prepared video job. The
transcriber reads the prepared full audio file, and highlight detection combines
transcript segments with ffmpeg audio-energy, scene-change, and lightweight motion
signals. Auto classification uses confidence-gated evidence and can return podcast,
educational, general, gaming, live, mixed, or unknown. It does not render clips or
create replacement media. Clip rendering is a separate stage.

```bash
uv run clipping-ai transcribe-job <job_id> --language en
uv run clipping-ai transcript <job_id>
uv run clipping-ai highlights <job_id> --content-type auto
uv run clipping-ai analyze-job <job_id> --language en
uv run clipping-ai analysis-config
```

Use `--no-llm` with `highlights` or `analyze-job` to force deterministic selection.
`analyze-job` transcribes first, then detects highlights. If transcription fails, it
continues with signal-only highlights and records the transcription error.

The API exposes the same workflow:

```bash
curl -s -X POST -H 'Content-Type: application/json'   -d '{"language":"en","word_timestamps":true}'   http://127.0.0.1:8765/api/video/jobs/<job_id>/transcribe
curl -s http://127.0.0.1:8765/api/video/jobs/<job_id>/transcript
curl -s -X POST -H 'Content-Type: application/json'   -d '{"content_type":"auto","use_llm":true}'   http://127.0.0.1:8765/api/video/jobs/<job_id>/highlights
curl -s http://127.0.0.1:8765/api/video/jobs/<job_id>/highlights
curl -s -X POST -H 'Content-Type: application/json'   -d '{"language":"en","content_type":"auto"}'   http://127.0.0.1:8765/api/video/jobs/<job_id>/analyze
curl -s http://127.0.0.1:8765/api/analysis/config
```

Default highlights are 12-180 seconds, with at most 10 results and a minimum
confidence of 0.25. The wider maximum lets a complete explanation survive without
forcing unrelated moments to merge; self-contained short clips remain valid. Short
jobs may return no highlights. Adjust `config/analysis.json` or
`config/local/analysis.json` for different clip lengths, semantic consolidation,
audio/scene/motion thresholds, ranking weights, classification confidence, overlap
deduplication, and LLM refinement. `content_type` accepts `auto`, `podcast`,
`educational`, `general`, `gaming`, `live`, `mixed`, or `unknown`; `auto` classifies
the media, while an explicit value overrides auto classification. LLM refinement is
optional and still follows the free-only model policy; deterministic
signal/transcript selection is the fallback.

## Render clips

Rendering consumes an already prepared job with a stored highlight result. Clips are
cut from the original source video and audio, never from the normalised transcription
audio. The original file is not modified. Outputs are written atomically under
`data/clips/jobs/<clip_id>/` and verified with ffprobe before they become `ready`.
Storage roots are resolved to absolute paths at startup, and each job directory is
created before FFmpeg starts, so exports do not depend on the backend launch cwd.

```bash
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
```

Default formats are vertical `1080x1920`, square `1080x1080`, and landscape
`1920x1080` at 30 fps. `--format` may be repeated. The default fit is `crop`; `pad`
and `blur` are also available. For landscape sources, vertical clips automatically
use the configured blurred fit unless the request passes an explicit fit. Captions
are off unless `--captions` is supplied. Arabic and English captions are rendered
through ASS/libass, which handles Arabic shaping and bidirectional layout. The default
style uses a `0.062` height-relative font size (`119px` at `1080x1920`), a
semi-transparent background box, and whole-text bidi. With word timestamps, captions
use short phrase cues with a timed current-word accent and restrained important-word
emphasis; without them, segment captions are preserved. If captions are requested
for a job without a transcript, rendering fails with a clear error instead of
inventing text.

Automatic visual effects are enabled by default but evidence-gated. Long clips may
receive one visible but restrained smoothstep-eased zoom, important words may receive
a controlled punch-in or emphasis pulse, and sentence boundaries may receive short
fades. Insufficient highlight confidence or signals produce a clean render with no
added effects. Successful renders write `render_diagnostics.json` beside the clip
with the selected effects, effect filters, full filter graph, caption style, and
output metadata. See `config/clips.json` and `CONFIGURATION.md` for thresholds.

The API uses the same jobs:

```bash
curl -s -X POST -H 'Content-Type: application/json' \
  -d '{"formats":["vertical","square"],"captions":true,"render":true}' \
  http://127.0.0.1:8765/api/video/jobs/<job_id>/clips
curl -s http://127.0.0.1:8765/api/video/jobs/<job_id>/clips
curl -s http://127.0.0.1:8765/api/clip-jobs
curl -s http://127.0.0.1:8765/api/clip-jobs/<clip_id>/status
curl -s -o clip.mp4 http://127.0.0.1:8765/api/clip-jobs/<clip_id>/download
curl -s http://127.0.0.1:8765/api/clips/config
```

Set `"render": false` to only queue clip jobs for the background worker. The clip
worker uses the existing `data/jobs.db` database and a `clip_jobs` table. Temporary
files are cleaned on success, failure, and cancellation; interrupted `processing`
jobs are requeued when the service starts. Rendering is local CPU work and does not
call any paid service.

Gaming and live-stream detection use transcript-free evidence: sudden audio peaks,
silence-to-sound transitions, scene changes, motion, duration, and filename/URL
source hints. The stored result includes the classification confidence and
per-highlight `detection_signals`. Heuristics are not game-specific: they do not
recognise game titles, kill feeds, HUD text, or OCR. For commentary-heavy or
ambiguous recordings, an explicit `--content-type gaming` or `--content-type live`
still gives the strongest ranking bias. The clip renderer is content-agnostic.

## Troubleshooting on EndeavourOS

- `uv: command not found`: install it with `sudo pacman -S uv`, or install it from
  Astral with `curl -LsSf https://astral.sh/uv/install.sh | sh` and open a new shell.
- `ffmpeg: command not found`: install it with `sudo pacman -S ffmpeg`.
- `yt-dlp` errors: run `uv sync` so the Python dependency is installed, then retry.
  Private, DRM-protected, or unsupported URLs are rejected rather than bypassed.
- Video job is `failed`: inspect the stored error with
  `uv run clipping-ai job <job_id>`. The input and partial outputs remain for retry.
- Clip render fails with `Error opening output files: No such file or directory`:
  update to the current version, confirm `CLIPPING_AI_DATA_DIR` is writable, then run
  `uv run clipping-ai retry-clip <clip_id>`. Storage paths are resolved at startup
  and clip output directories are created before FFmpeg runs.
- `error: externally-managed-environment`: do not install this project with system
  `pip`; run commands through `uv sync` and `uv run`.
- `Address already in use`: stop the other process or use
  `uv run clipping-ai serve --port 8766`. Inspect the port with
  `ss -ltnp 'sport = :8765'`.
- `missing_api_key` or a provider shows `key=no`: add the key to `.env`, then
  restart `serve`. Check `uv run clipping-ai providers`.
- `whisper_cpp` is unavailable: this is expected until an existing `whisper-cli`
  binary and GGML model are configured. Groq speech works without it. Check the
  precise provider status (`not_installed`, `model_missing`, `misconfigured`,
  `disabled`, or `error`) in the studio or `uv run clipping-ai providers`. A local
  whisper.cpp build was verified with `tiny.en` and multilingual `tiny`; configure
  `binary` and `model_path` in `config/providers.json` or the
  `CLIPPING_AI_WHISPER_CPP_*` environment variables.
- Provider returns `429`, `503`, or `provider_unavailable`: this is usually a
  temporary provider-side limit or outage. Wait and retry, or check
  `uv run clipping-ai status --health`.
- OpenRouter verification says the routing alias cannot be checked: use a concrete
  model ID, or keep the explicit manual approval and run
  `uv run clipping-ai verify-free <model-id>` for concrete models.

## Zero-paid guarantee

The service rejects a model unless `free: true` and `status: enabled`. Paid models,
unregistered models, and arbitrary provider model IDs are not executable. Quota
exhaustion stops execution by default rather than falling back to a paid model.
Provider pricing can change; re-verify the registry periodically. OpenRouter
verification treats missing or malformed pricing metadata as unknown, not free, and
revokes free approval when non-zero pricing is confirmed.

Video preparation covers ingestion, metadata, audio extraction, and chunking.
Transcription, highlight suggestion, and source-preserving clip rendering with
optional dynamic burned-in captions and evidence-gated automatic effects are
implemented on top of prepared jobs. No AI-generated video, voice replacement, TTS,
or synthetic speaker audio is used.

See `ARCHITECTURE.md`, `PROVIDERS.md`, `CONFIGURATION.md`, `FREE_TIER_POLICY.md`, and
`TESTING.md` for details.
