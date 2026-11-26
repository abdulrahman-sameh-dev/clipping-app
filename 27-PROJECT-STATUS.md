# Project Status

## Current state

- Phase 1-3 complete: free-only provider routing, video preparation, transcription,
  highlight detection, API, CLI, and persistence.
- Phase 4 complete: source-preserving clip rendering with vertical, square, and
  landscape MP4 output; optional Arabic/English ASS captions; cancellation, retry,
  recovery, cleanup, API, CLI, and documentation.
- Phase 3.1 complete: confidence-gated podcast/educational/general/gaming/live/mixed/
  unknown classification; transcript-free audio peaks, silence transitions, scene
  changes, and motion; configurable weighted ranking and overlap dedupe; per-highlight
  detection signals.
- Phase 5 complete: packaged local studio UI served by FastAPI at `/ui/`, covering
  dashboard, video library, analysis, highlight selection, export, clip preview,
  jobs monitor, and providers. English/Arabic RTL, responsive navigation, and no
  frontend build step.
- Clip export path hardening complete: storage roots resolve once at startup, FFmpeg
  receives absolute source/output/caption paths, per-job directories are created
  before rendering, and regression tests cover cwd changes, relative data roots,
  Arabic captions, all formats, concurrent exports, partial cleanup, and retry.
- Highlight semantic-completeness pass complete: transcript candidates now include
  longer semantic windows, related fragments are consolidated across related gaps,
  boundaries snap to sentence context, long explanations are no longer capped at 90s,
  and standalone `highlights` and full `analyze` share the same post-processing.
- Automated Editing MVP complete: word-timestamp dynamic phrase captions with
  current-word accents, confidence-gated important-word emphasis, Arabic/English/
  mixed RTL-safe ASS rendering, safe-area placement, restrained automatic zoom/
  punch/emphasis/boundary fades, and automatic blurred vertical fit for landscape
  sources. Explicit caller fit still wins.
- Short-form, metadata, and trash phase complete: highlights prefer a soft 30-45s
  target and split only at transcript boundaries; clips generate editable publishing
  metadata through bounded `title_generation` batches; the studio has clip detail,
  clip/source publishing status, selection/bulk actions, and unified Trash; source and
  clip deletion are soft by default with guarded permanent deletion.
- Metadata reliability and source completion complete: free-quota errors fail over
  across free providers with provider cooldown, batch failures split and fall back to
  deterministic local metadata, pending batches resume on restart, and source-level
  `All clips published` persists with counts and completed/incomplete filters.
- Provider, composition, and skills extension complete: an optional
  OpenAI-compatible adapter supports verified free gateways. NVIDIA NIM free
  endpoints are discovered from structured catalog metadata, task-filtered, checked
  against the public model list, and verified with bounded smoke tests before being
  marked Ready. Provider enable toggles persist locally, and provider status exposes
  gateway, catalog, cooldown, and last success/failure details.
- Content-aware visual composition complete: per-clip settings persist, intensity
  scales and caps effects, category toggles control zoom/punch/emphasis/transitions,
  and at most one top-safe callout is written as ASS for libass shaping.
- Skills/Tools infrastructure complete: data-only manifests, registered tools only,
  explicit external review, permission/timeout enforcement, API routes, and a studio
  Skills view with enable/disable controls.
- Local end-to-end acceptance testing complete against real Arabic and English media.
  See `ACCEPTANCE-TESTING.md` for the repeatable procedure and `TEST-RESULTS.md` for
  evidence.
- Original source video and speaker audio are never modified, replaced, or
  synthesised. Clips are cut from the original input file.

## Verification

- `uv run pytest -q`: `259 passed, 3 skipped`; coverage baseline `84%` from the last
  coverage run. New coverage includes the extension suite: gateway HTTP/auth and
  availability, provider toggle persistence, skill review/enable/permission/timeout
  behavior, skills API routes, visual settings scaling/capping, callout generation,
  Arabic ASS overlays, NVIDIA catalog classification/cache/merge/routing/policy and
  bounded smoke verification, plus the prior metadata, failover, publishing, trash,
  API, duration-splitting, and scoped polling coverage.
- `npm run test:frontend`: 6 Node polling tests pass; `npm run test:e2e` passes with
  the seven-link navigation and verifies an idle Clips page stops polling clip jobs.
- Real local FFmpeg tests render all three formats and probe video/audio streams.
- Arabic captions are tested for preserved RTL text and `Noto Sans Arabic`.
- Dynamic caption tests cover word-timestamp sync, current-word accenting, mixed
  Arabic/Latin fonts, safe-area placement, wrapping, important-word restraint, and
  non-overlapping cue timing.
- Automatic effect tests cover activation, evidence-based fallback, non-stacking
  punch/emphasis effects, filter-graph composition, and explicit-fit precedence.
- Clip regressions cover relative data roots, backend cwd changes, missing job
  directories, all formats without captions, concurrent exports, partial cleanup,
  and retry-to-success.
- A previously failed 12s source clip was retried and exported successfully:
  vertical `1080x1920`, H.264/AAC, `3,722,941` bytes, ffprobe valid, full decode clean.
- An Arabic-caption export from the same source also completed: `3,759,810` bytes,
  ffprobe valid, full decode clean, with `Noto Sans Arabic` ASS captions.
- Real Arabic source `a608c91bf05b40c9b032ce2d4c0056ce` highlight `0-12s` rendered
  with the updated readability and effect settings as vertical `1080x1920`
  H.264/AAC, `2,753,735` bytes, `12.02s` (clip `49a969686151498eaf4165b1ff2e4942`).
  The render wrote `render_diagnostics.json` with a `0.0971` zoom at `0.4-4.4s`, a
  `0.12` punch at `1.05-1.95s`, and a `1.0` fade-out at `11.7-12.0s`.
- Real rendered Arabic pixels confirm RTL order: at `0.8s` the amber first word is at
  x `608-715` while the remaining white words are at x `363-587`. Caption text is
  `119px` style with a visible semi-transparent background box (`y 1592-1751`), and
  the text sits inside the bottom safe area.
- Real rendered foreground scaling confirms the effects are visible: the centered
  foreground is `540px` tall at `0s`, `592px` at the `4.4s` zoom peak (`1.0963x`),
  and `615px` at the `1.5s` punch peak (`1.1389x`). A synthetic FFmpeg regression
  test also measures white-square scaling so this cannot regress to config-only.
- English real source `15aa85f23e584aad9b67683c857990fb` rendered as vertical
  `1080x1920` H.264/AAC, `662,524` bytes (clip
  `978ab740834e4057ac2e8ae309b65d0f`), with burned-in `Thank you.` captions and a
  visible dark caption box against the bright source.
- whisper.cpp provider inspection now reports `ready`, `not_installed`,
  `model_missing`, `misconfigured`, `error`, `disabled`, or `unavailable`; the studio
  displays the precise status in English and Arabic. No model is downloaded
  automatically.
- The analysis trim note is informational: the UI no longer implies a custom trim
  control, and the backend still uses detected highlight boundaries.
- Acceptance validation passed for vertical, square, and landscape outputs, including
  full decode, duration tolerance, and source-audio correlation above `0.8`.
- Arabic, English, and Arabic-educational speech were transcribed with Groq Whisper.
- API upload, duplicate detection, transcription, highlights, clip creation,
  download, cancellation, retry, and worker recovery were exercised locally.
- Real Arabic source `a608c91bf05b40c9b032ce2d4c0056ce` (342.08s, 123 transcript
  segments) was re-run deterministically. The old 18s/17.3s fragments at
  `91.52-109.52` and `124.38-141.68` now consolidate into one `91.52-141.68` moment;
  the full deterministic run returns three complete moments instead of five short
  fragments.
- `uv run ruff check .`: clean.
- `uv run mypy clipping_ai`: clean (65 source files).
- `node --check` passes for `app.js`, `api.js`, and `i18n.js`.
- `uv build`: successful; the wheel includes `clipping_ai/frontend`.
- Playwright studio smoke test passed against a temporary local backend, covering
  routing, language/RTL switching, dialogs, empty states, mobile navigation, and
  browser console/page errors.
- Local 60s, 640x360, 30fps FFmpeg-generated measurement: analysis completed in
  `0.94s`; process RSS increased by about `1.4 MiB` during analysis.

## Acceptance findings

- Fixed clip exports failing with `Error opening output files: No such file or
  directory` when configured storage paths were relative. `Settings` now resolves
  storage roots at startup, services use absolute data roots, and the renderer
  resolves source/output/caption paths and creates output parents before invoking
  FFmpeg. Existing failed jobs retry successfully.
- Fixed `test-connection` false negatives for reasoning models by raising the probe
  token budget from `8` to `64`.
- Fixed `--json` stdout contamination by routing LiteLLM logs through stderr.
- Fixed podcast over-classification for dialogue-heavy drama and lectures. Scoring now
  requires keyword evidence for podcast/educational labels and defaults ambiguous
  dialogue to `general`; Arabic educational keywords were added.
- Added regression tests for dialogue-heavy general content, educational keyword
  classification, and reasoning-safe connection probes.

## Known limitations

- Auto classification is heuristic. Arabic keyword coverage is limited, and a lecture
  without recognised keywords may be labelled `general`. Use an explicit
  `content_type` when the category is known.
- Subjective caption placement, readability, and framing still require human review.
  Automated checks verify ASS content, safe-area margins, rendered dimensions,
  frame-level caption pixels, RTL pixel order, background-box contrast, and measured
  effect scaling; they cannot judge taste or full-motion polish. Contact sheets are
  at `/tmp/opencode/visual-acceptance/visual-contact.png`.
- Semantic consolidation is heuristic. It uses transcript gaps, continuity markers,
  and lexical overlap; it does not perform full topic modelling or speaker diarisation.
  Unrelated ideas can still be merged if their bridge text shares enough vocabulary.
- The live LLM path was not re-validated in this environment because the configured
  Gemini provider timed out and fell back. The deterministic real-media run and
  fake-manager LLM regression tests cover both code paths.
- Gaming/live detection is heuristic, not game-aware. It does not recognise game
  titles, kill feeds, HUD text, or OCR. Ambiguous commentary-heavy recordings may
  still need an explicit `content_type=gaming` or `content_type=live`.
- No real podcast/interview or gaming sample was available for end-to-end acceptance.
  Those categories are covered by deterministic fixtures only.
- Local `whisper.cpp` was not available in the acceptance run. It remains optional;
  no binary or model is downloaded automatically.
- Arabic OCR is unavailable in the tested environment because tesseract lacks Arabic
  traineddata.
- Captions use the source transcript language. Automatic caption translation is not
  part of the clip renderer.
- Rendering is local CPU work with one clip worker and in-process serialisation.
  There is no distributed queue. An active FFmpeg render can delay graceful SIGTERM
  shutdown; crash recovery is verified for SIGKILL.
- The studio UI has no source-video streaming or custom trim editor. Clip preview
  uses the rendered download endpoint, and clips use detected highlight boundaries.
- Publishing is a local manual status by default. An optional CLI-only YouTube
  upload/scheduling module exists (off by default, unit-tested with mocked HTTP, not yet
  verified against the live YouTube API). There is no Facebook integration, UI upload,
  or analytics.
- Trash has no automatic expiry or background cleanup. Permanent deletion is explicit.
- Metadata quality depends on transcript context and the configured free model;
  provider failures are shown on the clip and can be regenerated later. Local fallback
  is deterministic and intentionally conservative, not a substitute for AI quality.
- Quota failover uses provider-reported retry windows or conservative local cooldowns;
  it cannot know exact provider-side remaining quota.
- NVIDIA catalog discovery is implemented and live catalog/model-list metadata was
  fetched successfully, but no NVIDIA inference request was made without an API key.
  Free Endpoint resources with API matches remain unverified until a bounded smoke
  test succeeds; FreeLLMAPI has been removed from the bundled configuration.
- Composition is intentionally restrained: at most one short callout from the
  highlight title and a small effect cap. Subjective visual polish still requires
  human review.
- External skills are data-only manifests and remain disabled until review. They
  cannot install code or add tools; enabling one only allows its declared built-in
  tools to run.
- Browser visual checks were performed through automated layout assertions and
  screenshots; subjective visual review is still recommended.

## Next

- Run a human visual/interaction pass over clip detail, metadata editing, publishing,
  selection/bulk actions, Trash, and responsive layouts at desktop and mobile widths.
- Repeat acceptance with real podcast/interview and gaming media, including
  commentary and no-commentary variants.
- Configure and verify local `whisper.cpp` with a multilingual model.
- Add a human visual review pass for caption placement, readability, and framing.
- Add a human visual review pass for the studio UI at desktop and mobile widths,
  including the new visual-settings and Skills controls.
- Set `NVIDIA_API_KEY` and run bounded smoke tests for a small number of discovered
  Free Endpoint models; only then should they be considered Ready.
- Consider optional OCR/game-aware evidence without changing the clip rendering
  contract.
