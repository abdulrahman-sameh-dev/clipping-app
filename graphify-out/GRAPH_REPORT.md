# Graph Report - cliping  (2026-11-14)

## Corpus Check
- 130 files · ~106,361 words
- Verdict: corpus is large enough that graph structure adds value.

## Summary
- 1674 nodes · 5767 edges · 74 communities (66 shown, 8 thin omitted)
- Extraction: 88% EXTRACTED · 12% INFERRED · 0% AMBIGUOUS · INFERRED: 713 edges (avg confidence: 0.52)
- Token cost: 0 input · 0 output

## Graph Freshness
- Built from commit: `1fc440f4`
- Run `git rev-parse HEAD` and compare to check if the graph is stale.
- Run `graphify update .` after code changes (no API cost).

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
- [[_COMMUNITY_Community 60|Community 60]]
- [[_COMMUNITY_Community 61|Community 61]]
- [[_COMMUNITY_Community 62|Community 62]]
- [[_COMMUNITY_Community 63|Community 63]]
- [[_COMMUNITY_Community 64|Community 64]]
- [[_COMMUNITY_Community 65|Community 65]]
- [[_COMMUNITY_Community 66|Community 66]]
- [[_COMMUNITY_Community 67|Community 67]]
- [[_COMMUNITY_Community 68|Community 68]]
- [[_COMMUNITY_Community 69|Community 69]]
- [[_COMMUNITY_Community 70|Community 70]]
- [[_COMMUNITY_Community 71|Community 71]]
- [[_COMMUNITY_Community 72|Community 72]]
- [[_COMMUNITY_Community 73|Community 73]]

## God Nodes (most connected - your core abstractions)
1. `AIError` - 258 edges
2. `ErrorCategory` - 133 edges
3. `TranscriptResult` - 109 edges
4. `ProviderManager` - 92 edges
5. `ModelEntry` - 88 edges
6. `ClipService` - 76 edges
7. `ClipJob` - 75 edges
8. `ClipConfig` - 60 edges
9. `Settings` - 60 edges
10. `VideoJob` - 58 edges

## Surprising Connections (you probably didn't know these)
- `FakeManager` --uses--> `HighlightSettings`  [INFERRED]
  tests/test_analysis_api.py → clipping_ai/analysis/config.py
- `FakeManager` --uses--> `HighlightSettings`  [INFERRED]
  tests/test_analysis_service.py → clipping_ai/analysis/config.py
- `FakeMetadataManager` --uses--> `HighlightSettings`  [INFERRED]
  tests/test_metadata_trash.py → clipping_ai/analysis/config.py
- `FakeManager` --uses--> `AnalysisService`  [INFERRED]
  tests/test_analysis_api.py → clipping_ai/analysis/service.py
- `FakeManager` --uses--> `AnalysisService`  [INFERRED]
  tests/test_analysis_service.py → clipping_ai/analysis/service.py

## Import Cycles
- None detected.

## Communities (74 total, 8 thin omitted)

### Community 0 - "Community 0"
Cohesion: 0.06
Nodes (108): HighlightSettings, Any, Path, Any, array, Path, TranscriptSegment, _align_to_transcript() (+100 more)

### Community 1 - "Community 1"
Cohesion: 0.26
Nodes (26): RouteCandidate, completion(), FakeTextProvider, make_manager(), make_registry(), Path, quota_error(), text_model() (+18 more)

### Community 2 - "Community 2"
Cohesion: 0.19
Nodes (15): Any, EditPlan, Effect, _build_callouts(), build_composition_plan(), _callout_text(), Build a deterministic composition plan from existing edit decisions., _scale_effects() (+7 more)

### Community 3 - "Community 3"
Cohesion: 0.06
Nodes (64): AnalysisConfig, _deep_merge(), load_analysis_config(), Configuration for transcription orchestration and highlight detection., _read_json(), TranscriptionSettings, Any, Path (+56 more)

### Community 4 - "Community 4"
Cohesion: 0.19
Nodes (14): Path, Path, test_audio_extraction_and_chunk_offsets(), test_probe_video_normalises_metadata(), test_probe_video_rejects_missing_audio(), test_probe_video_rejects_missing_file_and_long_duration(), test_unsupported_audio_codec_is_rejected(), _codec_args() (+6 more)

### Community 5 - "Community 5"
Cohesion: 0.08
Nodes (15): Any, Path, AIError, Any, BaseException, quota_exhausted(), Safe for API responses: never contains credentials., A provider-independent error with enough context for the UI and logs. (+7 more)

### Community 6 - "Community 6"
Cohesion: 0.19
Nodes (19): build_messages(), extract_json(), parse_and_validate(), Any, BaseModel, Prompt construction, controlled JSON extraction and Pydantic validation., Extract a JSON value from model output, tolerating markdown fences/preamble., repair_messages() (+11 more)

### Community 7 - "Community 7"
Cohesion: 0.10
Nodes (26): Any, ProviderConfig, configure_litellm(), _content_to_text(), LitellmTextProvider, Disable telemetry/verbose logging and never configure paid fallbacks., Any, speech_model() (+18 more)

### Community 8 - "Community 8"
Cohesion: 0.27
Nodes (33): AnalysisService, AnalyzeJobRequest, AnalyzeRequest, ClipCreateRequest, ClipMetadataGenerateRequest, ClipMetadataUpdateRequest, ClipPublishRequest, ConnectionRequest (+25 more)

### Community 9 - "Community 9"
Cohesion: 0.08
Nodes (22): Any, Any, Any, Connection, Path, GeneratedMetadataBatch, GeneratedMetadataItem, _BatchEntry (+14 more)

### Community 10 - "Community 10"
Cohesion: 0.22
Nodes (6): Any, Path, SkillRecord, _now(), Skill discovery with explicit review/enable state for external manifests., SkillRegistry

### Community 11 - "Community 11"
Cohesion: 0.09
Nodes (27): ALLOWED_EXTENSIONS, analysisView(), applyStaticTranslations(), bindShell(), breadcrumb, canCancelVideo(), canRetryVideo(), cleanup() (+19 more)

### Community 12 - "Community 12"
Cohesion: 0.15
Nodes (22): ArgumentParser, build_parser(), main(), test_analysis_commands_pass_options(), test_analysis_config_and_transcript_commands(), test_analysis_parser_accepts_commands(), test_transcript_command_handles_missing_transcript(), test_analyze_command_merges_payload() (+14 more)

### Community 13 - "Community 13"
Cohesion: 0.07
Nodes (26): Analysis and highlights (`clipping_ai/analysis/`, `clipping_ai/highlights/`), Architecture, Clip rendering (`clipping_ai/clips/`), Clip rendering flow, Components, Concurrency and limits, Extension points, FreeOnlyPolicy (`clipping_ai/policy.py`) (+18 more)

### Community 14 - "Community 14"
Cohesion: 0.28
Nodes (6): Any, Path, _offset_seconds(), _parse_timestamp(), Optional local whisper.cpp speech-to-text provider.  Nothing is downloaded autom, WhisperCppProvider

### Community 15 - "Community 15"
Cohesion: 0.17
Nodes (22): _analysis_service(), _clip_service(), _dump(), _manager(), _print_clip_job(), _print_clip_jobs(), _print_models(), _print_providers() (+14 more)

### Community 16 - "Community 16"
Cohesion: 0.13
Nodes (15): missing_key(), Attempt, _availability_error(), _capabilities_for_type(), _connection_dict(), _no_candidate_error(), ProviderManager, Any (+7 more)

### Community 17 - "Community 17"
Cohesion: 0.13
Nodes (26): Any, BaseException, RetryConfig, compute_delay(), Random, Retry helpers: exponential backoff, jitter and Retry-After support., Return seconds to wait before the next attempt (attempt is 1-based)., Exception (+18 more)

### Community 18 - "Community 18"
Cohesion: 0.09
Nodes (23): ABC, SpeechExecution, TextExecution, Last-line guard placed immediately before any provider call., Any, Path, Any, ModelEntry (+15 more)

### Community 19 - "Community 19"
Cohesion: 0.20
Nodes (18): _CancelCheck, Any, Path, ClipConfig, _base_filter(), _build_command(), build_filter_graph(), _cleanup() (+10 more)

### Community 20 - "Community 20"
Cohesion: 0.23
Nodes (7): Any, Connection, Path, Lightweight local usage tracking backed by SQLite.  This records what *we* obser, UsageTracker, _utcnow(), datetime

### Community 21 - "Community 21"
Cohesion: 0.12
Nodes (15): Any, Path, ClipFormatProfile, ClipQualityProfile, _deep_merge(), _default_formats(), _default_qualities(), load_clip_config() (+7 more)

### Community 22 - "Community 22"
Cohesion: 0.10
Nodes (31): formatDateTime(), formatNumber(), formatRelative(), getLocale(), messages, setLocale(), toggleLocale(), attr() (+23 more)

### Community 23 - "Community 23"
Cohesion: 0.06
Nodes (84): Path, CaptionResult, CaptionSegment, ClipEvaluation, ContentAnalysis, GeneratedMetadata, HighlightAnalysis, HighlightSuggestion (+76 more)

### Community 24 - "Community 24"
Cohesion: 0.18
Nodes (12): ConfigError, _deep_merge(), Any, Path, Configuration loading and the explicit free-model registry., _read_json(), test_real_config_loads_expected_registry(), test_relative_storage_paths_resolve_once_at_startup() (+4 more)

### Community 25 - "Community 25"
Cohesion: 0.13
Nodes (14): Analysis configuration, API, CLI, Clip configuration, Configuration, Environment, Files, Local overrides (+6 more)

### Community 26 - "Community 26"
Cohesion: 0.17
Nodes (4): cli_clip_env(), FakeClipService, test_clip_cli_commands(), test_clip_parser_accepts_commands()

### Community 27 - "Community 27"
Cohesion: 0.23
Nodes (21): cancelClip(), cancelVideo(), createClipsFromForm(), go(), handleGlobalAction(), loadingState(), openUploadDialog(), openUrlDialog() (+13 more)

### Community 28 - "Community 28"
Cohesion: 0.14
Nodes (13): Clipping AI, Configure, Features, Install on EndeavourOS, Manual startup, Prepare a video, Quick start, Render clips (+5 more)

### Community 29 - "Community 29"
Cohesion: 0.20
Nodes (9): Adding a provider, Error taxonomy, Gemini, Groq text, Groq speech, whisper.cpp, OpenRouter, Provider summary (+1 more)

### Community 31 - "Community 31"
Cohesion: 0.40
Nodes (10): Any, make_job(), _as_float(), _as_int(), _audio_stream(), _fps(), Normalised ffprobe metadata extraction., _video_stream() (+2 more)

### Community 32 - "Community 32"
Cohesion: 0.25
Nodes (7): Enforcement layers, Free-Tier Policy, Goal, Limitations, Quota and rate limits, Verification, What to do when a provider changes

### Community 33 - "Community 33"
Cohesion: 0.29
Nodes (6): Commands, Key invariants covered, Manual whisper.cpp verification, Setup, Test layout, Testing

### Community 34 - "Community 34"
Cohesion: 0.24
Nodes (13): Eligibility, FreeOnlyPolicy, Hard zero-cost policy: only explicitly approved free models may execute., Evaluates whether a registry model may be used for a task.      The policy is de, PolicyConfig, model(), test_disabled_model_is_blocked(), test_fallback_policy_allows_free_quota_failover_by_default() (+5 more)

### Community 35 - "Community 35"
Cohesion: 0.29
Nodes (6): Acceptance findings, Current state, Known limitations, Next, Project Status, Verification

### Community 36 - "Community 36"
Cohesion: 0.17
Nodes (19): BinaryIO, Path, test_copy_stream_enforces_size_and_hashes(), test_filename_and_extension_validation(), test_job_id_validation(), test_url_validation_and_redaction(), test_yt_dlp_failure_timeout_and_bad_output(), test_yt_dlp_rejects_unsupported_or_large_output() (+11 more)

### Community 41 - "Community 41"
Cohesion: 0.08
Nodes (23): 1. Provider readiness, 2. Ingest and prepare, 3. Transcribe, 4. Detect highlights and classify, 5. Render clips, 6. Validate output, 7. Failure cases, 8. Cancellation, retry, and recovery (+15 more)

### Community 42 - "Community 42"
Cohesion: 0.13
Nodes (14): AudioChunk, ffmpeg_available(), policy_violation(), Connection, Path, Persistent cache for transcription chunks so interrupted jobs can resume., TranscriptionCache, Any (+6 more)

### Community 43 - "Community 43"
Cohesion: 0.18
Nodes (9): _analysis(), _clips(), _manager(), _trash(), _video(), Any, Unified soft-delete, restore, and permanent deletion across sources and clips., TrashService (+1 more)

### Community 44 - "Community 44"
Cohesion: 0.05
Nodes (32): _now(), Path, Any, BinaryIO, Path, Any, Connection, Path (+24 more)

### Community 45 - "Community 45"
Cohesion: 0.33
Nodes (17): CompletedProcess, audio_correlation(), caption_ocr(), caption_pixel_fraction(), caption_reference_diff(), check_ass(), correlation(), frame_rgb() (+9 more)

### Community 46 - "Community 46"
Cohesion: 0.14
Nodes (7): BaseSettings, Path, Resolve storage roots once so later cwd changes cannot move them., Return a secret value, or None. Never log the returned value., Settings, cli_analysis_env(), FakeAnalysisService

### Community 47 - "Community 47"
Cohesion: 0.19
Nodes (18): canCancelClip(), canRetryClip(), clipCard(), clipDate(), clipDetailHref(), clipDetailView(), clipMeta(), clipPublicMeta() (+10 more)

### Community 48 - "Community 48"
Cohesion: 0.11
Nodes (17): Acceptance Test Results, API results, Artifacts, Automated editing validation, Automated verification, Bugs found and fixed during acceptance, Classification results, Conclusion (+9 more)

### Community 49 - "Community 49"
Cohesion: 0.17
Nodes (12): Transcription and highlight-analysis orchestration for prepared video jobs., Any, Path, test_defaults_are_usable(), test_extensions_and_schemes_are_normalised(), test_invalid_config_is_rejected(), test_invalid_values_are_rejected(), test_local_override_is_deep_merged() (+4 more)

### Community 50 - "Community 50"
Cohesion: 0.21
Nodes (12): api, API_BASE, ApiError, del(), errorFromPayload(), get(), parseResponse(), patch() (+4 more)

### Community 51 - "Community 51"
Cohesion: 0.17
Nodes (10): Typed error taxonomy shared by every provider, the manager and the API., In-process request throttling for a single local user., A small per-key token/interval limiter.      It intentionally does not try to mo, Throttle, Shared test doubles and factories., test_throttle_allows_first_request_and_resets(), test_throttle_rate_window_rejects_long_wait(), test_throttle_rate_window_waits_then_allows() (+2 more)

### Community 52 - "Community 52"
Cohesion: 0.40
Nodes (14): _build(), FakeBatchManager, _items(), _new_clip(), Any, Path, test_batch_metadata_maps_each_clip_and_is_idempotent(), test_batch_metadata_omitted_item_uses_local_fallback() (+6 more)

### Community 53 - "Community 53"
Cohesion: 0.33
Nodes (10): _ffprobe_available(), hash_file(), probe_duration(), Path, Audio helpers for chunked speech-to-text.  FFmpeg is used only to split audio wh, Split audio into 16 kHz mono FLAC chunks. Caller owns output_dir lifetime., split_audio(), test_ffmpeg_splits_audio_and_preserves_offsets() (+2 more)

### Community 54 - "Community 54"
Cohesion: 0.22
Nodes (8): Color, Components, Design, Imagery, Layout, Motion, Typography, Visual Theme

### Community 55 - "Community 55"
Cohesion: 0.22
Nodes (8): Accessibility & Inclusion, Anti-references, Brand Personality, Design Principles, Product, Product Purpose, Register, Users

### Community 56 - "Community 56"
Cohesion: 0.22
Nodes (8): devDependencies, playwright, name, private, scripts, test:e2e, test:frontend, type

### Community 57 - "Community 57"
Cohesion: 0.20
Nodes (14): create_app(), __getattr__(), Build the default app lazily so importing this module has no side effects., Local FastAPI surface for the clipping AI provider system., Any, FastAPI, _config(), FakeMetadataManager (+6 more)

### Community 58 - "Community 58"
Cohesion: 0.47
Nodes (4): freePort(), repoRoot, startBackend(), waitForHealth()

### Community 59 - "Community 59"
Cohesion: 0.70
Nodes (4): start_local.sh script, cleanup(), fail(), info()

### Community 60 - "Community 60"
Cohesion: 0.29
Nodes (6): _client(), Path, test_root_redirects_to_studio(), test_studio_assets_are_served(), test_studio_index_serves_app_shell(), test_studio_unknown_asset_is_not_found()

### Community 61 - "Community 61"
Cohesion: 0.36
Nodes (6): Reload registry/routing/policy in place (providers are rebuilt)., _has_key(), Optional real-provider smoke tests.  Run with:     CLIPPING_AI_LIVE_TESTS=1 uv r, test_live_gemini_content_analysis(), test_live_groq_transcription(), test_live_openrouter_content_analysis()

### Community 62 - "Community 62"
Cohesion: 0.28
Nodes (13): clipsView(), createView(), dashboardView(), exportView(), fitName(), jobsView(), libraryView(), loadSourceJobs() (+5 more)

### Community 63 - "Community 63"
Cohesion: 0.27
Nodes (10): Task-based routing resolution, independent of provider implementations., ResolvedCandidate, Router, SkippedCandidate, registry(), test_explicit_model_alias_is_resolved_only_if_registered(), test_explicit_paid_model_is_never_a_candidate(), test_max_fallback_candidates_is_enforced() (+2 more)

### Community 64 - "Community 64"
Cohesion: 0.16
Nodes (8): _external_manifest(), _FakeClient, _FakeResponse, test_external_skills_require_review_before_enable(), test_provider_toggle_api_persists_enablement(), test_skill_executor_enforces_timeout(), test_skill_executor_runs_declared_builtin_tool(), test_visual_settings_defaults_and_intensity_scaling()

### Community 65 - "Community 65"
Cohesion: 0.44
Nodes (10): TestClient, app_for(), test_analyze_endpoint_maps_missing_key_to_503(), test_analyze_endpoint_maps_policy_error_to_403(), test_analyze_endpoint_returns_structured_data(), test_models_endpoint_filters_by_task(), test_read_only_endpoints(), test_test_connection_endpoint() (+2 more)

### Community 67 - "Community 67"
Cohesion: 0.43
Nodes (4): fakeClock(), setup(), startPolling(), createPoller()

### Community 68 - "Community 68"
Cohesion: 0.28
Nodes (8): Any, _callout_plan(), _caption_plan(), _explainer_plan(), _moment_plan(), Registered built-in tools available to skills.  External manifests can reference, ToolRegistry, ToolSpec

### Community 69 - "Community 69"
Cohesion: 0.25
Nodes (8): Path, plan_language(), Callout, _ass_time(), _escape(), Render lightweight callouts to ASS so libass handles Arabic shaping., _wrap(), write_overlay_ass()

### Community 71 - "Community 71"
Cohesion: 0.33
Nodes (3): Skill manifest and permission models., SkillManifest, SkillPermissions

## Knowledge Gaps
- **141 isolated node(s):** `storedBase`, `API_BASE`, `main`, `sidebar`, `scrim` (+136 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **8 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `AIError` connect `Community 5` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 6`, `Community 7`, `Community 8`, `Community 9`, `Community 10`, `Community 12`, `Community 14`, `Community 15`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 21`, `Community 23`, `Community 24`, `Community 30`, `Community 31`, `Community 34`, `Community 36`, `Community 42`, `Community 43`, `Community 44`, `Community 49`, `Community 51`, `Community 52`, `Community 53`, `Community 57`, `Community 61`, `Community 64`, `Community 66`, `Community 70`?**
  _High betweenness centrality (0.226) - this node is a cross-community bridge._
- **Why does `ErrorCategory` connect `Community 8` to `Community 0`, `Community 1`, `Community 3`, `Community 4`, `Community 5`, `Community 6`, `Community 7`, `Community 9`, `Community 10`, `Community 12`, `Community 14`, `Community 16`, `Community 17`, `Community 18`, `Community 19`, `Community 21`, `Community 23`, `Community 24`, `Community 30`, `Community 31`, `Community 34`, `Community 36`, `Community 42`, `Community 43`, `Community 44`, `Community 49`, `Community 51`, `Community 52`, `Community 53`, `Community 57`, `Community 63`, `Community 64`, `Community 66`, `Community 70`?**
  _High betweenness centrality (0.088) - this node is a cross-community bridge._
- **Why does `TranscriptResult` connect `Community 42` to `Community 0`, `Community 1`, `Community 2`, `Community 3`, `Community 65`, `Community 8`, `Community 44`, `Community 12`, `Community 14`, `Community 46`, `Community 16`, `Community 49`, `Community 18`, `Community 51`, `Community 52`, `Community 23`, `Community 57`, `Community 31`?**
  _High betweenness centrality (0.055) - this node is a cross-community bridge._
- **Are the 81 inferred relationships involving `AIError` (e.g. with `AnalysisConfig` and `HighlightSettings`) actually correct?**
  _`AIError` has 81 INFERRED edges - model-reasoned connections that need verification._
- **Are the 80 inferred relationships involving `ErrorCategory` (e.g. with `AnalysisConfig` and `HighlightSettings`) actually correct?**
  _`ErrorCategory` has 80 INFERRED edges - model-reasoned connections that need verification._
- **Are the 33 inferred relationships involving `TranscriptResult` (e.g. with `AnalysisService` and `TranscriptionCache`) actually correct?**
  _`TranscriptResult` has 33 INFERRED edges - model-reasoned connections that need verification._
- **Are the 45 inferred relationships involving `ProviderManager` (e.g. with `AnalysisService` and `AnalyzeJobRequest`) actually correct?**
  _`ProviderManager` has 45 INFERRED edges - model-reasoned connections that need verification._