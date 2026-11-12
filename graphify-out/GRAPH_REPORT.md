# Graph Report - cliping  (2026-11-11)

## Corpus Check
- 109 files · ~82,588 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1382 nodes · 4578 edges · 60 communities (52 shown, 8 thin omitted)
- Extraction: 90% EXTRACTED · 10% INFERRED · 0% AMBIGUOUS · INFERRED: 474 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Community 0|Community 0]]
- [[_COMMUNITY_Community 1|Community 1]]
- [[_COMMUNITY_Community 2|Community 2]]
- [[_COMMUNITY_Community 3|Community 3]]
- [[_COMMUNITY_Community 4|Community 4]]
- [[_COMMUNITY_Community 5|Community 5]]
- [[_COMMUNITY_Community 6|Community 6]]
- [[_COMMUNITY_Community 7|Community 7]]
- [[_COMMUNITY_Community 8|Community 8]]
- [[_COMMUNITY_Community 9|Community 9]]
- [[_COMMUNITY_Community 10|Community 10]]
- [[_COMMUNITY_Community 11|Community 11]]
- [[_COMMUNITY_Community 12|Community 12]]
- [[_COMMUNITY_Community 13|Community 13]]
- [[_COMMUNITY_Community 14|Community 14]]
- [[_COMMUNITY_Community 15|Community 15]]
- [[_COMMUNITY_Community 16|Community 16]]
- [[_COMMUNITY_Community 17|Community 17]]
- [[_COMMUNITY_Community 18|Community 18]]
- [[_COMMUNITY_Community 19|Community 19]]
- [[_COMMUNITY_Community 20|Community 20]]
- [[_COMMUNITY_Community 21|Community 21]]
- [[_COMMUNITY_Community 22|Community 22]]
- [[_COMMUNITY_Community 23|Community 23]]
- [[_COMMUNITY_Community 24|Community 24]]
- [[_COMMUNITY_Community 25|Community 25]]
- [[_COMMUNITY_Community 26|Community 26]]
- [[_COMMUNITY_Community 27|Community 27]]
- [[_COMMUNITY_Community 28|Community 28]]
- [[_COMMUNITY_Community 29|Community 29]]
- [[_COMMUNITY_Community 30|Community 30]]
- [[_COMMUNITY_Community 31|Community 31]]
- [[_COMMUNITY_Community 32|Community 32]]
- [[_COMMUNITY_Community 33|Community 33]]
- [[_COMMUNITY_Community 34|Community 34]]
- [[_COMMUNITY_Community 35|Community 35]]
- [[_COMMUNITY_Community 36|Community 36]]
- [[_COMMUNITY_Community 37|Community 37]]
- [[_COMMUNITY_Community 38|Community 38]]
- [[_COMMUNITY_Community 39|Community 39]]
- [[_COMMUNITY_Community 41|Community 41]]
- [[_COMMUNITY_Community 42|Community 42]]
- [[_COMMUNITY_Community 43|Community 43]]
- [[_COMMUNITY_Community 44|Community 44]]
- [[_COMMUNITY_Community 45|Community 45]]
- [[_COMMUNITY_Community 46|Community 46]]
- [[_COMMUNITY_Community 47|Community 47]]
- [[_COMMUNITY_Community 48|Community 48]]
- [[_COMMUNITY_Community 49|Community 49]]
- [[_COMMUNITY_Community 50|Community 50]]
- [[_COMMUNITY_Community 51|Community 51]]
- [[_COMMUNITY_Community 52|Community 52]]
- [[_COMMUNITY_Community 53|Community 53]]
- [[_COMMUNITY_Community 54|Community 54]]
- [[_COMMUNITY_Community 55|Community 55]]
- [[_COMMUNITY_Community 56|Community 56]]
- [[_COMMUNITY_Community 57|Community 57]]
- [[_COMMUNITY_Community 58|Community 58]]
- [[_COMMUNITY_Community 59|Community 59]]

## God Nodes (most connected - your core abstractions)
1. `AIError` - 205 edges
2. `ErrorCategory` - 104 edges
3. `TranscriptResult` - 104 edges
4. `ModelEntry` - 77 edges
5. `ProviderManager` - 69 edges
6. `Settings` - 56 edges
7. `VideoConfig` - 51 edges
8. `VideoJob` - 50 edges
9. `TranscriptSegment` - 49 edges
10. `ClipJob` - 48 edges

## Surprising Connections (you probably didn't know these)
- `FakeManager` --uses--> `HighlightSettings`  [INFERRED]
  tests/test_analysis_api.py → clipping_ai/analysis/config.py
- `FakeManager` --uses--> `HighlightSettings`  [INFERRED]
  tests/test_analysis_service.py → clipping_ai/analysis/config.py
- `FakeManager` --uses--> `AnalysisService`  [INFERRED]
  tests/test_analysis_api.py → clipping_ai/analysis/service.py
- `FakeManager` --uses--> `AnalysisService`  [INFERRED]
  tests/test_analysis_service.py → clipping_ai/analysis/service.py
- `FakeGroqClient` --uses--> `AudioChunk`  [INFERRED]
  tests/test_providers.py → clipping_ai/audio.py

## Import Cycles
- None detected.

## Communities (60 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.08
Nodes (89): HighlightSettings, Any, Path, Any, array, Path, TranscriptSegment, _align_to_transcript() (+81 more)

### Community 1 - "Community 1"
Cohesion: 0.06
Nodes (72): Eligibility, FreeOnlyPolicy, Hard zero-cost policy: only explicitly approved free models may execute., Evaluates whether a registry model may be used for a task.      The policy is de, Last-line guard placed immediately before any provider call., PolicyConfig, RouteCandidate, Router (+64 more)

### Community 2 - "Community 2"
Cohesion: 0.19
Nodes (8): Any, Any, Connection, Path, VideoJob, _json(), _now(), VideoJobStore

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (69): AnalysisConfig, _deep_merge(), load_analysis_config(), Configuration for transcription orchestration and highlight detection., _read_json(), TranscriptionSettings, create_app(), __getattr__() (+61 more)

### Community 4 - "Community 4"
Cohesion: 0.18
Nodes (15): Path, Path, test_audio_extraction_and_chunk_offsets(), test_probe_video_normalises_metadata(), test_probe_video_rejects_missing_audio(), test_probe_video_rejects_missing_file_and_long_duration(), test_unsupported_audio_codec_is_rejected(), _codec_args() (+7 more)

### Community 5 - "Community 5"
Cohesion: 0.10
Nodes (11): Any, Path, AIError, Any, BaseException, quota_exhausted(), Safe for API responses: never contains credentials., A provider-independent error with enough context for the UI and logs. (+3 more)

### Community 6 - "Community 6"
Cohesion: 0.19
Nodes (19): build_messages(), extract_json(), parse_and_validate(), Any, BaseModel, Prompt construction, controlled JSON extraction and Pydantic validation., Extract a JSON value from model output, tolerating markdown fences/preamble., repair_messages() (+11 more)

### Community 7 - "Community 7"
Cohesion: 0.12
Nodes (23): Any, ProviderConfig, configure_litellm(), _content_to_text(), LitellmTextProvider, Disable telemetry/verbose logging and never configure paid fallbacks., FakeGroqClient, FakeModels (+15 more)

### Community 8 - "Community 8"
Cohesion: 0.17
Nodes (29): AnalysisService, _analysis(), AnalyzeJobRequest, AnalyzeRequest, ClipCreateRequest, _clips(), ConnectionRequest, HighlightJobRequest (+21 more)

### Community 9 - "Community 9"
Cohesion: 0.15
Nodes (8): Any, Any, Connection, Path, ClipJob, ClipJobStore, _json(), _now()

### Community 10 - "Community 10"
Cohesion: 0.13
Nodes (20): AudioChunk, ffmpeg_available(), _ffprobe_available(), hash_file(), probe_duration(), Path, Audio helpers for chunked speech-to-text.  FFmpeg is used only to split audio wh, Split audio into 16 kHz mono FLAC chunks. Caller owns output_dir lifetime. (+12 more)

### Community 11 - "Community 11"
Cohesion: 0.11
Nodes (28): ALLOWED_EXTENSIONS, analysisView(), breadcrumb, canCancelClip(), canCancelVideo(), canRetryClip(), canRetryVideo(), clipCard() (+20 more)

### Community 12 - "Community 12"
Cohesion: 0.16
Nodes (21): ArgumentParser, build_parser(), main(), test_analysis_commands_pass_options(), test_analysis_config_and_transcript_commands(), test_analysis_parser_accepts_commands(), test_transcript_command_handles_missing_transcript(), test_analyze_command_merges_payload() (+13 more)

### Community 13 - "Community 13"
Cohesion: 0.08
Nodes (23): Analysis and highlights (`clipping_ai/analysis/`, `clipping_ai/highlights/`), Architecture, Clip rendering (`clipping_ai/clips/`), Clip rendering flow, Components, Concurrency and limits, Extension points, FreeOnlyPolicy (`clipping_ai/policy.py`) (+15 more)

### Community 14 - "Community 14"
Cohesion: 0.12
Nodes (10): ABC, Random, Any, Path, BaseSpeechProvider, BaseTextProvider, ConnectionResult, Provider interfaces. Business logic depends on these, not on SDKs. (+2 more)

### Community 15 - "Community 15"
Cohesion: 0.20
Nodes (21): _analysis_service(), _clip_service(), _dump(), _manager(), _print_clip_job(), _print_clip_jobs(), _print_models(), _print_providers() (+13 more)

### Community 16 - "Community 16"
Cohesion: 0.16
Nodes (14): missing_key(), Attempt, _availability_error(), _capabilities_for_type(), _connection_dict(), _no_candidate_error(), ProviderManager, Any (+6 more)

### Community 17 - "Community 17"
Cohesion: 0.09
Nodes (34): Any, BaseException, ConfigError, _deep_merge(), Any, Path, Configuration loading and the explicit free-model registry., _read_json() (+26 more)

### Community 18 - "Community 18"
Cohesion: 0.36
Nodes (6): Reload registry/routing/policy in place (providers are rebuilt)., _has_key(), Optional real-provider smoke tests.  Run with:     CLIPPING_AI_LIVE_TESTS=1 uv r, test_live_gemini_content_analysis(), test_live_groq_transcription(), test_live_openrouter_content_analysis()

### Community 19 - "Community 19"
Cohesion: 0.23
Nodes (17): _CancelCheck, Any, Path, EditPlan, _base_filter(), _build_command(), build_filter_graph(), _cleanup() (+9 more)

### Community 20 - "Community 20"
Cohesion: 0.23
Nodes (7): Any, Connection, Path, Lightweight local usage tracking backed by SQLite.  This records what *we* obser, UsageTracker, _utcnow(), datetime

### Community 21 - "Community 21"
Cohesion: 0.15
Nodes (11): Any, Path, ClipFormatProfile, ClipQualityProfile, _deep_merge(), _default_formats(), _default_qualities(), load_clip_config() (+3 more)

### Community 22 - "Community 22"
Cohesion: 0.12
Nodes (24): formatDateTime(), formatRelative(), attr(), closeDialog(), dialogElement(), esc(), formatClock(), formatDateTimeLabel() (+16 more)

### Community 23 - "Community 23"
Cohesion: 0.06
Nodes (93): Path, CaptionResult, CaptionSegment, ClipEvaluation, ContentAnalysis, GeneratedMetadata, HighlightAnalysis, HighlightSuggestion (+85 more)

### Community 24 - "Community 24"
Cohesion: 0.31
Nodes (6): Any, Path, _offset_seconds(), _parse_timestamp(), Optional local whisper.cpp speech-to-text provider.  Nothing is downloaded autom, WhisperCppProvider

### Community 25 - "Community 25"
Cohesion: 0.14
Nodes (13): Analysis configuration, API, CLI, Clip configuration, Configuration, Environment, Files, Local overrides (+5 more)

### Community 26 - "Community 26"
Cohesion: 0.17
Nodes (4): cli_clip_env(), FakeClipService, test_clip_cli_commands(), test_clip_parser_accepts_commands()

### Community 27 - "Community 27"
Cohesion: 0.20
Nodes (21): cancelClip(), cancelVideo(), cleanup(), createClipsFromForm(), go(), handleGlobalAction(), loadingState(), openClipPreview() (+13 more)

### Community 28 - "Community 28"
Cohesion: 0.14
Nodes (13): Clipping AI, Configure, Features, Install on EndeavourOS, Manual startup, Prepare a video, Quick start, Render clips (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.20
Nodes (9): Adding a provider, Error taxonomy, Gemini, Groq text, Groq speech, whisper.cpp, OpenRouter, Provider summary (+1 more)

### Community 32 - "Community 32"
Cohesion: 0.25
Nodes (7): Enforcement layers, Free-Tier Policy, Goal, Limitations, Quota and rate limits, Verification, What to do when a provider changes

### Community 33 - "Community 33"
Cohesion: 0.29
Nodes (6): Commands, Key invariants covered, Manual whisper.cpp verification, Setup, Test layout, Testing

### Community 35 - "Community 35"
Cohesion: 0.29
Nodes (6): Acceptance findings, Current state, Known limitations, Next, Project Status, Verification

### Community 36 - "Community 36"
Cohesion: 0.16
Nodes (20): BinaryIO, Path, BinaryIO, test_copy_stream_enforces_size_and_hashes(), test_filename_and_extension_validation(), test_job_id_validation(), test_url_validation_and_redaction(), test_yt_dlp_failure_timeout_and_bad_output() (+12 more)

### Community 41 - "Community 41"
Cohesion: 0.09
Nodes (22): 1. Provider readiness, 2. Ingest and prepare, 3. Transcribe, 4. Detect highlights and classify, 5. Render clips, 6. Validate output, 7. Failure cases, 8. Cancellation, retry, and recovery (+14 more)

### Community 42 - "Community 42"
Cohesion: 0.16
Nodes (7): ModelEntry, Loads providers, models, routing and policy from config/ with local overrides., Registry, Task-based routing resolution, independent of provider implementations., ResolvedCandidate, RoutingResult, SkippedCandidate

### Community 43 - "Community 43"
Cohesion: 0.18
Nodes (17): Row, make_job(), store(), test_claim_and_recover(), test_create_get_and_update(), test_fail_retry_and_cancel(), test_find_by_hash_and_source(), test_invalid_and_unknown_ids() (+9 more)

### Community 45 - "Community 45"
Cohesion: 0.33
Nodes (17): CompletedProcess, audio_correlation(), caption_ocr(), caption_pixel_fraction(), caption_reference_diff(), check_ass(), correlation(), frame_rgb() (+9 more)

### Community 46 - "Community 46"
Cohesion: 0.13
Nodes (11): Transcription and highlight-analysis orchestration for prepared video jobs., BaseSettings, Path, Centralised settings. Secrets are only ever read from the environment/.env., Resolve storage roots once so later cwd changes cannot move them., Return a secret value, or None. Never log the returned value., Settings, test_clip_store_status_transitions_and_recovery() (+3 more)

### Community 47 - "Community 47"
Cohesion: 0.31
Nodes (11): clipsView(), clipTitle(), createView(), dashboardView(), highlightFor(), jobsView(), libraryView(), loadSourceJobs() (+3 more)

### Community 48 - "Community 48"
Cohesion: 0.14
Nodes (13): Acceptance Test Results, API results, Artifacts, Automated verification, Bugs found and fixed during acceptance, Classification results, Conclusion, Failure and recovery results (+5 more)

### Community 49 - "Community 49"
Cohesion: 0.22
Nodes (11): Any, Path, test_defaults_are_usable(), test_extensions_and_schemes_are_normalised(), test_invalid_config_is_rejected(), test_invalid_values_are_rejected(), test_local_override_is_deep_merged(), _deep_merge() (+3 more)

### Community 50 - "Community 50"
Cohesion: 0.23
Nodes (10): api, API_BASE, ApiError, errorFromPayload(), get(), parseResponse(), pathWithQuery(), post() (+2 more)

### Community 51 - "Community 51"
Cohesion: 0.24
Nodes (10): applyStaticTranslations(), bindShell(), init(), formatNumber(), getLocale(), messages, setLocale(), toggleLocale() (+2 more)

### Community 52 - "Community 52"
Cohesion: 0.27
Nodes (4): Connection, Path, Persistent cache for transcription chunks so interrupted jobs can resume., TranscriptionCache

### Community 54 - "Community 54"
Cohesion: 0.22
Nodes (8): Color, Components, Design, Imagery, Layout, Motion, Typography, Visual Theme

### Community 55 - "Community 55"
Cohesion: 0.22
Nodes (8): Accessibility & Inclusion, Anti-references, Brand Personality, Design Principles, Product, Product Purpose, Register, Users

### Community 56 - "Community 56"
Cohesion: 0.25
Nodes (7): devDependencies, playwright, name, private, scripts, test:e2e, type

### Community 57 - "Community 57"
Cohesion: 0.40
Nodes (10): Any, make_job(), _as_float(), _as_int(), _audio_stream(), _fps(), Normalised ffprobe metadata extraction., _video_stream() (+2 more)

### Community 58 - "Community 58"
Cohesion: 0.47
Nodes (4): freePort(), repoRoot, startBackend(), waitForHealth()

### Community 59 - "Community 59"
Cohesion: 0.70
Nodes (4): start_local.sh script, cleanup(), fail(), info()

## Knowledge Gaps
- **131 isolated node(s):** `storedBase`, `API_BASE`, `main`, `sidebar`, `scrim` (+126 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AIError` connect `Community 5` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 12`, `Community 15`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 21`, `Community 23`, `Community 24`, `Community 30`, `Community 31`, `Community 36`, `Community 42`, `Community 43`, `Community 44`, `Community 46`, `Community 49`, `Community 53`, `Community 57`?**
  _High betweenness centrality (0.195) - this node is a cross-community bridge._
- **Why does `TranscriptResult` connect `Community 23` to `Community 0`, `Community 1`, `Community 34`, `Community 3`, `Community 2`, `Community 8`, `Community 10`, `Community 43`, `Community 12`, `Community 46`, `Community 14`, `Community 16`, `Community 19`, `Community 52`, `Community 53`, `Community 24`, `Community 57`?**
  _High betweenness centrality (0.082) - this node is a cross-community bridge._
- **Why does `ErrorCategory` connect `Community 8` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 12`, `Community 16`, `Community 17`, `Community 19`, `Community 21`, `Community 23`, `Community 24`, `Community 30`, `Community 31`, `Community 36`, `Community 42`, `Community 43`, `Community 44`, `Community 46`, `Community 49`, `Community 57`?**
  _High betweenness centrality (0.075) - this node is a cross-community bridge._
- **Are the 60 inferred relationships involving `AIError` (e.g. with `AnalysisConfig` and `HighlightSettings`) actually correct?**
  _`AIError` has 60 INFERRED edges - model-reasoned connections that need verification._
- **Are the 59 inferred relationships involving `ErrorCategory` (e.g. with `AnalysisConfig` and `HighlightSettings`) actually correct?**
  _`ErrorCategory` has 59 INFERRED edges - model-reasoned connections that need verification._
- **Are the 32 inferred relationships involving `TranscriptResult` (e.g. with `AnalysisService` and `TranscriptionCache`) actually correct?**
  _`TranscriptResult` has 32 INFERRED edges - model-reasoned connections that need verification._
- **Are the 21 inferred relationships involving `ModelEntry` (e.g. with `Attempt` and `ProviderManager`) actually correct?**
  _`ModelEntry` has 21 INFERRED edges - model-reasoned connections that need verification._