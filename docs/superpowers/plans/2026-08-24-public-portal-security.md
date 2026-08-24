# Public Portal Security Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Protect the shared `testExplain` portal while allowing direct OpenAI, Anthropic, and OpenRouter BYOK analysis.

**Architecture:** Replace caller-controlled provider URLs with a small provider allowlist in the API gateway selector. Keep demo mode server-configured but guard it with an in-memory per-IP fixed-window quota. Disable the development-only server-path endpoint unless explicitly enabled. Preserve `FakeGateway` and existing upload safety.

**Tech Stack:** FastAPI, Pydantic, OpenAI SDK, Anthropic SDK, pytest, vanilla JavaScript.

---

### Task 1: Provider-Allowlisted BYOK

**Files:**
- Modify: `src/testexplain/api.py`
- Modify: `src/testexplain/gateway.py`
- Modify: `tests/test_demo_mode.py`

- [ ] **Step 1: Write failing provider tests**

Cover JSON and multipart requests selecting `openai`, `anthropic`, and
`openrouter`; reject unknown/missing providers, any `base_url` field, blank
keys/models, and prove no gateway is constructed on rejection.

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_demo_mode.py -k provider -v`

Expected: FAIL because the API currently accepts `base_url` and has no
provider field.

- [ ] **Step 3: Implement the fixed provider map**

Use fixed OpenAI and OpenRouter URLs inside `_gateway_for_request`. Extend
`AnthropicGateway` with an explicit `api_key` parameter; it falls back to
`ANTHROPIC_API_KEY` only when called by local/server code. Do not accept any
caller URL.

- [ ] **Step 4: Verify focused and full tests**

Run: `uv run pytest tests/test_demo_mode.py tests/test_gateway.py -v` then
`make test`.

- [ ] **Step 5: Commit**

```bash
git add src/testexplain/api.py src/testexplain/gateway.py tests/test_demo_mode.py
git commit -m "feat: restrict portal byok providers"
```

### Task 2: Public Route and Demo Quota

**Files:**
- Modify: `src/testexplain/api.py`
- Modify: `tests/test_api.py`
- Modify: `tests/test_demo_mode.py`

- [ ] **Step 1: Write failing public-boundary tests**

Cover public local-path requests returning 404 without leaking the path,
development opt-in behavior, ten demo requests allowed per IP, the eleventh
returning 429, separate client IP buckets, window expiry, and no analysis call
on quota rejection.

- [ ] **Step 2: Verify failure**

Run: `uv run pytest tests/test_api.py tests/test_demo_mode.py -k 'local_path or quota' -v`

Expected: FAIL because the path route is public and no quota exists.

- [ ] **Step 3: Implement the smallest in-memory fixed-window limiter**

Use `Request` client identity with a trusted forwarded client address when
present. Keep limiter state private to `api.py`, expose only a test reset seam,
and check it only for `demo=true` after fake/BYOK precedence. Gate the legacy
route with `TESTEXPLAIN_ENABLE_LOCAL_PATH_API=true`.

- [ ] **Step 4: Verify focused and full tests**

Run: `uv run pytest tests/test_api.py tests/test_demo_mode.py -v` then
`make test`.

- [ ] **Step 5: Commit**

```bash
git add src/testexplain/api.py tests/test_api.py tests/test_demo_mode.py
git commit -m "fix: protect public portal routes"
```

### Task 3: Sample Archive Regression Coverage

**Files:**
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write archive-content tests**

Fetch every `SAMPLE_FILES` entry. Open ZIP responses with `zipfile.ZipFile`.
Assert the trace-only archive has `report.json` and a trace member but no HAR;
assert the combined archive has a report, trace, and HAR member.

- [ ] **Step 2: Verify tests fail if a member assertion is intentionally wrong**

Run: `uv run pytest tests/test_api.py -k sample -v`

Expected: FAIL for the intentionally wrong assertion; restore the intended
assertion before implementation verification.

- [ ] **Step 3: Run tests and commit**

Run: `uv run pytest tests/test_api.py -k sample -v` then `make test`.

```bash
git add tests/test_api.py
git commit -m "test: verify packaged demo sample contents"
```

### Task 4: Provider and Demo UI

**Files:**
- Modify: `src/testexplain/static/index.html`
- Modify: `tests/test_api.py`

- [ ] **Step 1: Write failing HTML contract tests**

Assert the page has provider choices for Demo, OpenAI, Anthropic, and
OpenRouter; has API key/model fields; has no editable base URL; and retains the
four sample actions.

- [ ] **Step 2: Implement the portal controls**

Sample actions always submit `demo=true`. Manual requests submit `provider`,
`api_key`, and `model` only when a BYOK provider is selected. The UI labels the
demo’s 10-per-hour limit and shows server 429 errors without retrying.

- [ ] **Step 3: Verify and commit**

Run: `uv run pytest tests/test_api.py -k index -v` then `make test`.

```bash
git add src/testexplain/static/index.html tests/test_api.py
git commit -m "feat: add safe portal provider controls"
```
