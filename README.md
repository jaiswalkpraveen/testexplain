# testExplain

> Most test reports tell you **what** failed. testExplain tells you **why** — and (soon) proves its answers are right.

testExplain is an AI-native test-failure triage platform. It ingests Playwright reports and uses an LLM to explain, in plain English, why each test failed and what to check first. It's built milestone-by-milestone as a learning-grade, framework-free AI engineering project.

**Status:** Milestone 0 established the walking skeleton. Milestone 1 adds structured, validated LLM output with corrective retries. Milestone 2 now builds multi-source, redacted evidence before prompt composition.

## Milestone progress

### M0 — Walking skeleton

M0 proved the complete path from a Playwright report to an LLM-generated explanation exposed through both a CLI and an HTTP API:

```text
report.json ──▶ ingestion ──▶ FailureContext ──▶ core ──▶ Gateway (LLM) ──▶ FailureAnalysis
                   (parse)       (normalized)      (prompt)    (swappable)        (result)
                                                                                     │
                                                     ┌───────────────────────────────┴──────────────┐
                                                     ▼                                              ▼
                                           CLI (testexplain analyze)                      HTTP API (/analyze)
```

The M0 output was one free-text explanation. It was useful for a person to read, but software could not reliably filter, sort, or aggregate it.

### M1 — Structured outputs and self-correction

M1 turns the LLM reply into a machine-usable contract:

```text
LLM raw text
    │
    ▼
strip optional Markdown fences
    │
    ▼
parse JSON
    │
    ▼
validate with Pydantic
    │
    ├── valid ──▶ FailureAnalysis
    │
    └── invalid ──▶ send the validation error back to the LLM and retry
                    (up to 3 total attempts, then fail loudly)
```

Each `FailureAnalysis` now contains:

- `test_title`
- `summary`
- `suspected_category` from a controlled category list
- `evidence` as a list
- `next_steps` as a list
- `confidence` between `0.0` and `1.0`

Example CLI output:

```text
### user sees dashboard after login
[flaky] (confidence: 50%)
The test likely timed out while waiting for the dashboard.
Evidence:
 - Timeout 30000ms exceeded
Next steps:
 - Check whether the dashboard API was slow
 - Re-run the test to determine whether the failure is intermittent
```

The prompt asks for JSON, but testExplain does not trust the model to comply. Every reply is parsed and validated at the system boundary. Invalid JSON, invented categories, missing fields, or out-of-range confidence values trigger a corrective retry.

### M2 — Multi-source context and safe evidence

M2 moves the pipeline from one failure summary to attempt-aware evidence assembled from the artifacts that explain a failure:

```text
report.json or bundle.zip
          │
          ▼
 safe input reader ──▶ attempt-aware report parser
          │                         │
          └──────────────┬──────────┘
                         ▼
              trace and HAR adapters
                         │
                         ▼
                 normalized Evidence
                         │
                         ▼
        deterministic redaction before prompt composition
```

M2 Tasks 1–13 now provide:

- typed `Evidence` and `FailedAttempt` contracts with provenance and severity
- recursive, retry-aware Playwright report ingestion
- safe report and bundle input handling with bounded artifact access
- trace evidence for actions, console messages, page errors, stdout, stderr, and network events
- HAR fallback evidence with bounded response-body previews
- deterministic, idempotent redaction of authorization values, cookies, API keys, query secrets, JSON secret fields, and vendor-prefixed tokens in normalized evidence
- pure redaction helpers that preserve evidence metadata while returning new objects
- deterministic severity/temporal ranking, network deduplication, and a 4,000-token evidence budget
- attempt-aware prompt orchestration with project, retry, flaky, provenance, and evidence-gap context
- deterministic CLI bundle packaging with safe rewritten artifact paths
- multipart ZIP uploads through `POST /analyze-bundle`, with streaming size limits and temporary-file cleanup
- synthetic production-shaped fixtures and prompt ablation tests proving report-only, trace, and trace-plus-HAR context

The security boundary is deliberate: common secrets are redacted from normalized artifact evidence before that evidence is composed into a prompt. This matters because prompt content is sent to a third-party model, and the model may quote evidence in its response. Report metadata, error messages, and stack traces remain caller-supplied prompt fields and must be reviewed separately. Generic URL path segments and locator-shaped action arguments remain documented limitations because they cannot be distinguished reliably from ordinary diagnostic values without more context.

The M2 workflow is documented in [Collecting Playwright Artifacts](docs/guides/collecting-artifacts.md). The guide covers native JSON reports, trace/HAR attachment paths, offline validation, ZIP bundling, API upload, and troubleshooting.

## Gateway design

All model calls go through the `Gateway` Protocol. The analysis pipeline depends only on a `generate(prompt) -> str` shape, not on a particular model provider.

- `FakeGateway` provides deterministic offline tests and dry runs.
- `OpenAICompatibleGateway` supports hosted routers and self-hosted inference servers through the standard `/v1/chat/completions` API.
- `AnthropicGateway` remains available for a direct Anthropic integration.

The CLI and API currently use `OpenAICompatibleGateway` for real analysis. Changing the model, router, or endpoint requires configuration rather than changes to the core pipeline.

## Quick start

### 1. Install dependencies

```bash
uv sync
```

### 2. Try the offline fake gateway

No API key or network connection is required:

```bash
make run-cli-fake
```

Or run the command directly:

```bash
uv run testexplain analyze tests/fixtures/sample_report.json --fake
```

### 3. Configure a real OpenAI-compatible endpoint

Copy the environment template:

```bash
cp .env.example .env
```

Edit `.env` and provide the endpoint, key, and model. For Gateframe:

```env
LLM_BASE_URL=https://router.gateframe.ai/v1
LLM_API_KEY=your-gateframe-key
LLM_MODEL=gateframe/opus-4.7
```

`.env` is ignored by Git. Never commit a real API key. The tracked `.env.example` file documents the required variable names without storing secrets.

For a no-auth self-hosted endpoint, keep `LLM_API_KEY=` present but empty. testExplain supplies an internal placeholder because the OpenAI client requires a non-empty value even when the server ignores authentication.

Run a real analysis:

```bash
uv run testexplain analyze tests/fixtures/sample_report.json
```

### 4. Run the HTTP API

```bash
make run-api
```

Then open `http://127.0.0.1:8000/docs`. The development-only local-path endpoint is:

```text
GET /analyze?report_path=tests/fixtures/sample_report.json&fake=true
```

Set `TESTEXPLAIN_ENABLE_LOCAL_PATH_API=true` before using that endpoint. It is
disabled by default so a public deployment cannot read caller-supplied server
paths. Use `fake=true` for an offline dry run.

To analyze a bundled report through the API:

```bash
curl -X POST http://127.0.0.1:8000/analyze-bundle \
  -F "bundle=@artifacts/failure-bundle.zip" \
  -F "fake=true"
```

See the [artifact collection guide](docs/guides/collecting-artifacts.md) for
the complete report, trace, HAR, bundling, and troubleshooting workflow.

## Shareable Demo Portal

`render.yaml` deploys the FastAPI portal as a Render free web service. It
supports the full 50 MiB ZIP-file limit, unlike serverless hosts with small
request-body caps. A free Render instance sleeps when idle, so the first request
can take roughly 30 seconds to wake.

### Deploy On Render

1. Push the desired branch and connect `jaiswalkpraveen/testexplain` in Render.
2. Create a web service from this repository's `render.yaml` Blueprint.
3. In Render's Environment settings, set these secrets:

```text
LLM_BASE_URL=https://router.gateframe.ai/v1
LLM_API_KEY=your-demo-provider-key
LLM_MODEL=your-low-cost-demo-model
```

4. Set `TRUSTED_PROXY_IPS` to the immediate Render ingress address or addresses
   observed for the service, separated by commas. Only these peers may supply a
   forwarded client address for the demo quota. Without it, the portal uses the
   direct peer address: safe against spoofing, but colleagues can share a quota
   bucket.
5. Set a provider-side spending limit before enabling `LLM_API_KEY`. Removing
   that variable disables server-funded demo mode immediately.
6. Deploy and open the generated `https://...onrender.com` URL.

Demo mode is limited to 10 requests per client per hour in the single Render
process. It is a feedback-demo guardrail, not authentication or durable billing
protection. The counter resets after a restart; use authentication and a shared
store before treating it as a production quota system.

### Portal Providers

The browser never sends a provider URL. A colleague selects a named provider,
supplies a transient key and model, and the server maps the name to a fixed
trusted endpoint:

| Provider | Key is sent to | Example model |
| --- | --- | --- |
| Demo | server-configured `LLM_BASE_URL` | your configured low-cost model |
| OpenAI | `https://api.openai.com/v1` | `gpt-4.1-mini` |
| Anthropic | Anthropic Messages API | `claude-sonnet-4-5` |
| OpenRouter | `https://openrouter.ai/api/v1` | a model exposed by OpenRouter |

BYOK values are used only for the request and are not stored. Custom endpoints,
LAN model servers, and arbitrary `base_url` values are rejected by the public
portal to prevent server-side request forgery. Use the CLI or local API for
self-hosted endpoints.

### Colleague Feedback Flow

1. Run **Report only**.
2. Run **Report + trace**.
3. Run **Report + trace + HAR**.
4. Run **Missing trace** and confirm the evidence-gap warning is visible.
5. Optionally upload a real failure bundle larger than 4.5 MB and smaller than
   50 MiB to prove the portal is not constrained by a small serverless limit.
6. Optionally select OpenAI, Anthropic, or OpenRouter and enter a personal key
   and model.
7. Record whether the explanation, cited evidence, confidence, and next steps
   were clear and useful.

The four samples are synthetic M2 fixtures. They demonstrate **evidence
ablation**: hold the same failure constant, then observe how report-only,
trace, and HAR context change the available explanation. They do not yet prove
model answer quality; M3 and M4 add classification measurement and evaluation.

## Testing

```bash
make test
```

The automated suite uses `FakeGateway`, so it requires no API key, makes no network calls, and does not spend LLM tokens.

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Data contracts and validation | Pydantic |
| LLM integration | OpenAI-compatible gateway, plus optional direct Anthropic gateway |
| Environment loading | python-dotenv |
| CLI | Typer |
| HTTP API | FastAPI + Uvicorn |
| Tests | pytest |
| Tooling | uv |

## Project layout

```text
src/testexplain/
├── models.py # FailureContext, Evidence, and FailureAnalysis contracts
├── ingestion/
│ ├── input_reader.py # safe report/bundle input
│ └── playwright.py # Playwright JSON → attempts and failure contexts
├── sources/
│ ├── trace.py # Playwright trace → normalized evidence
│ ├── har.py # HAR → fallback network evidence
│ └── common.py # shared source-adapter primitives
├── assembly/
│ └── redact.py # deterministic secret redaction before prompts
├── gateway.py # Gateway Protocol and fake/real implementations
├── core.py                   # prompt, parse, validate, retry, and orchestration pipeline
├── cli.py                    # testexplain analyze
└── api.py                    # FastAPI /analyze endpoint
tests/                        # one test module per source module
docs/milestones/              # per-milestone learning notes
```

## Roadmap

M0 walking skeleton (done) → **M1 structured outputs (done)** → **M2 multi-source context and safe artifact workflow (done)** → M3 categorization → **M4 eval harness** → embeddings/vector DB → RAG → flaky detection → debugging agent → Slack → MCP server → multi-agent → production hardening.

The eval harness (M4) is the centerpiece: rigorous, testable evaluation of a non-deterministic system — the SDET-to-AI-engineer bridge.
