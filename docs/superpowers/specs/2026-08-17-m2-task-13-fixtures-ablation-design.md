# M2 Task 13 - Fixtures, ablation evaluation, and user documentation

**Date:** 2026-08-17  
**Milestone:** M2 (Multi-Source Context), Task 13 of 13  
**Status:** approved

## Goal

Close M2 with a small, synthetic but production-shaped artifact set that proves
the evidence pipeline adds useful context incrementally. Document how a
Playwright user creates, locates, validates, bundles, and uploads the artifacts.
Complete the M2 learning notes so the implementation and its Python/AI concepts
are understandable from first principles.

## Scope

In scope:

- A sanitized, hand-authored checkout failure fixture group under
  `tests/fixtures/m2/`.
- A compact edge fixture for a report that references an unavailable artifact.
- Ablation tests using `FakeGateway` and the real `analyze_input()` pipeline.
- A user guide covering Playwright reporter configuration, traces, HAR files,
  attachment paths, bundling, validation, upload, and troubleshooting.
- README updates describing the completed M2 pipeline and artifact workflow.
- M2 learning notes for Task 13, including a pipeline mental model and the
  context-engineering/ablation concepts used here.
- Full test and configured-tool verification.

Out of scope:

- Real user or production artifacts.
- New ingestion, adapter, assembly, gateway, CLI, or API behavior.
- A scoring or judge model for answer quality; that belongs to M4.
- Benchmark claims about LLM accuracy or latency.
- A large matrix of failure scenarios duplicating the source-adapter tests.

## Fixture design

The primary fixture group will model one failed Chromium checkout attempt:

```text
tests/fixtures/m2/
  checkout-report.json
  checkout.trace.zip
  checkout.har
  missing-trace-report.json
```

`checkout-report.json` uses the native Playwright JSON report shape. It contains
one unexpected failed attempt with a timeout, report stdout/stderr, and
attachment records pointing to `checkout.trace.zip` and `checkout.har`.
Filenames and attachment paths are relative to the fixture directory, matching
how a report produced by a Playwright run normally resolves local artifacts.

The report will establish the symptom: the checkout page waited for a result
and timed out. The trace will establish the timeline: the test navigated to the
checkout page, submitted the order, and received a `503 Service Unavailable`
response from fictional `https://shop.test/api/checkout`. The HAR will contain
the matching request and response as independent network confirmation. All
domains, IDs, headers, bodies, selectors, and timestamps are fictional and
must contain no credentials or user data.

`missing-trace-report.json` will be a small valid report whose failed attempt
references an absent trace. It demonstrates the fail-soft artifact warning
path without making the primary ablation test depend on an error case.

Fixtures should be minimal and deterministic. ZIP member order, JSON encoding,
and timestamps should be stable where the test constructs or compares them.
The fixture itself should be readable enough for a learner to connect the
report symptom with the trace/HAR cause.

## Ablation evaluation

`tests/test_ablation.py` will execute the same report through
`analyze_input(report_path, gateway)` under three controlled conditions. The
test will use temporary copies or a temporary fixture directory so each run
controls which referenced artifacts exist:

| Run | Available artifacts | Evidence expected in captured prompt |
|---|---|---|
| Report-only | neither trace nor HAR | timeout and report output; no trace/HAR evidence |
| Report + trace | trace only | report facts, trace action, and trace-attributed `503` |
| Report + trace + HAR | trace and HAR | preceding facts plus HAR-attributed network confirmation |

Each run will use a `FakeGateway` containing the same valid JSON response. The
assertions inspect `gateway.calls[0]`, which is the prompt sent to the model;
they do not assert on model-generated prose. The tests will verify:

- all three prompts retain the report-level timeout;
- the report-only prompt has no `[trace]` or `[har]` evidence lines;
- the trace prompt contains trace provenance and the checkout `503`;
- the combined prompt contains both trace and HAR provenance for the matching
  network event;
- the combined prompt contains enough timestamp/request detail to connect the
  failed network call with the later timeout;
- missing artifacts produce a visible warning while analysis still returns a
  result.

Assertions will target stable semantic markers, not the complete prompt string
or exact ordering of unrelated evidence. This keeps the test focused on the
context boundary while allowing harmless prompt wording improvements.

The test is an ablation study in the narrow engineering sense: hold the input
failure constant, remove one evidence source at a time, and observe which facts
remain available to the model. It demonstrates prompt-input capability, not
causal proof that an LLM will always produce a better answer.

## User documentation

Create `docs/guides/collecting-artifacts.md` as the operational guide. It will
show an exact Playwright configuration using the JSON reporter and trace/HAR
options, then explain the resulting directory layout and report attachment
links. It will distinguish:

- the native Playwright report JSON required by TestExplain;
- trace ZIP files produced by `trace: 'retain-on-failure'` or an equivalent
  tracing setup;
- HAR files produced with Playwright context recording;
- relative attachment paths and the directory from which they resolve;
- `testexplain bundle REPORT --output BUNDLE.zip` packaging;
- offline validation with the CLI fake gateway and HTTP bundle upload with
  `fake=true`.

The guide will include troubleshooting for missing attachments, unsupported or
malformed reports, invalid ZIPs, oversized uploads, and the intentional
redaction of secrets before prompt composition. It will state that users must
review artifacts for credentials and personal data before sharing them, even
though TestExplain applies deterministic redaction.

README changes will update stale M2 progress/test-count text, describe Tasks
9-13 as complete, link to the collecting-artifacts guide, and show the report
versus bundle entry points without duplicating the full guide.

## Learning notes

Append Task 13 notes to `docs/milestones/M2.md` with:

- the end-to-end caller flow from report/bundle input through attachment
  resolution, adapters, normalized evidence, assembly, prompt, and gateway;
- why a synthetic production-shaped fixture is safer and more deterministic
  than committing a real report;
- ablation as a simple way to test whether an evidence source changes model
  context;
- the distinction between testing prompt evidence and evaluating answer
  quality;
- Python concepts used in the task, including filesystem fixtures, ZIP members,
  temporary directories, and assertions over captured calls;
- a concise M2 pipeline diagram and the AI terms worth retaining: provenance,
  normalization, context engineering, token budget, redaction, and ablation.

## Verification

Run the focused ablation tests first, then `make test`. Inspect available
project configuration for lint/typecheck commands and run each configured
command. If a command is not configured or cannot run in the environment,
record that fact rather than introducing a new tool or dependency.

Before completion, verify:

- no fixture contains real secrets or user data;
- `git diff --check` is clean;
- all tests pass, apart from any already-known warning;
- only intended Task 13 files are included in the implementation commit.
