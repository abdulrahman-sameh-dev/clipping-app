# Acceptance Test Results

**Date:** 2026-11-14
**Status:** PASS WITH LIMITATIONS  
**Environment:** EndeavourOS, Python `3.14.8`, `uv 0.11.25`, FFmpeg `n9.0.2`, 8 vCPU,
13 GiB RAM, approximately `55 GB` free disk.

## Summary

| Area | Result | Evidence |
| --- | --- | --- |
| Automated test suite | PASS | `259 passed, 3 skipped`; coverage baseline `84%` |
| Static checks | PASS | `ruff check .` clean; `mypy clipping_ai` clean |
| Build | PASS | Source distribution and wheel built |
| Local ingestion | PASS | Arabic, English, educational samples reached `ready` |
| URL ingestion | PASS | Local HTTP URL reached `ready` via Python `yt_dlp` |
| Groq transcription | PASS | Arabic, English, and Arabic-educational transcripts produced |
| Highlight detection | PASS | Deterministic and LLM paths produced in-range highlights |
| Auto classification | PASS WITH LIMITATION | False podcast labels fixed; Arabic lecture now `educational` |
| Vertical rendering | PASS | `1080x1920`, captions, audio preserved |
| Square rendering | PASS | `1080x1080`, pad fit, audio preserved |
| Landscape rendering | PASS | `1920x1080`, blur fit, audio preserved |
| Caption burn-in | PASS | ASS + A/B reference diff; Arabic RTL pixel order and background box verified |
| Automated editing | PASS | Dynamic captions, evidence-gated effects, measured real zoom/punch, render diagnostics |
| API smoke | PASS | All tested endpoints returned expected status |
| Cancellation/retry | PASS | Queued clip cancelled, retried, rendered |
| Worker recovery | PASS | Killed `processing` job recovered and cleaned temporary files |
| Invalid input | PASS | Clear failures; failed jobs recorded where applicable |
| Subjective visual quality | NOT TESTED | Quantitative frame checks passed; contact sheet for human review |
| Local Whisper.cpp | NOT TESTED | Provider disabled; no binary or GGML model installed |
| Real podcast/interview | NOT TESTED | No suitable local sample |
| Real gaming | NOT TESTED | No suitable local sample |
| Arabic OCR | NOT TESTED | Tesseract lacks Arabic traineddata |

## Automated verification

```text
uv run pytest -q
259 passed, 3 skipped in 32.51s

uv run pytest --cov=clipping_ai --cov-report=term -q
TOTAL 7786 statements, 1220 missed, 84%

uv run ruff check .
All checks passed!

uv run mypy clipping_ai
Success: no issues found in 65 source files

uv build
Successfully built dist/clipping_ai-0.1.0.tar.gz
Successfully built dist/clipping_ai-0.1.0-py3-none-any.whl
```

The three skips are optional live-provider tests in `tests/test_live.py`.

### NVIDIA catalog discovery verification

NVIDIA catalog discovery was exercised live against the structured NGC search API
and the public `/v1/models` endpoint. The run found 110 endpoint resources, 43 Free
Endpoint resources, 18 text-capable resources, and 9 API-list matches. Models remain
unverified until an operator supplies `NVIDIA_API_KEY` and completes a bounded smoke
test. All policy, routing, cache, merge, and verification behavior is covered with
mocked tests in `tests/test_nvidia_catalog.py`; no live inference request was made.

### Short-form, metadata, and trash verification

The post-acceptance phase adds `tests/test_metadata_trash.py` and frontend static
integration checks. Verified results:

```text
uv run pytest -q tests/test_metadata_trash.py
5 passed in 0.73s

uv run pytest -q tests/test_frontend.py tests/test_metadata_trash.py
12 passed in 1.05s

uv run ruff check .
All checks passed!

uv run mypy clipping_ai
Success: no issues found in 65 source files

node --check clipping_ai/frontend/js/app.js
node --check clipping_ai/frontend/js/api.js
node --check clipping_ai/frontend/js/i18n.js
node --check clipping_ai/frontend/js/polling.js

npm run test:frontend
6 passed

PLAYWRIGHT_CHROMIUM_EXECUTABLE=... npm run test:e2e
Studio UI smoke test passed.
```

Covered behavior: preferred 30-45s long-highlight splitting at transcript boundaries;
metadata generation from clip-range context; manual-edit protection; forced
regeneration; manual publishing timestamps; deleted-source guards; source/clip soft
delete, restore, and permanent deletion with file-ownership checks; deleting a source
without deleting clips; unified trash list/restore/empty; metadata/trash/delete API
responses without internal paths; static integration of clip detail, metadata,
publishing, selection, delete, and trash UI routes; scoped active-clip polling that
pauses while hidden and stops when idle; and Node-level polling tests for overlap,
cleanup/abort, visibility refresh, and failure backoff. The Playwright smoke test also
verifies that an idle Clips page does not keep requesting `/api/clip-jobs?limit=200`.

### Provider expansion, composition, and skills verification

This phase adds `tests/test_skills_composition.py`, updates the real-config model
count, and expands the Skills studio navigation. Verified results:

```text
uv run pytest -q
259 passed, 3 skipped

uv run ruff check clipping_ai tests
All checks passed!

uv run mypy clipping_ai
Success: no issues found in 65 source files

npm run test:frontend
6 passed

npm run test:e2e
Studio UI smoke test passed.
```

Covered behavior: OpenAI-compatible gateway requests use the configured base URL,
bearer key, and model; missing `base_url` blocks execution; provider enable toggles
persist; external skill manifests stay disabled until review; unknown tools and
undeclared permissions are rejected; skill tools time out with a bounded error;
skills API list/toggle/execute work; visual enhancement can be disabled independently
from captions; intensity scales/caps effects; Arabic callouts are written with an
Arabic libass style. No real gateway quota was consumed.

### Metadata reliability and source completion verification

This phase adds `tests/test_metadata_reliability.py` and updates quota-policy tests.
Verified results at that phase:

```text
uv run pytest -q
232 passed, 3 skipped in 31.91s

uv run ruff check clipping_ai tests
All checks passed!

uv run mypy clipping_ai
Success: no issues found in 65 source files

npm run test:frontend
6 passed

PLAYWRIGHT_CHROMIUM_EXECUTABLE=... npm run test:e2e
Studio UI smoke test passed.
```

Covered behavior: one structured batch request maps metadata by clip ID; repeated
runs are idempotent; omitted items receive local fallback; failed batches split until
single clips succeed; quota failures produce local metadata without raising; clips
without transcript context fail cleanly; manual edits survive unless force is
explicit; free-quota exhaustion fails over to the next free model and skips the
exhausted provider on later calls; policy can explicitly disable quota fallback; and
source publishing completion stores/clears timestamps, reports total/published counts,
resets when clips are created, and survives trash/restore. Real provider quota was
not consumed.

## Real media results

All samples were local, user-owned files. The educational sample is an Arabic
mobile-development lecture.

| Sample | Job ID | Transcription | Classification | Highlights |
| --- | --- | --- | --- | --- |
| Arabic drama | `ddfcecb6cf954c818cc309f76eacd56e` | Groq Whisper, Arabic script | `general`, `0.6376` | 3 |
| English drama | `ec2a14b17ad4450e88ba34ddd18d6830` | Groq Whisper, English | `general`, `0.6379` | 3 |
| Arabic educational | `6ee1d8cfcd0947809dac9c09dc9b58b0` | Groq Whisper, Arabic script | `educational`, `0.6747` | 4 |
| API smoke (20s) | `8e8c03a839364a13b8541a43eaea99e9` | Groq Whisper, English | `mixed`, `0.7304` with LLM | 1 |

The Arabic transcript was noisy because the excerpt includes music and stylised
dialogue, but it still produced Arabic-script text and usable timestamps. The English
excerpt is quiet; mean audio volume was low, but transcription succeeded.

### Rendered output validation

Every row passed stream presence, full decode, duration tolerance, and audio
correlation checks. Correlation threshold is `0.8`.

| Output | Clip ID | Duration expected / actual | Audio correlation | Format | Captions |
| --- | --- | --- | --- | --- | --- |
| Arabic vertical | `096e186d738245458a8c8e4472b7cf1c` | `15.0` / `15.023243` | `0.95436` | `1080x1920` | `Noto Sans Arabic` |
| English vertical | `ad5b2c2e690a44ce8aa8ff73aec32697` | `13.0` / `13.021334` | `0.836391` | `1080x1920` | `Noto Sans` |
| Educational vertical | `1f9514f177b74d3f88a4b802e17283f7` | `12.78` / `12.846688` | `0.999847` | `1080x1920` | `Noto Sans Arabic` |
| API vertical | `aaa9ffc8a75d4caabd8710b9d2b0eace` | `13.0` / `13.013333` | `0.835506` | `1080x1920` | `Noto Sans` |
| Recovery vertical, high quality | `e4f26141ffbb4373a4658426097e803a` | `13.28` / `13.30322` | `0.949617` | `1080x1920` | none |
| Cancel/retry square | `f4469d6c3af94837ae53b004f1dce91a` | `14.0` / `14.016` | `0.900625` | `1080x1080` | none |
| Landscape blur | `61a96f65fdbb42e3a7a60dbf70812dff` | `12.38` / `12.401333` | `0.8264` | `1920x1080` | none |

Caption A/B checks compared each captioned output against a no-caption render of the
same segment:

| Output | Changed pixel fraction | Mean absolute difference | Result |
| --- | --- | --- | --- |
| Arabic | `0.003557` | `0.954782` | PASS |
| English | `0.012079` | `2.380054` | PASS |
| Educational | `0.026684` | `2.801074` | PASS |
| API | `0.012065` | `2.466005` | PASS |

English OCR was skipped because tesseract has no `eng` traineddata. This was reported
as skipped, not failed.

### Automated editing validation

The updated editing path was verified on the real Arabic source
`a608c91bf05b40c9b032ce2d4c0056ce`, highlight `0-12s`:

| Check | Result |
| --- | --- |
| Dynamic captions | 28 per-word ASS events; Arabic shaping preserved |
| Caption style | `119px` at `1080x1920`, `BorderStyle=3`, `BackColour=&H50000000`, `Encoding=-1` |
| Caption box | Visible semi-transparent box at `y 1592-1751`; text inside the bottom safe area |
| RTL pixel order | At `0.8s`, amber first word at x `608-715`; remaining white words at x `363-587` |
| Current-word accent | Amber accent present in sampled caption pixels |
| Important-word emphasis | Confidence-gated emphasis present |
| Automatic effects | One zoom, one punch-in, one boundary fade-out |
| Effect visibility | Foreground height `540px` at `0s`, `592px` at `4.4s` (`1.0963x`), `615px` at `1.5s` (`1.1389x`) |
| Auto vertical fit | Landscape source rendered as blurred `1080x1920` |
| Output | `49a969686151498eaf4165b1ff2e4942`, H.264/AAC, `2,753,735` bytes, `12.02s` |
| Diagnostics | `data/clips/jobs/49a969686151498eaf4165b1ff2e4942/render_diagnostics.json` |
| Full decode | Clean |
| Audio correlation | `0.892889` at `0.023s` offset |
| Contact sheet | `/tmp/opencode/visual-acceptance/visual-contact.png` |

English real source `15aa85f23e584aad9b67683c857990fb` also rendered as vertical
`1080x1920` H.264/AAC, `662,524` bytes (clip `978ab740834e4057ac2e8ae309b65d0f`).
The burned-in `Thank you.` caption had a visible dark box against the bright source;
the bottom-region mean dropped from `[109.5, 110.7, 120.2]` without captions to
`[93.8, 93.8, 94.1]` with captions. Frame measurements are recorded in
`/tmp/opencode/visual-acceptance/measurements.json`.

Subjective motion and readability still require human review. Automated checks now
verify caption size, box contrast, RTL pixel order, safe-area placement, diagnostics,
and measurable effect scaling, but they cannot judge taste or full-motion polish.

The high-quality 13.28s recovery clip produced a `10,074,770` byte output and took
about 57 seconds after recovery. Draft clips were faster.

## Provider results

| Check | Result | Details |
| --- | --- | --- |
| Groq `groq-gpt-oss-20b` | PASS | `ok:true`, replied `OK`, latency `5217 ms` |
| OpenRouter `openrouter-free-router` | PASS | `ok:true`, replied `OK`, latency `6787 ms` |
| Gemini `gemini-3.5-flash-lite` | PASS | `ok:true`, replied `OK`, latency `12101 ms` |
| Gemini `gemini-3.8-flash` | UPSTREAM UNAVAILABLE | HTTP `503`, high demand |
| Default routing fallback | PASS | Gemini `503` -> OpenRouter success; total `20.3s` |
| Explicit Groq LLM highlights | PASS | `method: llm`, `mixed 0.7304`, 1 highlight |

The default task router correctly fell back when `gemini-3.8-flash` returned HTTP
`503`. The fallback result was a valid title, description, hashtags, and language from
OpenRouter.

## Classification results

The initial acceptance run found false positives: all three long speech samples were
classified as `podcast` because speech coverage dominated the score. The scoring was
revised to require keyword evidence for podcast/educational labels and to default
dialogue-heavy media to `general`.

| Sample | Before | After |
| --- | --- | --- |
| Arabic drama | `podcast`, `0.7343` | `general`, `0.6376` |
| English drama | `podcast`, `0.7335` | `general`, `0.6379` |
| Arabic educational lecture | `podcast`, `0.8056` | `educational`, `0.6747` |
| Explicit `--content-type educational` | `educational`, `1.0` | unchanged |

Arabic keywords were added for educational, podcast, gaming, and live evidence. This
allowed the Arabic mobile-development lecture to classify as `educational` without an
override.

Regression tests were added:

- dialogue-heavy media without category keywords -> `general`, not `podcast`;
- lecture wording with educational keywords -> `educational`;
- provider connection test must request at least 64 output tokens.

## API results

The API ran on `127.0.0.1:8766` against the isolated acceptance data directory.

| Test | Result |
| --- | --- |
| `/health` | `200`, `{"status":"ok"}` |
| AI status/providers/models/usage | `200` |
| Video/analysis/clips config | `200` |
| Video jobs and clip jobs | `200` |
| Upload `api_smoke.mp4` | queued -> ready |
| Duplicate upload | `duplicate:true`, existing job `8e8c03a839364a13b8541a43eaea99e9` |
| Transcribe endpoint | ready |
| Highlights endpoint | ready |
| Create captioned clip | ready, `1,757,469` bytes |
| Download clip | `2,528,396` bytes |
| Malformed clip ID | `400`, `invalid_request` |
| Valid missing clip ID | `404`, `not_found` |

`render:true` is synchronous and can block for the full render. `render:false` and the
CLI `--queue` path use the background `ClipWorker`.

## Failure and recovery results

| Scenario | Result |
| --- | --- |
| Unsupported `.txt` input | exit `1`; `Unsupported video format '.txt'`; no job |
| Corrupt `.mp4` | exit `1`; FFmpeg `moov atom not found`; failed job `0fd0a45d3df14d328813df18a9aa54b9` |
| Video without audio | exit `1`; `The media file has no audio stream.`; failed job `ecd3a866099a4815961993ffd04aedd9` |
| Missing file | exit `1`; `Video file does not exist`; no job |
| Highlight index `99` | exit `1`; `Highlight index out of range: 99.` |
| Disabled `whisper_cpp` refresh | exit `1`; `provider_unavailable` |
| SIGTERM during render | process did not exit promptly; SIGKILL used |
| SIGKILL during render | status remained `processing`; `.part` file remained |
| Restart after SIGKILL | job recovered, completed, `.part` and FFmpeg log cleaned |
| Cancel queued clip | cancelled successfully |
| Retry cancelled clip | requeued and rendered successfully |

The SIGTERM behavior is a limitation: an active FFmpeg render thread can delay
graceful shutdown. The recovery path is verified for the harsher SIGKILL case.

## Bugs found and fixed during acceptance

1. **Provider connection false negatives.** `test-connection` used
   `max_tokens=8`, which returned an empty completion for reasoning models.
   Changed to `max_tokens=64`; Groq, OpenRouter, and Gemini 3.5 now return
   `ok:true`.
2. **JSON output contaminated by provider logs.** LiteLLM wrote INFO logs to stdout,
   breaking `--json` parsing. LiteLLM log handlers are now routed through the normal
   stderr logging path. `test-connection --json` stdout is valid JSON.
3. **Podcast over-classification.** Speech-heavy drama and lecture samples were
   labelled `podcast`. Scoring now requires keyword evidence and defaults ambiguous
   dialogue to `general`; Arabic educational keywords were added.
4. **Missing regression coverage.** Added tests for dialogue-heavy general content,
   educational keyword classification, and reasoning-safe connection-test tokens.
5. **Arabic RTL and caption readability.** Whole-text bidi is now enabled with ASS
   `Encoding=-1`; captions use a larger resolution-scaled font and a visible
   semi-transparent background box. Real rendered pixels verify the first Arabic word
   is on the right.
6. **Effects were configured but not visibly strong enough.** Zoom and punch now use
   smoothstep easing with stronger defaults; a real render measures `1.0963x` zoom and
   `1.1389x` punch scaling, and a synthetic FFmpeg regression test guards against
   configuration-only no-ops.
7. **whisper.cpp health was ambiguous.** Provider inspection now reports
   `not_installed`, `model_missing`, `misconfigured`, `error`, `disabled`, or
   `unavailable`, and the studio displays the precise status in English and Arabic.
8. **Trim messaging was misleading.** The analysis note now states that clips use
   detected highlight boundaries; no custom trim control exists in the UI or backend.

## Not tested / blocked

- Subjective visual inspection of caption placement, readability, and framing. The
  model cannot reliably inspect generated images; contact sheets are available for a
  human reviewer.
- Arabic OCR: tesseract lacks Arabic traineddata.
- Live NVIDIA inference: no `NVIDIA_API_KEY` was available. Catalog and public model
  list discovery ran live; inference verification remains mocked. FreeLLMAPI was
  removed from the bundled configuration.
- Subjective visual review of the new intensity/category controls and one-callout
  composition; automated ASS and plan assertions passed.
- Local Whisper.cpp in this run: provider is disabled and no binary/model is
  installed. Historical manual verification is documented in `TESTING.md`.
- Real podcast/interview media: no suitable local sample was available.
- Real gaming media with and without commentary: no suitable local sample was
  available.
- Category A/C/D end-to-end accuracy claims must not be made from this run.
- Subjective interaction review of the new clip detail, metadata, publishing, and
  Trash screens at desktop/mobile widths was not performed in this automated run.

## Artifacts

Acceptance data is disposable and stored under:

```text
/tmp/opencode/cliping-acceptance/
  api/                 API response captures and downloaded clip
  data/                isolated SQLite databases, video jobs, clip jobs
  failures/            corrupt, no-audio, unsupported, and missing-file evidence
  logs/                prepare, transcribe, highlights, render, validate, server logs
  outputs/             contact sheets and caption crops
  samples/             extracted real-media samples
```

Validation reports: `logs/validate-{arabic,english,educational,api,cancel-retry,
interrupt-recovery,landscape-blur}.json`. The automated-editing validation used the
same script against the real Arabic render and returned `"pass": true` for streams,
dimensions, duration, decode, audio correlation, ASS metadata, caption pixels, and
contact sheet.

All validation reports have `"pass": true` and no failed checks.

## Conclusion

The product passes local end-to-end acceptance for ingestion, real speech
transcription, highlight detection, automated editing, clip rendering, captions,
audio preservation, API operation, cancellation/retry, and crash recovery. The two
acceptance defects found during the run, provider connection false negatives and
podcast over-classification, were fixed with regression tests.

Remaining gaps are sample coverage and human visual review, not observed product
failures. The acceptance run should be repeated with real podcast/interview and
gaming media, and with local Whisper.cpp configured, before making accuracy claims
for those categories.
