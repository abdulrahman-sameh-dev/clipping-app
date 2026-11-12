# Free-Tier Policy

## Goal

The system must never incur a paid API charge. Free-tier usage is allowed only
within each provider's current free-plan terms and limits.

## Enforcement layers

A request can reach a provider only after all of these checks:

1. **Registry membership**: the model ID must exist in `config/models.json` or a
   local override.
2. **Explicit approval**: `free: true`.
3. **Enablement**: `status: "enabled"`.
4. **Task support**: the model must list the requested task.
5. **Verification policy**: if `require_verified_free` is true, the model must also
   have `free_verified: true`.
6. **Provider availability**: the provider must be enabled and have its API key.
7. **Last-line guard**: the manager checks `free` immediately before each provider
   call, and the text and speech adapters independently reject models that are not
   `free` and enabled.
8. **No paid fallback**: fallback can only choose another approved free model. Quota
   exhaustion stops execution by default.

API callers cannot pass an arbitrary provider model string. Explicit model overrides
must resolve to a registered model.

## Quota and rate limits

Providers expose different quota information, and some expose none through a common
API. This system distinguishes:

- **Rate limit**: temporary; may be retried or may fall back depending on policy.
- **Free quota exhausted**: the free allowance is used up; by default the request
  stops. It must not trigger a paid request.

Local daily counters are guards based on configured limits. They are not authoritative
remaining-quota values. The usage dashboard labels them as local observations.

## Verification

- Gemini and Groq free eligibility is manually verified against provider pricing and
  rate-limit documentation and recorded in the registry.
- OpenRouter concrete models can be checked with `clipping-ai verify-free <model-id>`
  against the public models pricing endpoint. Routing aliases cannot be verified
  automatically. Missing or malformed pricing metadata is treated as unknown, not
  free. Confirmed non-zero pricing revokes free approval.
- Verification results are written to `config/local/models.json`.
- Pricing and free-tier terms change. Re-verify periodically and disable retired or
  paid models.

## What to do when a provider changes

1. Disable the affected model or provider in `config/local/`.
2. Re-route the task to another approved free model.
3. Re-run `clipping-ai status --health`.
4. Update the verification date and source.
5. Do not enable a paid model as a workaround.

## Limitations

- Free-tier terms are controlled by providers, not this code.
- Provider-side quotas may be reached before local counters predict.
- A model that was free may become paid; the registry must be maintained.
- The system cannot guarantee availability of any third-party free tier.
