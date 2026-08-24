# testExplain Demo Portal Design

**Date:** 2026-08-24  
**Status:** Approved design

## Goal

Deploy a shareable `testExplain` web portal on Render so colleagues can
verify the M2 evidence pipeline through a browser. The portal must support
the existing M2 bundle workflow, offer one-click sample analyses that show
evidence ablation, allow a server-configured demo model as well as bring-your-
own-key (BYOK) analysis, and retain a path to other Python web hosts.

## Why Render

The application is a normal FastAPI ASGI application and already has a
`Procfile` with a Uvicorn start command. Render can run that process directly,
so the deployment does not need a serverless adapter.

Vercel is not the target because its approximately 4.5 MB request-body limit
is smaller than realistic Playwright failure bundles. Typical artifact sizes
vary with test count, DOM size, network traffic, screenshots, and response
bodies:

| Artifact | Typical size |
| --- | ---: |
| Playwright JSON report | 0.5–5 MB |
| Single failure trace ZIP | 0.5–5 MB |
| DOM-heavy trace ZIP | 10–40 MB |
| HAR with response bodies | 1–20 MB |
| Complete failure bundle | 2–10 MB commonly; up to 50 MB |

The current application limit of 50 MiB remains meaningful on Render. Render's
free service may sleep after inactivity and take roughly 30 seconds to wake,
which is acceptable for colleague feedback and sample demonstrations.

## Scope

### Deployment configuration

- Add Render configuration that installs `requirements.txt` and starts
  `testexplain.api:app` with Uvicorn on Render's `$PORT`.
- Add `python-multipart` to `requirements.txt`; it is already declared in
  `pyproject.toml` and is required for FastAPI's multipart form parsing.
- Keep the existing `Procfile` so the application remains portable to Railway
  and other Procfile-compatible hosts.
- Configure `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` as Render-managed
  environment variables. No credential is committed to the repository.

### Explicit demo mode and BYOK

The existing BYOK request fields remain supported. Both `POST /analyze` and
`POST /analyze-bundle` gain an explicit `demo` request field.

Request behavior:

1. If `fake=true`, use `FakeGateway` exactly as today for offline tests.
2. Otherwise, if request-level `api_key` is supplied, use the request's
   `api_key`, `base_url`, and `model` values. BYOK takes precedence over demo
   mode.
3. Otherwise, if `demo=true`, construct the gateway from the server's
   `LLM_*` environment variables.
4. Otherwise, return the existing missing-API-key validation error.
5. If demo mode is requested but the required server configuration is absent,
   return a clear client-facing error rather than silently falling back to a
   fake response.

The demo flag is explicit so public demo behavior is visible, testable, and
auditable. Because anyone with the public URL can invoke the demo key, the
deployment instructions will recommend a low-cost model and a provider-side
spend limit. Removing the Render `LLM_API_KEY` environment variable disables
the real demo mode.

### M2 sample analyses

The portal will make the M2 ablation experiment visible without requiring a
colleague to generate Playwright artifacts first. Deployable sample assets
will represent these cases:

- Report only.
- Report plus trace.
- Report plus trace plus HAR.
- A missing-trace case that demonstrates an evidence-gap warning.

ZIP samples will be produced using the real `testexplain bundle` CLI and the
existing M2 fixtures. This ensures the browser demo exercises the same bundle
format and path-rewriting behavior as the CLI workflow.

Sample assets are committed under the packaged static directory
(`src/testexplain/static/samples/`) so they deploy with the application and
need no runtime generation. Bundles are rebuilt only when the underlying M2
fixtures change.

The UI will expose one-click actions labelled by evidence level. The report-
only action posts JSON to `POST /analyze`; ZIP actions post multipart data to
`POST /analyze-bundle`. Each sample request uses `demo=true`. The UI will show
which sample is running and which evidence sources are available, so a
colleague can compare the same synthetic failure with progressively richer
context.

### UI and branding

- Rename visible product branding to `testExplain`.
- Add an explicit demo-mode control and retain the existing BYOK fields.
- Retain manual JSON file selection, pasted JSON, and bundle ZIP upload.
- Show actionable errors for server wake-up, missing demo configuration,
  invalid bundles, and oversized uploads.
- Keep the existing result cards, while making the sample workflow prominent
  enough for a first-time colleague to discover it.

The repository and Python package are already named `testexplain`, and the
GitHub remote already points to `jaiswalkpraveen/testexplain`. The remaining
rename covers active code, user-facing documentation, historical project
notes, LinkedIn drafts, temporary-file prefixes, and the local directory name
`testlens` to `testexplain`. The local directory move will be performed only
after repository changes are verified because moving the active working
directory during development can interrupt the session.

## Architecture and Data Flow

The production path remains the existing framework-free pipeline:

```text
Colleague browser
    -> Render FastAPI app
    -> sample JSON or uploaded ZIP
    -> safe input loading and temporary-file cleanup
    -> Playwright report / trace / HAR parsing
    -> redaction and evidence assembly
    -> explicit demo gateway or BYOK gateway
    -> validated FailureAnalysis JSON
    -> result cards in the browser
```

The hosting layer does not alter evidence parsing, ranking, redaction,
prompt composition, structured-output validation, or gateway seams. The
server-configured demo key is used only to construct the existing
`OpenAICompatibleGateway`; `FakeGateway` remains available for tests and
local dry runs.

Normal Uvicorn process execution is preferred over a Vercel serverless shim.
This preserves the 50 MiB application upload limit and means a future move to
Railway needs the existing `Procfile`, while a move to a container host needs
only equivalent process configuration.

## Error Handling and Security

- Credentials are supplied through Render environment variables or transient
  BYOK form fields and are never stored in the repository.
- Existing ZIP traversal, duplicate-name, decompression, extracted-size, and
  attachment-containment protections remain enabled.
- Existing 50 MiB upload enforcement remains enabled. The UI checks the file
  size early, but server-side enforcement remains authoritative.
- Temporary uploaded files are deleted on success, validation failure, gateway
  failure, and upload-close failure.
- A missing trace or HAR is reported as an evidence gap; it does not create a
  fabricated explanation.
- Public demo mode is an intentional unauthenticated demonstration surface.
  The deployment guide must document the spend risk, low-cost model choice,
  provider budget cap, and environment-variable kill switch.
- This task does not add user accounts, persistent result storage, rate
  limiting, or object storage. Those are separate production-hardening work.

## Testing Strategy

Implementation follows the repository's TDD convention: write failing tests,
implement the smallest change, then run the focused test and the full suite.

Tests will cover:

- Demo gateway construction when server environment variables are configured.
- Missing demo configuration and the resulting clear error.
- BYOK precedence over demo configuration.
- Preservation of `fake=true` behavior.
- Sample asset serving and safe sample selection.
- UI presence of the four sample actions (report only, report plus trace,
  report plus trace plus HAR, missing trace) and their endpoint and demo-mode
  wiring.
- Bundle upload behavior, including the existing size and cleanup checks.
- Render dependency installation, including `python-multipart`.

Verification before deployment:

```bash
make test
git diff --check
```

Deployment verification:

1. Open the Render URL and wait through the first free-tier wake-up if needed.
2. Run report-only, report-plus-trace, and report-plus-trace-plus-HAR samples.
3. Run the missing-trace sample and confirm the warning is visible.
4. Upload a real bundle larger than 4.5 MB but below 50 MiB.
5. Verify BYOK mode and that it overrides demo defaults.
6. Verify invalid ZIPs and oversized uploads are rejected.
7. Share the URL with colleagues using a feedback checklist covering clarity,
   evidence usefulness, confidence, and next-step quality.

## Out Of Scope

- Vercel deployment or a direct-to-object-storage workaround for its request
  limit.
- Authentication, user accounts, result history, databases, and persistent
  uploads.
- Rate limiting or abuse prevention beyond provider spend controls.
- Changes to M2 parsing, redaction, evidence ranking, or prompt semantics.
- M3 categorization, severity, confidence calibration, or evaluation metrics.
