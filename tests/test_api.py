from pathlib import Path

from fastapi.testclient import TestClient

from testexplain.api import app
from testexplain.core import analyze_report
from testexplain.gateway import FakeGateway

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
REPORT_TEXT = FIXTURE.read_text()


def test_analyze_endpoint_with_fake_gateway():
    client = TestClient(app)

    # Using the sample fixture fixture.
    response = client.get(
        "/analyze",
        params={
            "report_path": "tests/fixtures/sample_report.json",
            "fake": "true",
        },
    )

    assert response.status_code == 200
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["test_title"] == "user sees dashboard after login"
    assert "FAKE:" in data[0]["summary"]
    assert data[0]["suspected_category"] == "flaky"
    assert 0.0 <= data[0]["confidence"] <= 1.0


def test_analyze_endpoint_returns_error_for_missing_file():
    client = TestClient(app)

    response = client.get(
        "/analyze",
        params={
            "report_path": "nonexistent.json",
            "fake": "true",
        },
    )

    # File not found should be a 4xx client error, not a server crash.
    assert response.status_code == 404


# ------------------------------------------------------------------
# POST /analyze (BYOK endpoint)
# ------------------------------------------------------------------

def test_post_analyze_with_fake_gateway():
    client = TestClient(app)

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": True},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["test_title"] == "user sees dashboard after login"
    assert "FAKE:" in data[0]["summary"]


def test_post_analyze_rejects_invalid_json():
    client = TestClient(app)

    response = client.post(
        "/analyze",
        json={"report": "this is not json", "fake": True},
    )

    assert response.status_code == 422
    assert "not valid JSON" in response.json()["detail"]


def test_post_analyze_rejects_missing_api_key():
    client = TestClient(app)

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": False},
    )

    assert response.status_code == 422
    assert "api_key is required" in response.json()["detail"]


def test_index_serves_html_form():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Playwright" in response.text