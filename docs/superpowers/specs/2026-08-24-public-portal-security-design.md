# Public Portal Security Design

**Status:** Approved
**Date:** 2026-08-24

## Goal

Make the shareable `testExplain` Render portal safe enough for trusted
colleague feedback without accepting caller-controlled network destinations or
unlimited server-funded model requests.

## Provider Boundary

The browser selects a named provider, supplies its own API key and model, and
never supplies a provider URL. The API maps provider names to fixed public
endpoints:

| Provider | Gateway | Fixed endpoint |
| --- | --- | --- |
| `openai` | `OpenAICompatibleGateway` | `https://api.openai.com/v1` |
| `anthropic` | `AnthropicGateway` | Anthropic Messages API |
| `openrouter` | `OpenAICompatibleGateway` | `https://openrouter.ai/api/v1` |
| `demo` | server-configured gateway | `LLM_BASE_URL` |

OpenRouter gives colleagues access to other frontier models exposed by that
router. Direct Anthropic access uses a supplied Anthropic key; the existing
gateway is extended only to accept an explicit key. Arbitrary `base_url`
values are rejected. A complete BYOK request requires a supported provider,
nonblank API key, and nonblank model.

## Demo Spending Boundary

`demo=true` uses the server configuration and is limited to 10 requests per
client IP during each one-hour in-memory window. The counter resets when the
free Render instance restarts, which is accepted for this short-lived feedback
portal. The limit is applied before analysis begins and returns HTTP 429
without calling the gateway or analysis pipeline.

This limit is process-local. A multi-worker or horizontally scaled deployment
can give the same client a separate budget per process. The chosen Render demo
uses one Uvicorn process, and this limitation is accepted alongside the
provider spending cap. A production service would replace the dictionary with
an atomic shared store such as Redis.

The rate limiter is not authentication or durable billing protection. Render
environment variables, a low-cost demo model, and a provider spending cap
remain required operational controls.

`TRUSTED_PROXY_IPS` is a comma-separated deployment setting for the immediate
Render ingress peer addresses. Only when `request.client.host` matches this
setting does the quota use the first `X-Forwarded-For` address. With the
setting absent or incorrect, the quota deliberately uses the direct peer
instead of trusting a caller-supplied header; this can make colleagues share a
bucket, but cannot be bypassed by spoofing headers. Set the value from the
Render service's observed ingress topology before enabling the server demo key.

## Public Route Boundary

The legacy `GET /analyze?report_path=...` endpoint is disabled by default. It
is available only when `TESTEXPLAIN_ENABLE_LOCAL_PATH_API=true`, for local
development. The public default returns 404 without reflecting caller-supplied
paths or accessing the filesystem. Hosted use is upload-only.

## Sample Reliability

Tests fetch all four shipped samples. ZIP tests inspect archive members so the
trace-only bundle proves it has no HAR and the trace-plus-HAR bundle proves it
has both artifacts. The fixed sample allowlist remains the only public sample
file access path.

## Error Handling

- Unknown providers, caller-supplied base URLs, incomplete BYOK data, and
  malformed native reports return HTTP 422 before any provider call.
- Demo quota exhaustion returns HTTP 429 before any provider call.
- Public local-path requests return HTTP 404.
- Direct provider keys are transient request values and are neither logged nor
  stored.

## Out Of Scope

- Authentication, persistent/durable distributed rate limiting, user accounts,
  token billing, or result storage.
- Arbitrary private gateways, LAN inference servers, or custom URL support on
  the public service. These remain appropriate only for local execution.
