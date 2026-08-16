import asyncio
import json
from pathlib import Path

from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

import testexplain.api as api
from testexplain.api import app
from testexplain.core import analyze_report
from testexplain.gateway import FakeGateway
from testexplain.ingestion.package import create_bundle

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


# ------------------------------------------------------------------
# POST /analyze-bundle (multipart bundle upload)
# ------------------------------------------------------------------


def _create_native_bundle(tmp_path: Path) -> Path:
    report = json.loads(REPORT_TEXT)
    report.update(config={}, errors=[], stats={})
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    return create_bundle(report_path, tmp_path / "bundle.zip").output_path


def test_post_analyze_bundle_with_fake_gateway(tmp_path):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert data[0]["test_title"] == "user sees dashboard after login"
    assert "FAKE:" in data[0]["summary"]


def test_post_analyze_bundle_rejects_non_zip_upload():
    client = TestClient(app)

    response = client.post(
        "/analyze-bundle",
        data={"fake": "true"},
        files={"bundle": ("not-a-bundle.zip", b"not a ZIP", "application/zip")},
    )

    assert response.status_code == 422


def test_post_analyze_bundle_rejects_upload_larger_than_50_mib(tmp_path, monkeypatch):
    client = TestClient(app)
    upload_path = tmp_path / "too-large.zip"
    # Sparse file: 50 MiB + 1 bytes on the wire without 50 MiB of test memory.
    with upload_path.open("wb") as upload:
        upload.truncate(50 * 1024 * 1024 + 1)

    def analysis_must_not_run(*args, **kwargs):
        raise AssertionError("analysis must not run for an oversized upload")

    monkeypatch.setattr(api, "analyze_report", analysis_must_not_run)
    with upload_path.open("rb") as upload:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("too-large.zip", upload, "application/zip")},
        )

    assert response.status_code == 413


class _UnsizedUpload:
    """A read-only stream whose length httpx cannot peek.

    Without a peekable length httpx sends the body chunked, so no
    Content-Length header reaches the middleware and the request must be
    stopped by the handler's own streaming counter.
    """

    def __init__(self, path: Path):
        self._file = path.open("rb")

    def read(self, size: int = -1) -> bytes:
        return self._file.read(size)

    def close(self) -> None:
        self._file.close()


def test_post_analyze_bundle_rejects_missing_api_key(tmp_path):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "false"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    assert response.json()["detail"] == "api_key is required when fake is false."


def _capture_temp_uploads(monkeypatch, tmp_path) -> list[Path]:
    """Redirect the handler's temp ZIP into tmp_path so leaks are observable."""
    created: list[Path] = []
    real_mkstemp = api.tempfile.mkstemp

    def mkstemp_in_tmp_path(*args, **kwargs):
        fd, path = real_mkstemp(*args, **{**kwargs, "dir": tmp_path})
        created.append(Path(path))
        return fd, path

    monkeypatch.setattr(api.tempfile, "mkstemp", mkstemp_in_tmp_path)
    return created


def test_post_analyze_bundle_rejects_chunked_upload_before_the_endpoint_runs(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    created = _capture_temp_uploads(monkeypatch, tmp_path)
    upload_path = tmp_path / "too-large.zip"
    with upload_path.open("wb") as upload:
        upload.truncate(50 * 1024 * 1024 + 1)

    upload = _UnsizedUpload(upload_path)
    try:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("too-large.zip", upload, "application/zip")},
        )
    finally:
        upload.close()

    assert response.status_code == 413
    assert created == []


def test_post_analyze_bundle_deletes_temp_file_when_api_key_is_missing(
tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    created = _capture_temp_uploads(monkeypatch, tmp_path)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "false"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    assert len(created) == 1
    assert not created[0].exists()


def test_post_analyze_bundle_passes_byok_fields_and_deletes_temp_file_on_success(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    created = _capture_temp_uploads(monkeypatch, tmp_path)
    gateways = []

    class CapturingGateway(FakeGateway):
        def __init__(self, *, api_key, base_url, model):
            super().__init__()
            self.api_key = api_key
            self.base_url = base_url
            self.model = model
            gateways.append(self)

    monkeypatch.setattr(api, "OpenAICompatibleGateway", CapturingGateway)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={
                "api_key": "byok-key",
                "base_url": "https://llm.example/v1",
                "model": "test-model",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert [(gateway.api_key, gateway.base_url, gateway.model) for gateway in gateways] == [
        ("byok-key", "https://llm.example/v1", "test-model")
    ]
    assert len(created) == 1
    assert not created[0].exists()


def test_post_analyze_bundle_propagates_invalid_llm_response_as_server_error(
    tmp_path, monkeypatch
):
    client = TestClient(app, raise_server_exceptions=False)
    bundle_path = _create_native_bundle(tmp_path)

    class InvalidResponseGateway(FakeGateway):
        def __init__(self, **kwargs):
            super().__init__(response="not JSON")

    monkeypatch.setattr(api, "OpenAICompatibleGateway", InvalidResponseGateway)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"api_key": "byok-key"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 500


def test_post_analyze_bundle_deletes_temp_file_when_upload_close_fails(
    tmp_path, monkeypatch
):
    client = TestClient(app, raise_server_exceptions=False)
    bundle_path = _create_native_bundle(tmp_path)
    created = _capture_temp_uploads(monkeypatch, tmp_path)

    async def close_fails(self):
        raise RuntimeError("close failed")

    monkeypatch.setattr(StarletteUploadFile, "close", close_fails)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 500
    assert len(created) == 1
    assert not created[0].exists()


def test_post_analyze_bundle_rejects_oversized_content_length_before_the_endpoint_runs(
tmp_path, monkeypatch
):
    client = TestClient(app)
    # An empty list proves the handler never started: it always creates a temp
    # ZIP as its first step.
    created = _capture_temp_uploads(monkeypatch, tmp_path)
    upload_path = tmp_path / "too-large.zip"
    with upload_path.open("wb") as upload:
        upload.truncate(50 * 1024 * 1024 + 1)

    with upload_path.open("rb") as upload:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("too-large.zip", upload, "application/zip")},
        )

    assert response.status_code == 413
    assert created == []


def test_get_analyze_bundle_with_oversized_content_length_preserves_405_routing():
    client = TestClient(app)

    response = client.get(
        "/analyze-bundle",
        headers={"Content-Length": str(api.MAX_BUNDLE_UPLOAD_BYTES + 1)},
    )

    assert response.status_code == 405


def test_post_analyze_bundle_runs_analysis_off_the_event_loop(tmp_path, monkeypatch):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    real_analyze_report = api.analyze_report
    on_event_loop: list[bool] = []

    def record_calling_context(path, gateway):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_event_loop.append(False)
        else:
            on_event_loop.append(True)
        return real_analyze_report(path, gateway)

    monkeypatch.setattr(api, "analyze_report", record_calling_context)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert on_event_loop == [False]


def test_index_serves_html_form():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert "Playwright" in response.text


def test_index_serves_zip_bundle_picker():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'accept=".zip"' in response.text


def test_index_references_analyze_bundle_endpoint():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert "/analyze-bundle" in response.text


def test_index_includes_client_side_bundle_size_limit():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    # Mirrors api.MAX_BUNDLE_UPLOAD_BYTES so the UI rejects oversized ZIPs
    # before they hit the network.
    assert "50 * 1024 * 1024" in response.text


def test_index_posts_to_both_analyze_endpoints():
    client = TestClient(app)

    response = client.get("/")

    assert response.status_code == 200
    assert 'fetch("/analyze"' in response.text
    assert 'fetch("/analyze-bundle"' in response.text


def test_index_blocks_submit_while_selected_json_file_is_still_loading():
    client = TestClient(app)

    response = client.get("/")

    assert "let pendingJsonRead = false;" in response.text
    assert "reportText.value = \"\";" in response.text
    assert "pendingJsonRead = true;" in response.text
    assert "reader.onloadend" in response.text
    assert "Wait for the JSON file to finish loading." in response.text
