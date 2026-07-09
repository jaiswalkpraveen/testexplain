# TestExplain

> Most test reports tell you **what** failed. TestExplain tells you **why** — and (soon) proves its answers are right.

TestExplain is an AI-native test-failure triage platform. It ingests Playwright reports and uses an LLM to explain, in plain English, why each test failed and what to check first. It's built milestone-by-milestone as a learning-grade, framework-free AI engineering project.

**Status:** Milestone 0 (Walking Skeleton) complete — parse a Playwright report → explain each failure via an LLM → CLI + HTTP API.

## Architecture (M0)

```
report.json ──▶ ingestion ──▶ FailureContext ──▶ core ──▶ Gateway (LLM) ──▶ FailureAnalysis
                (parse)         (normalized)     (prompt)   (swappable)        (result)
                                                                │
                                              ┌─────────────────┴─────────────────┐
                                              ▼                                   ▼
                                         CLI (testexplain analyze)           HTTP API (/analyze)
```

**Key design decision — the gateway seam:** all LLM calls go through a `Gateway` shape (Protocol). Tests and dry runs use `FakeGateway` (no network, no API key); production uses `AnthropicGateway` (real Claude). The core code never mentions a concrete provider, so swapping models later is trivial.

## Quick start

```bash
# Install deps
uv sync

# Try it with the fake gateway (no API key needed)
make run-cli-fake
# or:
uv run testexplain analyze tests/fixtures/sample_report.json --fake

# Real analysis (needs ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
uv run testexplain analyze tests/fixtures/sample_report.json

# Run the HTTP API, then open http://127.0.0.1:8000/docs
make run-api
```

## Testing

```bash
make test        # run all tests (uses FakeGateway — no API key, no network)
```

## Tech stack

| Layer | Choice |
|---|---|
| Language | Python 3.11+ |
| Models | Pydantic |
| LLM | Anthropic Claude (behind a swappable gateway) |
| CLI | Typer |
| HTTP API | FastAPI + Uvicorn |
| Tests | pytest |
| Tooling | uv |

## Project layout

```
src/testexplain/
├── models.py            # FailureContext (LLM input), FailureAnalysis (LLM output)
├── ingestion/
│   └── playwright.py    # parse_report() — Playwright JSON → FailureContext[]
├── gateway.py           # Gateway Protocol, FakeGateway, AnthropicGateway
├── core.py              # build_prompt(), analyze_report() — the pipeline
├── cli.py               # testexplain analyze
└── api.py               # FastAPI /analyze endpoint
tests/                   # one test module per source module
docs/milestones/         # per-milestone learning notes
```

## Roadmap

M0 (done) → structured outputs → multi-source context → categorization → **eval harness** → embeddings/vector DB → RAG → flaky detection → debugging agent → Slack → MCP server → multi-agent → production hardening.

The eval harness (M4) is the centerpiece: rigorous, testable evaluation of a non-deterministic system — the SDET-to-AI-engineer bridge.