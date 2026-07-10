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
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from testexplain.core import analyze_report
from testexplain.gateway import FakeGateway, OpenAICompatibleGateway
from testexplain.models import FailureAnalysis

load_dotenv()

app = FastAPI(title="TestLens", description="Explain why your tests failed.")


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
# Root — serve the HTML form
# ------------------------------------------------------------------

_HERE = Path(__file__).parent


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (_HERE / "static" / "index.html").read_text()