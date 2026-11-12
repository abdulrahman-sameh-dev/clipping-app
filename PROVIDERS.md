# Providers

## Provider summary

| Provider ID | Adapter | Env var | Used for |
| --- | --- | --- | --- |
| `gemini` | LiteLLM text | `GEMINI_API_KEY` | All text tasks |
| `openrouter` | LiteLLM text | `OPENROUTER_API_KEY` | All text tasks |
| `groq` | LiteLLM text | `GROQ_API_KEY` | All text tasks |
| `groq_speech` | Groq SDK | `GROQ_API_KEY` | Speech-to-text |
| `whisper_cpp` | Local subprocess | none | Speech-to-text |

A missing key disables only that provider. The service still starts and reports the
provider as unavailable.

## Gemini

- Adapter: `LitellmTextProvider`
- LiteLLM prefix: `gemini/`
- Registry models: `gemini-3.8-flash`, `gemini-3.5-flash-lite`,
  `gemini-3.1-flash-lite`
- Free status: manually approved and verified against Google AI Studio pricing on
  the registry date.
- Re-verify the pricing page before relying on a model for long periods.

## OpenRouter

- Adapter: `LitellmTextProvider`
- LiteLLM prefix: `openrouter/`
- Registry model: `openrouter-free-router`
- The example concrete model is disabled and not approved. Replace it with a
  current `:free` model ID and verify it before enabling.
- `clipping-ai verify-free <model-id>` can check OpenRouter pricing metadata for a
  concrete model ID. Routing aliases cannot be verified automatically. Missing or
  malformed pricing metadata is treated as unknown, not free. Confirmed non-zero
  pricing revokes the model's free approval.

## Groq text

- Adapter: `LitellmTextProvider`
- LiteLLM prefix: `groq/`
- Registry models: `groq-gpt-oss-120b`, `groq-gpt-oss-20b`, `groq-qwen3.8-27b`
- Registry daily request limits are local guards based on the documented free plan.
- Groq may change model availability; if a model is retired, disable it in the local
  registry and route to another approved free model.

## Groq speech

- Adapter: `GroqSpeechProvider`
- SDK: official `groq` package
- Registry models: `groq-whisper-turbo`, `groq-whisper-v3`
- Request limit: 25 MB by default; larger files are split with ffmpeg.
- Default chunk length: 600 seconds, no overlap.
- Timestamps: verbose JSON segments and words, offset back to the original file.
- Resume: completed chunks are stored in `data/jobs.db`.

The adapter uses the Groq API key from `GROQ_API_KEY`, the same key as Groq text.
The provider is separate so speech capability and rate limits remain explicit.

## whisper.cpp

- Adapter: `WhisperCppProvider`
- Disabled by default.
- Requires an existing binary and GGML model file.
- Configure `binary` and `model_path` in `config/providers.json`, or set
  `CLIPPING_AI_WHISPER_CPP_BINARY` and `CLIPPING_AI_WHISPER_CPP_MODEL_PATH`.
- No model or binary is downloaded automatically.
- Health inspection distinguishes `ready`, `not_installed`, `model_missing`,
  `misconfigured`, `error`, `disabled`, and `unavailable`. The studio and
  `uv run clipping-ai providers` display the precise state.
- A local build with `tiny.en` and multilingual `tiny` was verified; see
  `TESTING.md` for the exact commands and results. The tiny model is useful for a
  quick smoke test, not production accuracy.

Example:

```json
{
  "providers": {
    "whisper_cpp": {
      "type": "whisper_cpp",
      "enabled": true,
      "binary": "/usr/local/bin/whisper-cli",
      "model_path": "/models/ggml-base.en.bin"
    }
  }
}
```

## Adding a provider

1. Implement `BaseTextProvider` or `BaseSpeechProvider` in `clipping_ai/providers/`.
2. Add a provider `type` in `config/providers.json`.
3. Register models in `config/models.json` with `free: true` only if they are
   genuinely free.
4. Add routing entries in `config/routing.json`.
5. Add offline tests using a fake client. Do not require a real API key in the
   default test suite.

## Error taxonomy

Provider SDK exceptions are normalized into:

- `missing_api_key`
- `authentication_failed`
- `rate_limit_exceeded`
- `free_quota_exhausted`
- `model_unavailable`
- `provider_unavailable`
- `network_timeout`
- `invalid_response`
- `schema_validation_failed`
- `unsupported_capability`
- `invalid_request`
- `policy_violation`
- `unknown_provider_error`

Quota exhaustion is never treated as a normal rate limit. By default it stops the
request instead of falling back.
