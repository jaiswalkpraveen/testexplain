from fastapi.testclient import TestClient

from testlens.api import app


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
    assert "FAKE:" in data[0]["explanation"]


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