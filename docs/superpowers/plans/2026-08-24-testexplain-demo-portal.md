# testExplain Demo Portal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy a shareable `testExplain` portal on Render with full-size M2 bundle uploads, explicit server-side demo mode, BYOK support, and one-click evidence-ablation samples.

**Architecture:** Keep the existing FastAPI application and M2 analysis pipeline unchanged at its core. Add a small gateway-selection seam for explicit demo/BYOK behavior, serve committed sample assets through a safe static route, and enhance the existing single-page HTML portal. Render runs the ASGI app directly using the existing Uvicorn process command, preserving the 50 MiB server-side upload limit and portability to other Python hosts.

**Tech Stack:** Python 3.11+, FastAPI, Starlette, Pydantic, Uvicorn, pytest, vanilla HTML/CSS/JavaScript, Render Blueprint configuration.

---

## File Map

- Create: `render.yaml` — Render service definition, build command, start command, and non-secret defaults.
- Create: `tests/test_deployment.py` — dependency and Render configuration checks.
- Create: `tests/test_demo_mode.py` — gateway selection tests for fake, BYOK, demo, and missing credentials.
- Create: `src/testexplain/static/samples/checkout-report.json` — packaged report-only sample copied from the M2 fixture.
- Create: `src/testexplain/static/samples/checkout-trace.zip` — deterministic report-plus-trace bundle built by the CLI.
- Create: `src/testexplain/static/samples/checkout-trace-har.zip` — deterministic report-plus-trace-plus-HAR bundle built by the CLI.
- Create: `src/testexplain/static/samples/missing-trace-report.json` — packaged evidence-gap sample copied from the M2 fixture.
- Modify: `requirements.txt` — add the runtime multipart dependency required by `/analyze-bundle`.
- Modify: `src/testexplain/api.py` — add explicit demo request fields, gateway selection, and safe sample serving.
- Modify: `src/testexplain/static/index.html` — add sample actions, demo toggle, evidence labels, and `testExplain` branding.
- Modify: `tests/test_api.py` — preserve and extend endpoint behavior coverage.
- Modify: `README.md` — document Render deployment, demo environment variables, sample workflow, and colleague feedback steps.
- Modify: `docs/guides/collecting-artifacts.md` — link the deployed-portal workflow and explain the public-demo credential risk.
- Modify: `src/testexplain/cli.py`, `src/testexplain/gateway.py`, `docs/milestones/M0.md`, `docs/milestones/M2.md`, `docs/AUDIT-BRIEF.md`, and files under `docs/linkedin/` — replace project-controlled `TestLens` branding with `testExplain`.
- Rename outside the repository after verification: `/Users/praveen/Documents/personal/testlens` to `/Users/praveen/Documents/personal/testexplain`.

## Task 1: Add Render Runtime Configuration

**Files:**
- Create: `render.yaml`
- Modify: `requirements.txt`
- Create: `tests/test_deployment.py`

- [ ] **Step 1: Write failing dependency and configuration tests**

```python
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_requirements_include_multipart_runtime_dependency():
    requirements = (ROOT / "requirements.txt").read_text()
    assert any(line.startswith("python-multipart") for line in requirements.splitlines())


def test_render_config_runs_fastapi_on_render_port():
    config = (ROOT / "render.yaml").read_text()
    assert "PYTHONPATH=src uvicorn testexplain.api:app" in config
    assert "$PORT" in config
    assert "LLM_API_KEY" in config
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run pytest tests/test_deployment.py -v`

Expected: FAIL because `requirements.txt` does not contain `python-multipart` and `render.yaml` does not exist.

- [ ] **Step 3: Add the runtime dependency and Render Blueprint**

Append this line to `requirements.txt`:

```text
python-multipart>=0.0.32
```

Create `render.yaml`:

```yaml
services:
  - type: web
    name: testexplain
    runtime: python
    buildCommand: pip install -r requirements.txt
    startCommand: PYTHONPATH=src uvicorn testexplain.api:app --host 0.0.0.0 --port $PORT
    plan: free
    envVars:
      - key: LLM_BASE_URL
        sync: false
      - key: LLM_API_KEY
        sync: false
      - key: LLM_MODEL
        value: gateframe/gemini-2.5-flash
```

`sync: false` means Render prompts for the secret value rather than storing a
credential in the repository. The model default is replaceable in the Render
dashboard with a low-cost model.

- [ ] **Step 4: Run the focused tests and verify they pass**

Run: `uv run pytest tests/test_deployment.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the runtime configuration**

```bash
git add requirements.txt render.yaml tests/test_deployment.py
git commit -m "feat: configure render deployment"
```

## Task 2: Add Explicit Demo Gateway Selection

**Files:**
- Create: `tests/test_demo_mode.py`
- Modify: `src/testexplain/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing tests for gateway precedence**

Add tests using `monkeypatch` and the existing request client. The assertions
must prove that fake mode remains offline, BYOK wins over demo configuration,
demo mode uses server environment variables, and missing demo configuration is
an actionable client error.

```python
def test_post_analyze_demo_uses_server_environment(monkeypatch, client):
    monkeypatch.setenv("LLM_API_KEY", "server-key")
    monkeypatch.setenv("LLM_BASE_URL", "https://server.example/v1")
    monkeypatch.setenv("LLM_MODEL", "server-model")
    response = client.post("/analyze", json={"report": REPORT, "demo": True})
    assert response.status_code == 200


def test_post_analyze_demo_without_server_key_is_clear_error(monkeypatch, client):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    response = client.post("/analyze", json={"report": REPORT, "demo": True})
    assert response.status_code == 422
    assert "demo" in response.json()["detail"].lower()
    assert "LLM_API_KEY" in response.json()["detail"]


def test_post_analyze_byok_takes_precedence_over_demo(monkeypatch, client):
    monkeypatch.setenv("LLM_API_KEY", "server-key")
    response = client.post(
        "/analyze",
        json={
            "report": REPORT,
            "demo": True,
            "api_key": "request-key",
            "base_url": "https://request.example/v1",
            "model": "request-model",
        },
    )
    assert response.status_code == 200
```

Use the existing `fake` request field in these tests or patch the gateway
constructor so no real provider is contacted. Add an equivalent multipart
request test for `/analyze-bundle`.

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run pytest tests/test_demo_mode.py tests/test_api.py -v`

Expected: FAIL because `AnalyzeRequest` has no `demo` field and the multipart
endpoint has no demo fallback.

- [ ] **Step 3: Implement one shared gateway selector**

Add a private helper in `api.py` with this signature:

```python
def _gateway_for_request(
    *,
    api_key: str | None,
    base_url: str | None,
    model: str | None,
    fake: bool,
    demo: bool,
):
```

Its exact decision order is:

```python
if fake:
    return FakeGateway()
if api_key:
    return OpenAICompatibleGateway(
        api_key=api_key,
        base_url=base_url,
        model=model,
    )
if demo:
    try:
        return OpenAICompatibleGateway()
    except KeyError as exc:
        raise HTTPException(
            status_code=422,
            detail=(
                "Demo mode is not configured. Set LLM_API_KEY, LLM_BASE_URL, "
                "and LLM_MODEL on the server, or provide your own api_key."
            ),
        ) from exc
raise HTTPException(status_code=422, detail="api_key is required when fake is false.")
```

Add `demo: bool = False` to `AnalyzeRequest` and `demo: bool = Form(default=False)`
to `analyze_bundle`. Replace duplicated gateway construction with the helper.
Do not alter the existing `fake=true` behavior or the cleanup `finally` blocks.

- [ ] **Step 4: Run tests and verify the implementation**

Run: `uv run pytest tests/test_demo_mode.py tests/test_api.py -v`

Expected: PASS, including all pre-existing API tests.

- [ ] **Step 5: Commit demo mode**

```bash
git add src/testexplain/api.py tests/test_demo_mode.py tests/test_api.py
git commit -m "feat: add explicit demo gateway mode"
```

## Task 3: Package and Serve M2 Sample Assets

**Files:**
- Create: `src/testexplain/static/samples/checkout-report.json`
- Create: `src/testexplain/static/samples/checkout-trace.zip`
- Create: `src/testexplain/static/samples/checkout-trace-har.zip`
- Create: `src/testexplain/static/samples/missing-trace-report.json`
- Modify: `src/testexplain/api.py`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing sample-serving tests**

```python
def test_sample_report_is_served(client):
    response = client.get("/samples/checkout-report.json")
    assert response.status_code == 200
    assert response.json()["suites"]


def test_sample_zip_is_served_as_download(client):
    response = client.get("/samples/checkout-trace-har.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"


def test_sample_path_cannot_escape_sample_directory(client):
    response = client.get("/samples/../api.py")
    assert response.status_code in {404, 400}
```

- [ ] **Step 2: Run the focused tests and verify they fail**

Run: `uv run pytest tests/test_api.py -k sample -v`

Expected: FAIL because sample files and the sample route do not exist.

- [ ] **Step 3: Build deterministic sample assets from M2 fixtures**

Copy the two JSON fixtures into the packaged sample directory. The source
checkout report references both the trace and HAR. Build the trace-only ZIP
from a temporary JSON copy with the HAR attachment removed, then build the
trace-plus-HAR ZIP from the original report. Both ZIPs must be produced by the
actual CLI, not by an ad-hoc archive script:

```bash
mkdir -p src/testexplain/static/samples /tmp/testexplain-checkout-fixture
cp tests/fixtures/m2/checkout-report.json src/testexplain/static/samples/checkout-report.json
cp tests/fixtures/m2/missing-trace-report.json src/testexplain/static/samples/missing-trace-report.json
cp tests/fixtures/m2/checkout-report.json /tmp/testexplain-checkout-fixture/report.json
cp tests/fixtures/m2/checkout.trace.zip /tmp/testexplain-checkout-fixture/checkout.trace.zip
uv run python -c 'import json; from pathlib import Path; p=Path("/tmp/testexplain-checkout-fixture/report.json"); report=json.loads(p.read_text()); result=report["suites"][0]["specs"][0]["tests"][0]["results"][0]; result["attachments"]=[a for a in result["attachments"] if a.get("path") != "checkout.har"]; p.write_text(json.dumps(report))'
uv run testexplain bundle /tmp/testexplain-checkout-fixture/report.json --output src/testexplain/static/samples/checkout-trace.zip
uv run testexplain bundle tests/fixtures/m2/checkout-report.json --output src/testexplain/static/samples/checkout-trace-har.zip
rm -rf /tmp/testexplain-checkout-fixture
```

The temporary report edit must preserve valid JSON and leave its trace
attachment unchanged. Verify each archive with `unzip -l` and confirm the
trace-only archive contains no HAR while the second contains both intended
attachments. Do not include credentials or arbitrary generated files.

- [ ] **Step 4: Add a safe fixed-name sample route**

In `api.py`, define a fixed mapping rather than accepting arbitrary filesystem
paths:

```python
SAMPLE_FILES = {
    "checkout-report.json": "checkout-report.json",
    "checkout-trace.zip": "checkout-trace.zip",
    "checkout-trace-har.zip": "checkout-trace-har.zip",
    "missing-trace-report.json": "missing-trace-report.json",
}
```

Add `GET /samples/{sample_name}`. Look up `sample_name` in `SAMPLE_FILES`,
raise 404 for anything else, and return a `FileResponse` from the static
samples directory. This prevents traversal and prevents the route from
exposing unrelated static or source files.

- [ ] **Step 5: Run focused sample tests and inspect archives**

Run: `uv run pytest tests/test_api.py -k sample -v` and
`unzip -l src/testexplain/static/samples/checkout-trace-har.zip`.

Expected: sample tests PASS and the archive contains only the intended report,
trace, and HAR-related members.

- [ ] **Step 6: Commit packaged samples**

```bash
git add src/testexplain/static/samples src/testexplain/api.py tests/test_api.py
git commit -m "feat: add packaged m2 demo samples"
```

## Task 4: Add the Evidence-Ablation Portal Workflow

**Files:**
- Modify: `src/testexplain/static/index.html`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Add failing HTML contract assertions**

Extend the existing index test with assertions for every sample and the
explicit demo field:

```python
html = client.get("/").text
for label in (
    "Report only",
    "Report + trace",
    "Report + trace + HAR",
    "Missing trace",
):
    assert label in html
assert "demo" in html
assert "/samples/checkout-report.json" in html
assert "/samples/checkout-trace.zip" in html
assert "/samples/checkout-trace-har.zip" in html
assert "/samples/missing-trace-report.json" in html
```

- [ ] **Step 2: Run the focused UI contract test and verify it fails**

Run: `uv run pytest tests/test_api.py -k index -v`

Expected: FAIL because the current page has no sample actions and no demo
request field.

- [ ] **Step 3: Add sample controls and explicit demo submission**

Add a clearly labelled “M2 evidence comparison” card above the manual form.
Each button must identify its evidence sources and call a single JavaScript
function such as:

```javascript
async function runSample(path, kind, label) {
  setLoading(`Running ${label}...`);
  if (kind === "report") {
    const report = await fetch(path).then((response) => response.text());
    res = await fetch("/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ report, demo: true }),
    });
  } else {
    const blob = await fetch(path).then((response) => response.blob());
    const formData = new FormData();
    formData.append("bundle", blob, path.split("/").pop());
    formData.append("demo", "true");
    res = await fetch("/analyze-bundle", { method: "POST", body: formData });
  }
  await renderResponse(res);
}
```

Adapt this to the existing submission/rendering functions rather than
duplicating response parsing. The missing-trace sample uses JSON and the
existing report-only endpoint. Show the active label while waiting and retain
the existing error handling. Add a demo-mode checkbox or selector that is
checked for samples and can be unchecked when a colleague enters BYOK values.
The manual JSON and ZIP paths must continue forwarding their current fields.

- [ ] **Step 4: Rename visible UI branding**

Change the HTML title, heading, subtitle, and demo note to `testExplain`.
The page should explain in plain language that sample buttons compare what the
system can infer with report-only, trace, and HAR evidence.

- [ ] **Step 5: Run UI contract and full API tests**

Run: `uv run pytest tests/test_api.py -v`

Expected: PASS with the existing manual upload, invalid input, size-limit,
cleanup, and HTML tests still passing.

- [ ] **Step 6: Commit the portal workflow**

```bash
git add src/testexplain/static/index.html tests/test_api.py
git commit -m "feat: add m2 evidence demo controls"
```

## Task 5: Complete Project-Controlled testExplain Branding

**Files:**
- Modify: `src/testexplain/cli.py`
- Modify: `src/testexplain/gateway.py`
- Modify: `src/testexplain/api.py`
- Modify: `src/testexplain/static/index.html`
- Modify: `README.md`
- Modify: `docs/guides/collecting-artifacts.md`
- Modify: `docs/milestones/M0.md`
- Modify: `docs/milestones/M2.md`
- Modify: `docs/AUDIT-BRIEF.md`
- Modify: `docs/linkedin/*.md`
- Modify: `AGENTS.md`

- [ ] **Step 1: Capture the current active-name inventory**

Run:

```bash
```

Review every result and classify it as project-controlled text, Git metadata,
generated dependency/cache content, or an intentional historical identifier.
This task changes project-controlled text only.

- [ ] **Step 2: Replace project-controlled branding**

Use `testExplain` for visible product text and `testexplain` for Python module,
CLI, filesystem, and technical identifiers. Update FastAPI title/description,
temporary-file prefixes, CLI help text, gateway documentation, HTML branding,
README, milestone notes, audit notes, LinkedIn drafts, artifact guide, and
local-only project instructions. Do not rewrite Git history, `.venv`, Python
bytecode, or `.superpowers` generated state.

- [ ] **Step 3: Verify no unintended active occurrences remain**

Run the inventory command again. Expected: no project-controlled `TestLens` or
`testlens` occurrences, apart from this migration plan's historical file-path
references and any explicitly documented cache/generated files.

- [ ] **Step 4: Run the full suite and commit the rename**

Run: `make test`

Expected: all tests pass.

```bash
git add src/testexplain/cli.py src/testexplain/gateway.py src/testexplain/api.py \
  src/testexplain/static/index.html README.md \
  docs/guides/collecting-artifacts.md docs/milestones/M0.md \
  docs/milestones/M2.md docs/AUDIT-BRIEF.md docs/linkedin \
  AGENTS.md
git commit -m "docs: standardize testexplain branding"
```

## Task 6: Document Deployment, Feedback, and Local Directory Migration

**Files:**
- Modify: `README.md`
- Modify: `docs/guides/collecting-artifacts.md`
- Rename outside repository: `/Users/praveen/Documents/personal/testlens` to `/Users/praveen/Documents/personal/testexplain`

- [ ] **Step 1: Add exact Render setup instructions**

Document:

1. Connect `jaiswalkpraveen/testexplain` to Render.
2. Select the `render.yaml` Blueprint or create the web service from the repo.
3. Set `LLM_API_KEY`, `LLM_BASE_URL`, and `LLM_MODEL` in Render's Environment
   settings.
4. Use a low-cost model and configure a provider spending limit.
5. Deploy and wait for the free service wake-up.
6. Open the generated HTTPS URL.

Explain that the server accepts bundles up to 50 MiB, but free-tier sleeping
can make the first request take about 30 seconds.

- [ ] **Step 2: Add the colleague feedback checklist**

Include this exact verification sequence:

```text
1. Run Report only.
2. Run Report + trace.
3. Run Report + trace + HAR.
4. Run Missing trace and check the evidence-gap warning.
5. Upload a real bundle larger than 4.5 MB and smaller than 50 MiB.
6. Try BYOK and confirm request credentials override demo defaults.
7. Record whether the explanation, evidence, confidence, and next steps are clear.
```

State that the demo is unauthenticated, requests can spend the configured
provider budget, and removing `LLM_API_KEY` disables server-side demo mode.

- [ ] **Step 3: Verify documentation links and rendered commands**

Run: `git diff --check` and search the README for `render.yaml`,
`analyze-bundle`, `LLM_API_KEY`, `50 MiB`, and the four sample labels.

- [ ] **Step 4: Commit deployment documentation**

```bash
git add README.md docs/guides/collecting-artifacts.md
git commit -m "docs: document render demo workflow"
```

- [ ] **Step 5: Run final local verification before moving the directory**

Run:

```bash
make test
```

Expected: the complete suite passes, the diff check is clean, and only
intentional worktree files are present.

- [ ] **Step 6: Rename the local directory after all repository work is done**

From the parent directory, run:

```bash
mv /Users/praveen/Documents/personal/testlens /Users/praveen/Documents/personal/testexplain
```

Open a new shell in `/Users/praveen/Documents/personal/testexplain`, verify
`git status --short`, and confirm the personal Git conditional include still
applies because the directory remains under `~/Documents/personal/`. Do not
rename the directory during an active command session.

## Task 7: Deploy and Verify the Shareable Portal

**Files:**
- No repository changes expected unless deployment verification finds a
  concrete defect.

- [ ] **Step 1: Verify GitHub account before deployment integration**

Run: `gh auth status`.

Expected: `jaiswalkpraveen` is the active account. If it is not active, run:
`gh auth switch --user jaiswalkpraveen`.

- [ ] **Step 2: Push the verified branch**

Inspect `git status --short`, `git diff`, and `git log --oneline -10`, then push
only the intended commits with:

```bash
```

- [ ] **Step 3: Create the Render service**

In Render, select the `testexplain` repository and apply `render.yaml`. Set
the three `LLM_*` variables. Do not paste secrets into source files, build
logs, issue comments, or colleague messages.

- [ ] **Step 4: Verify health and sample flows in the browser**

Open the Render URL and execute all four sample actions. Confirm that:

- Report-only returns an analysis without trace/HAR evidence.
- Report-plus-trace exposes action/trace evidence.
- Report-plus-trace-plus-HAR exposes network/HAR evidence.
- Missing-trace visibly reports the evidence gap.

- [ ] **Step 5: Verify real upload and BYOK behavior**

Upload a real bundle between 4.5 MB and 50 MiB to demonstrate that the portal
is not constrained by Vercel's serverless request cap. Verify an invalid ZIP,
an oversized upload, and a BYOK request. Confirm temporary files are cleaned
through application logs or the existing automated behavior; do not expose
uploaded reports containing credentials to colleagues.

- [ ] **Step 6: Share the URL with the feedback checklist**

Send colleagues the Render URL, the four sample actions to try, the expected
first-request wake-up delay, and the feedback questions from Task 6. Record
feedback separately from implementation changes so product observations can
inform M3 without changing the M2 pipeline during the demo.

## Final Verification Checklist

- [ ] `python-multipart` is in `requirements.txt`.
- [ ] Render starts `testexplain.api:app` with `$PORT`.
- [ ] No credentials are committed.
- [ ] `demo=true` is explicit and BYOK takes precedence.
- [ ] Fake gateway tests remain offline.
- [ ] All four M2 samples deploy with the package.
- [ ] Sample route uses a fixed allowlist.
- [ ] 50 MiB server-side upload enforcement remains active.
- [ ] Existing upload cleanup and ZIP safety tests pass.
- [ ] `testExplain` is used in project-controlled visible text.
- [ ] `make test` passes.
- [ ] `git diff --check` is clean.
- [ ] The Render URL is tested with the colleague checklist.
