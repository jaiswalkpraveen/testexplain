# TestExplain

> Most test reports tell you **what** failed. TestExplain tells you **why** — and (soon) proves its answers are right.

TestExplain is an AI-native test-failure triage platform. It ingests Playwright reports and uses an LLM to explain, in plain English, why each test failed and what to check first. It's built milestone-by-milestone as a learning-grade, framework-free AI engineering project.

**Status:** Milestone 0 established the walking skeleton. Milestone 1 adds structured, validated LLM output with corrective retries.

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

The prompt asks for JSON, but TestExplain does not trust the model to comply. Every reply is parsed and validated at the system boundary. Invalid JSON, invented categories, missing fields, or out-of-range confidence values trigger a corrective retry.

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

For a no-auth self-hosted endpoint, keep `LLM_API_KEY=` present but empty. TestExplain supplies an internal placeholder because the OpenAI client requires a non-empty value even when the server ignores authentication.

Run a real analysis:

```bash
uv run testexplain analyze tests/fixtures/sample_report.json
```

### 4. Run the HTTP API

```bash
make run-api
```

Then open `http://127.0.0.1:8000/docs`. The endpoint is:

```text
GET /analyze?report_path=tests/fixtures/sample_report.json&fake=true
```

Use `fake=true` for an offline dry run or `fake=false` for the configured real endpoint.

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
├── models.py                 # FailureContext and structured FailureAnalysis contracts
├── ingestion/
│   └── playwright.py         # Playwright JSON → list[FailureContext]
├── gateway.py                # Gateway Protocol and fake/real implementations
├── core.py                   # prompt, parse, validate, retry, and orchestration pipeline
├── cli.py                    # testexplain analyze
└── api.py                    # FastAPI /analyze endpoint
tests/                        # one test module per source module
docs/milestones/              # per-milestone learning notes
```

## Roadmap

M0 walking skeleton (done) → **M1 structured outputs (done)** → multi-source context → categorization → **eval harness** → embeddings/vector DB → RAG → flaky detection → debugging agent → Slack → MCP server → multi-agent → production hardening.

The eval harness (M4) is the centerpiece: rigorous, testable evaluation of a non-deterministic system — the SDET-to-AI-engineer bridge.
