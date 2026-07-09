"""HTTP API (FastAPI).

Exposes the same analysis pipeline as the CLI over HTTP, so a dashboard
(or any client) can request explanations. For M0 this is a single
read-only GET endpoint.

    GET /analyze?report_path=...&fake=true

Returns a JSON list of FailureAnalysis objects. FastAPI uses the
Pydantic models as the response schema, so serialization + validation
are automatic.
"""

from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException

from testlens.core import analyze_report
from testlens.gateway import FakeGateway, OpenAICompatibleGateway
from testlens.models import FailureAnalysis

load_dotenv()

app = FastAPI(title="TestLens", description="Explain why your tests failed.")


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