"""HTTP API (FastAPI).

Exposes the same analysis pipeline as the CLI over HTTP. Two endpoints:

    GET  /analyze?report_path=...&fake=true   — local file path (legacy)
    POST /analyze                              — upload report + BYOK

Returns a JSON list of FailureAnalysis objects. FastAPI uses the
Pydantic models as the response schema, so serialisation + validation
are automatic.
"""

import json
import os
import tempfile
import zipfile
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from testexplain.core import analyze_report
from testexplain.gateway import AnthropicGateway, FakeGateway, OpenAICompatibleGateway
from testexplain.ingestion.input_reader import InvalidBundleError
from testexplain.models import FailureAnalysis

load_dotenv()

app = FastAPI(title="TestLens", description="Explain why your tests failed.")

MAX_BUNDLE_UPLOAD_BYTES = 50 * 1024 * 1024
MAX_REPORT_UPLOAD_BYTES = 10 * 1024 * 1024
# Multipart boundaries and field headers sit outside the uploaded ZIP bytes.
MAX_MULTIPART_OVERHEAD_BYTES = 1024 * 1024
MAX_BUNDLE_REQUEST_BYTES = MAX_BUNDLE_UPLOAD_BYTES + MAX_MULTIPART_OVERHEAD_BYTES
UPLOAD_CHUNK_BYTES = 1024 * 1024
UPLOAD_LIMITS = {
    "/analyze": (MAX_REPORT_UPLOAD_BYTES, "Report upload exceeds 10 MiB limit."),
    "/analyze-bundle": (
        MAX_BUNDLE_REQUEST_BYTES,
        "Bundle upload exceeds 50 MiB limit.",
    ),
}
# Demo mode spends the server's own quota, so it requires the full trio.
DEMO_ENV_VARS = ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL")
BYOK_PROVIDER_URLS = {
    "openai": "https://api.openai.com/v1",
    "openrouter": "https://openrouter.ai/api/v1",
}
BYOK_PROVIDERS = frozenset({*BYOK_PROVIDER_URLS, "anthropic"})


class _UploadTooLarge(BaseException):
    """Internal control signal that bypasses FastAPI's exception handlers."""


class UploadSizeLimitMiddleware:
    """Reject oversized public analysis bodies before FastAPI reads them."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return

        limit = UPLOAD_LIMITS.get(scope["path"])
        if limit is None:
            await self.app(scope, receive, send)
            return

        max_bytes, detail = limit
        headers = dict(scope["headers"])
        declared_length = headers.get(b"content-length", b"").decode()
        if declared_length.isdigit() and int(declared_length) > max_bytes:
            await JSONResponse(status_code=413, content={"detail": detail})(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
            if received_bytes > max_bytes:
                raise _UploadTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _UploadTooLarge:
            await JSONResponse(status_code=413, content={"detail": detail})(scope, receive, send)


app.add_middleware(UploadSizeLimitMiddleware)


def _gateway_for_request(
    *,
    api_key: str | None,
    provider: str | None,
    base_url: str | None,
    model: str | None,
    fake: bool,
    demo: bool,
    byok_supplied: bool | None = None,
):
    """Pick the gateway for one request.

    Precedence: fake (offline) > caller's own key (BYOK) > demo (the
    server's own LLM_* configuration) > refuse.
    """
    if fake:
        return FakeGateway()
    byok_fields = {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url,
        "model": model,
    }
    if byok_supplied is None:
        byok_supplied = any(value is not None for value in byok_fields.values())
    if byok_supplied:
        if base_url is not None:
            raise HTTPException(
                status_code=422,
                detail="Custom base_url values are not supported by the public portal.",
            )
        missing = [
            name
            for name, value in byok_fields.items()
            if name != "base_url"
            if not value or not value.strip()
        ]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"BYOK requires request-level {', '.join(missing)}.",
            )
        if provider not in BYOK_PROVIDERS:
            raise HTTPException(
                status_code=422,
                detail="Unsupported BYOK provider. Choose openai, anthropic, or openrouter.",
            )
        if provider == "anthropic":
            return AnthropicGateway(api_key=api_key, model=model)
        return OpenAICompatibleGateway(
            api_key=api_key,
            base_url=BYOK_PROVIDER_URLS[provider],
            model=model,
        )
    if demo:
        # Stricter than the gateway itself, which tolerates an empty
        # LLM_API_KEY for no-auth LAN endpoints. A public demo must not
        # silently fall back to an unauthenticated call.
        missing = [name for name in DEMO_ENV_VARS if not os.environ.get(name, "").strip()]
        if missing:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Demo mode is not configured on this server "
                    f"(missing {', '.join(missing)}). "
                    f"Set {', '.join(DEMO_ENV_VARS)}, "
                    "or provide your own api_key."
                ),
            )
        return OpenAICompatibleGateway()
    raise HTTPException(
        status_code=422,
        detail="api_key is required when fake is false.",
    )


# ------------------------------------------------------------------
# Legacy GET endpoint — local file path, mostly for development
# ------------------------------------------------------------------

@app.get("/analyze", response_model=list[FailureAnalysis])
def analyze(report_path: str, fake: bool = False) -> list[FailureAnalysis]:
    """Analyze a Playwright report and return an explanation per failure."""
    if not Path(report_path).exists():
        raise HTTPException(status_code=404, detail=f"Report not found: {report_path}")

    if fake:
        gateway = FakeGateway()
    else:
        try:
            gateway = OpenAICompatibleGateway()
        except KeyError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"Missing environment variable {exc}. "
                "Set LLM_* vars or use ?fake=true for a dry run.",
            )

    return analyze_report(report_path, gateway)


# ------------------------------------------------------------------
# POST endpoint — bring your own key + report content
# ------------------------------------------------------------------

class AnalyzeRequest(BaseModel):
    report: str
    provider: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    fake: bool = False
    demo: bool = False


@app.post("/analyze", response_model=list[FailureAnalysis])
def analyze_post(body: AnalyzeRequest) -> list[FailureAnalysis]:
    """Analyze a Playwright report uploaded as JSON text.

    Accepts the report content and LLM configuration in the request
    body (bring-your-own-key).  When *fake* is true no real LLM is
    called — useful for testing the integration.
    """
    # Validate that the report is parseable JSON.
    try:
        json.loads(body.report)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            status_code=422,
            detail=f"Report is not valid JSON: {exc}",
        )

    gateway = _gateway_for_request(
        api_key=body.api_key,
        provider=body.provider,
        base_url=body.base_url,
        model=body.model,
        fake=body.fake,
        demo=body.demo,
    )

    path = _write_tmp_report(body.report)
    try:
        return analyze_report(path, gateway)
    finally:
        os.unlink(path)


def _write_tmp_report(content: str) -> str:
    """Write report content to a temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="testlens-")
    owns_fd = True
    try:
        with os.fdopen(fd, "w") as f:
            owns_fd = False
            f.write(content)
        return path
    except Exception:
        if owns_fd:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            Path(path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------
# POST bundle endpoint — multipart ZIP upload + BYOK
# ------------------------------------------------------------------


@app.post("/analyze-bundle", response_model=list[FailureAnalysis])
async def analyze_bundle(
    request: Request,
    bundle: UploadFile = File(...),
    provider: str | None = Form(default=None),
    api_key: str | None = Form(default=None),
    base_url: str | None = Form(default=None),
    model: str | None = Form(default=None),
    fake: bool = Form(default=False),
    demo: bool = Form(default=False),
) -> list[FailureAnalysis]:
    """Analyze an uploaded failure-evidence ZIP bundle."""
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="testlens-")
    owns_fd = True
    try:
        uploaded_bytes = 0
        with os.fdopen(fd, "wb") as destination:
            owns_fd = False
            while chunk := await bundle.read(UPLOAD_CHUNK_BYTES):
                uploaded_bytes += len(chunk)
                if uploaded_bytes > MAX_BUNDLE_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Bundle upload exceeds 50 MiB limit.",
                )
                destination.write(chunk)

        form_data = await request.form()
        gateway = _gateway_for_request(
            api_key=api_key,
            provider=provider,
            base_url=base_url,
            model=model,
            fake=fake,
            demo=demo,
            byok_supplied=any(
                name in form_data
                for name in ("provider", "api_key", "base_url", "model")
            ),
        )

        try:
            # analyze_report is synchronous; off-loading it keeps this async
            # handler from blocking the event loop for the whole analysis.
            return await run_in_threadpool(analyze_report, path, gateway)
        except (InvalidBundleError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        try:
            await bundle.close()
        finally:
            if owns_fd:
                try:
                    os.close(fd)
                except OSError:
                    pass
            Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------
# Root — serve the HTML form
# ------------------------------------------------------------------

_HERE = Path(__file__).parent
_SAMPLES_DIR = _HERE / "static" / "samples"
SAMPLE_FILES = {
    "checkout-report.json": "checkout-report.json",
    "checkout-trace.zip": "checkout-trace.zip",
    "checkout-trace-har.zip": "checkout-trace-har.zip",
    "missing-trace-report.json": "missing-trace-report.json",
}


@app.get("/samples/{sample_name}")
def sample(sample_name: str) -> FileResponse:
    """Serve one of the fixed demo inputs packaged with the application."""
    filename = SAMPLE_FILES.get(sample_name)
    if filename is None:
        raise HTTPException(status_code=404, detail="Sample not found")
    return FileResponse(_SAMPLES_DIR / filename)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_HERE / "static" / "index.html").read_text()
