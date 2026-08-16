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
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from testexplain.core import analyze_report
from testexplain.gateway import FakeGateway, OpenAICompatibleGateway
from testexplain.ingestion.input_reader import InvalidBundleError
from testexplain.models import FailureAnalysis

load_dotenv()

app = FastAPI(title="TestLens", description="Explain why your tests failed.")

MAX_BUNDLE_UPLOAD_BYTES = 50 * 1024 * 1024
UPLOAD_CHUNK_BYTES = 1024 * 1024
BUNDLE_UPLOAD_PATH = "/analyze-bundle"


class _BundleUploadTooLarge(BaseException):
    """Internal control signal that bypasses FastAPI's exception handlers."""


class BundleUploadSizeLimitMiddleware:
    """Reject oversized bundle request bodies before multipart parsing."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if (
            scope["type"] != "http"
            or scope["method"] != "POST"
            or scope["path"] != BUNDLE_UPLOAD_PATH
        ):
            await self.app(scope, receive, send)
            return

        headers = dict(scope["headers"])
        declared_length = headers.get(b"content-length", b"").decode()
        if declared_length.isdigit() and int(declared_length) > MAX_BUNDLE_UPLOAD_BYTES:
            await JSONResponse(
                status_code=413,
                content={"detail": "Bundle upload exceeds 50 MiB limit."},
            )(scope, receive, send)
            return

        received_bytes = 0

        async def limited_receive():
            nonlocal received_bytes
            message = await receive()
            if message["type"] == "http.request":
                received_bytes += len(message.get("body", b""))
                if received_bytes > MAX_BUNDLE_UPLOAD_BYTES:
                    raise _BundleUploadTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BundleUploadTooLarge:
            await JSONResponse(
                status_code=413,
                content={"detail": "Bundle upload exceeds 50 MiB limit."},
            )(scope, receive, send)


app.add_middleware(BundleUploadSizeLimitMiddleware)


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
    api_key: str | None = None
    base_url: str | None = None
    model: str | None = None
    fake: bool = False


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

    if body.fake:
        gateway = FakeGateway()
    else:
        if not body.api_key:
            raise HTTPException(
                status_code=422,
                detail="api_key is required when fake is false.",
            )
        gateway = OpenAICompatibleGateway(
            api_key=body.api_key,
            base_url=body.base_url,
            model=body.model,
        )

    path = _write_tmp_report(body.report)
    try:
        return analyze_report(path, gateway)
    finally:
        os.unlink(path)


def _write_tmp_report(content: str) -> str:
    """Write report content to a temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=".json", prefix="testlens-")
    with os.fdopen(fd, "w") as f:
        f.write(content)
    return path


# ------------------------------------------------------------------
# POST bundle endpoint — multipart ZIP upload + BYOK
# ------------------------------------------------------------------


@app.post("/analyze-bundle", response_model=list[FailureAnalysis])
async def analyze_bundle(
    bundle: UploadFile = File(...),
    api_key: str | None = Form(default=None),
    base_url: str | None = Form(default=None),
    model: str | None = Form(default=None),
    fake: bool = Form(default=False),
) -> list[FailureAnalysis]:
    """Analyze an uploaded failure-evidence ZIP bundle."""
    fd, path = tempfile.mkstemp(suffix=".zip", prefix="testlens-")
    try:
        uploaded_bytes = 0
        with os.fdopen(fd, "wb") as destination:
            while chunk := await bundle.read(UPLOAD_CHUNK_BYTES):
                uploaded_bytes += len(chunk)
                if uploaded_bytes > MAX_BUNDLE_UPLOAD_BYTES:
                    raise HTTPException(
                        status_code=413,
                        detail="Bundle upload exceeds 50 MiB limit.",
                    )
                destination.write(chunk)

        if fake:
            gateway = FakeGateway()
        else:
            if not api_key:
                raise HTTPException(
                    status_code=422,
                    detail="api_key is required when fake is false.",
                )
            gateway = OpenAICompatibleGateway(
                api_key=api_key,
                base_url=base_url,
                model=model,
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
            Path(path).unlink(missing_ok=True)


# ------------------------------------------------------------------
# Root — serve the HTML form
# ------------------------------------------------------------------

_HERE = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_HERE / "static" / "index.html").read_text()
