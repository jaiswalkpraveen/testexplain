# Collecting Playwright Artifacts

TestExplain can analyze a native Playwright JSON report by itself or a ZIP
bundle containing that report and the trace/HAR artifacts referenced by failed
results. The useful unit is one failed **attempt**: its report error, stdout and
stderr, trace events, and network records are correlated before the prompt is
sent to the model.

## Produce The Report

Configure Playwright's built-in JSON reporter. The report must be the native
Playwright JSON shape, not an HTML report, JUnit XML file, CTRF document, or a
custom summary:

```ts
// playwright.config.ts
import { defineConfig } from "@playwright/test";

export default defineConfig({
  reporter: [["json", { outputFile: "artifacts/playwright-report.json" }]],
  outputDir: "artifacts/test-results",
  use: {
    trace: "retain-on-failure",
  },
});
```

Run the tests from the project directory:

```bash
npx playwright test
```

The JSON reporter writes a report at `artifacts/playwright-report.json`. The
test runner writes traces according to its output directory and records the
trace path in the failed result's `attachments` list. Keep the report and its
referenced artifact files together until you bundle them.

## Record A HAR

Use Playwright's browser-context HAR recording when the failed test needs an
independent network artifact. A minimal fixture setup looks like this:

```ts
import { test as base } from "@playwright/test";

export const test = base.extend({
  context: async ({ browser }, use, testInfo) => {
    const harPath = testInfo.outputPath("network.har");
    const context = await browser.newContext({
      recordHar: {
        path: harPath,
        content: "embed",
      },
    });
    await use(context);
    await context.close();
    await testInfo.attach("network.har", {
      path: harPath,
      contentType: "application/json",
    });
  },
});
```

If the HAR is created outside a fixture, attach it to the result so the
report identifies which attempt owns it:

```ts
await testInfo.attach("network.har", {
  path: harPath,
  contentType: "application/json",
});
```

`content: "embed"` produces one plain `.har` JSON file. Playwright can also
produce an attached HAR ZIP with external response-body members. TestExplain
supports both shapes when the attachment points to the archive.

## Expected Layout

The exact trace filename depends on the Playwright runner and output settings.
The important relationship is the attachment path in the result, not a global
filename convention:

```text
artifacts/
├── playwright-report.json
├── test-results/
│   └── checkout-customer-can-place-an-order/
│       └── trace.zip
└── network/
    └── checkout.har
```

The failed result should contain attachment records similar to:

```json
{
  "name": "trace.zip",
  "contentType": "application/zip",
  "path": "test-results/checkout-customer-can-place-an-order/trace.zip"
}
```

For a report supplied as a standalone JSON file, relative paths resolve from
the report's parent directory. TestExplain follows only attachments belonging
to the current failed result. It does not scan the whole directory and attach
unrelated files. Absolute paths are accepted for standalone local reports when
they exist; bundle members must remain inside the ZIP extraction directory.

## Validate A Report Offline

Run the fake gateway first. This parses the report, reads available attachments,
redacts common secrets in normalized artifact evidence, assembles a bounded
prompt, and makes no network call. Review report error and stack fields too;
they are prompt inputs and may contain sensitive text from the test runner:

```bash
uv run testexplain analyze artifacts/playwright-report.json --fake
```

Use the fixture supplied with this repository for a known-good smoke test:

```bash
uv run testexplain analyze tests/fixtures/m2/checkout-report.json --fake
```

The fake gateway is for validation only. Its response is canned, but the
captured pipeline still exercises report parsing, trace/HAR adapters, evidence
assembly, and prompt construction.

## Build A Bundle

The bundle command validates the report before writing the archive. It includes
only attachments from unexpected failed or timed-out results and rewrites their
paths to safe `artifacts/NNN-name` members:

```bash
uv run testexplain bundle \
  artifacts/playwright-report.json \
  --output artifacts/failure-bundle.zip
```

Warnings are printed to standard error. Missing, unsupported, pathless, inline,
or unsafe attachments are not silently treated as valid evidence. Inspect the
warning and fix the collection setup when an expected artifact is absent.

Analyze the resulting bundle offline:

```bash
uv run testexplain analyze artifacts/failure-bundle.zip --fake
```

The bundle reader requires exactly one native Playwright JSON report. It also
checks member paths, duplicate names, member count, uncompressed member size,
total expansion size, and compression ratio before extraction. These limits
protect the process from unsafe archives; they are separate from the HTTP
transport upload limit.

## Upload A Bundle

Start the API locally:

```bash
make run-api
```

For an offline request, upload the ZIP with `fake=true`:

```bash
curl -X POST http://127.0.0.1:8000/analyze-bundle \
  -F "bundle=@artifacts/failure-bundle.zip" \
  -F "fake=true"
```

The endpoint streams the compressed upload to a temporary file, limits it to
50 MiB, analyzes it, and deletes the temporary file after success or failure.
The bundle reader's expansion limits still apply after upload. A real gateway
requires `api_key`, `base_url`, and `model` configuration as documented by the
API; never put a real key in a report, HAR, trace, shell history, or committed
fixture.

## Troubleshooting

### `path ... does not exist`

The report points to an artifact that is not beside the report at the recorded
relative path. Copy the complete Playwright result directory, or correct the
report/attachment generation before bundling. Analysis continues with a
visible evidence-gap warning, but the missing source cannot contribute facts.

### `Input must be a JSON report or ZIP bundle`

The input is not a native Playwright JSON report and is not a ZIP. Export the
JSON reporter output instead of HTML, JUnit, CTRF, or a custom report.

### `Bundle ZIP must contain exactly one Playwright JSON report`

The archive has no recognizable native report or has more than one. Create the
bundle with `testexplain bundle REPORT --output BUNDLE.zip`; do not zip an
entire workspace containing several reports.

### `unreadable ZIP` or an invalid-upload response

The archive may be truncated, corrupt, unsafe, or contain unsupported paths.
Recreate it with the bundle command. Do not manually bypass path or size checks.

### `413` or an oversized upload

The compressed HTTP upload is over 50 MiB. Remove unrelated videos/screenshots,
bundle only the report's failed-attempt artifacts, or validate the report
locally first. A small compressed ZIP can still be rejected after extraction if
its uncompressed members exceed the reader's safety limits.

### Secrets or personal data in artifacts

TestExplain performs deterministic redaction of common authorization headers,
cookies, API keys, query values, and JSON secret fields in normalized artifact
evidence before prompt composition. Report metadata, error messages, stack
traces, and other prompt fields may still contain sensitive text, so redaction
is a safety aid rather than a guarantee. Review the report, trace, HAR,
screenshots, and response bodies yourself and remove credentials, tokens,
customer data, and internal URLs before uploading or committing them.
