import asyncio
import io
import json
from pathlib import Path
import zipfile

import pytest
from fastapi.testclient import TestClient
from starlette.datastructures import UploadFile as StarletteUploadFile

import testexplain.api as api
from testexplain.api import app
from testexplain.core import analyze_report
from testexplain.gateway import FakeGateway
from testexplain.ingestion.package import create_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
REPORT = json.loads(FIXTURE.read_text())
REPORT.update(config={}, errors=[], stats={})
REPORT_TEXT = json.dumps(REPORT)


# ------------------------------------------------------------------
# GET /samples/{sample_name} (packaged demo inputs)
# ------------------------------------------------------------------


def test_sample_report_returns_playwright_json():
    client = TestClient(app)

    response = client.get("/samples/checkout-report.json")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["suites"]


def test_sample_bundle_returns_zip():
    client = TestClient(app)

    response = client.get("/samples/checkout-trace.zip")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/zip")


@pytest.mark.parametrize(
    "sample_name",
    sorted(
        (
            "checkout-report.json",
            "checkout-trace.zip",
            "checkout-trace-har.zip",
            "missing-trace-report.json",
        )
    ),
)
def test_every_packaged_sample_is_served(sample_name):
    response = TestClient(app).get(f"/samples/{sample_name}")

    assert response.status_code == 200


def test_trace_only_sample_contains_no_har():
    response = TestClient(app).get("/samples/checkout-trace.zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        members = archive.namelist()

    assert "report.json" in members
    assert any(member.endswith("checkout.trace.zip") for member in members)
    assert not any(member.endswith("checkout.har") for member in members)


def test_trace_har_sample_contains_both_artifacts():
    response = TestClient(app).get("/samples/checkout-trace-har.zip")

    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        members = archive.namelist()

    assert "report.json" in members
    assert any(member.endswith("checkout.trace.zip") for member in members)
    assert any(member.endswith("checkout.har") for member in members)


def test_sample_route_rejects_unknown_names_and_path_traversal():
    client = TestClient(app)

    for sample_name in (
        "not-a-sample.json",
        # Relative to static/samples/ these resolve to real source files.
        "..%2Findex.html",
        "..%2F..%2Fapi.py",
    ):
        response = client.get(f"/samples/{sample_name}")
        assert response.status_code == 404


def test_analyze_endpoint_with_fake_gateway(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("TESTEXPLAIN_ENABLE_LOCAL_PATH_API", "true")

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


def test_local_path_analyze_endpoint_is_disabled_by_default(monkeypatch):
    client = TestClient(app)
    monkeypatch.delenv("TESTEXPLAIN_ENABLE_LOCAL_PATH_API", raising=False)

    response = client.get(
        "/analyze",
        params={"report_path": "missing-private-report.json", "fake": "true"},
    )

    assert response.status_code == 404
    assert response.json()["detail"] == "Not found"
    assert "missing-private-report.json" not in response.text


def test_local_path_analyze_endpoint_requires_explicit_development_opt_in(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("TESTEXPLAIN_ENABLE_LOCAL_PATH_API", "true")

    response = client.get(
        "/analyze",
        params={"report_path": str(FIXTURE), "fake": "true"},
    )

    assert response.status_code == 200


def test_analyze_endpoint_returns_error_for_missing_file(monkeypatch):
    client = TestClient(app)
    monkeypatch.setenv("TESTEXPLAIN_ENABLE_LOCAL_PATH_API", "true")

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


def test_post_analyze_rejects_valid_json_that_is_not_a_playwright_report(monkeypatch):
    monkeypatch.setattr(api, "analyze_report", lambda *args: (_ for _ in ()).throw(AssertionError()))

    response = TestClient(app).post(
        "/analyze",
        json={"report": json.dumps({"suites": []}), "fake": True},
    )

    assert response.status_code == 422
    assert "report" in response.json()["detail"].lower()


def test_post_analyze_rejects_missing_api_key():
    client = TestClient(app)

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": False},
    )

    assert response.status_code == 422
    assert "api_key is required" in response.json()["detail"]


def test_post_analyze_rejects_oversized_content_length_before_writing_report(
    monkeypatch,
):
    client = TestClient(app)

    def must_not_run(*args, **kwargs):
        raise AssertionError("oversized reports must not reach the endpoint")

    monkeypatch.setattr(api, "_write_tmp_report", must_not_run)
    monkeypatch.setattr(api, "analyze_report", must_not_run)

    response = client.post(
        "/analyze",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(api.MAX_REPORT_UPLOAD_BYTES + 1),
        },
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Report upload exceeds 10 MiB limit."


def test_post_analyze_rejects_chunked_oversized_report_before_writing_report(
    monkeypatch,
):
    client = TestClient(app)

    def must_not_run(*args, **kwargs):
        raise AssertionError("oversized reports must not reach the endpoint")

    monkeypatch.setattr(api, "_write_tmp_report", must_not_run)
    monkeypatch.setattr(api, "analyze_report", must_not_run)

    def body_chunks():
        yield b'{"report":"'
        yield b"x" * (api.MAX_REPORT_UPLOAD_BYTES + 1)
        yield b'", "fake": true}'

    response = client.post(
        "/analyze",
        content=body_chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["detail"] == "Report upload exceeds 10 MiB limit."


def test_get_analyze_with_oversized_content_length_preserves_validation_routing():
    client = TestClient(app)

    response = client.get(
        "/analyze",
        headers={"Content-Length": str(api.MAX_REPORT_UPLOAD_BYTES + 1)},
    )

    assert response.status_code == 422


def test_write_tmp_report_deletes_file_if_writing_fails(tmp_path, monkeypatch):
    created: list[Path] = []
    real_mkstemp = api.tempfile.mkstemp

    def mkstemp_in_tmp_path(*args, **kwargs):
        fd, path = real_mkstemp(*args, **{**kwargs, "dir": tmp_path})
        created.append(Path(path))
        return fd, path

    class FailingFile:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def write(self, content):
            raise OSError("disk full")

    monkeypatch.setattr(api.tempfile, "mkstemp", mkstemp_in_tmp_path)
    monkeypatch.setattr(api.os, "fdopen", lambda *args, **kwargs: FailingFile())

    with pytest.raises(OSError, match="disk full"):
        api._write_tmp_report("{}")

    assert len(created) == 1
    assert not created[0].exists()


def test_write_tmp_report_closes_descriptor_and_deletes_file_if_fdopen_fails(
    tmp_path, monkeypatch
):
    created: list[Path] = []
    closed: list[int] = []
    real_mkstemp = api.tempfile.mkstemp

    def mkstemp_in_tmp_path(*args, **kwargs):
        fd, path = real_mkstemp(*args, **{**kwargs, "dir": tmp_path})
        created.append(Path(path))
        return fd, path

    def fdopen_fails(*args, **kwargs):
        raise OSError("cannot open")

    monkeypatch.setattr(api.tempfile, "mkstemp", mkstemp_in_tmp_path)
    monkeypatch.setattr(api.os, "fdopen", fdopen_fails)
    monkeypatch.setattr(api.os, "close", closed.append)

    with pytest.raises(OSError, match="cannot open"):
        api._write_tmp_report("{}")

    assert len(created) == 1
    assert closed
    assert not created[0].exists()


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


def test_bundle_wire_limit_allows_multipart_framing_around_a_50_mib_file():
    assert api.MAX_BUNDLE_REQUEST_BYTES > api.MAX_BUNDLE_UPLOAD_BYTES


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


def test_post_analyze_bundle_rejects_chunked_upload_larger_than_50_mib_and_cleans_up(
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
    assert len(created) == 1
    assert not created[0].exists()


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
            "provider": "openai",
            "api_key": "byok-key",
            "model": "test-model",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert [(gateway.api_key, gateway.base_url, gateway.model) for gateway in gateways] == [
        ("byok-key", "https://api.openai.com/v1", "test-model")
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
        data={
            "provider": "openai",
            "api_key": "byok-key",
            "model": "test-model",
            },
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


def test_post_analyze_bundle_closes_descriptor_when_fdopen_fails(tmp_path, monkeypatch):
    client = TestClient(app, raise_server_exceptions=False)
    bundle_path = _create_native_bundle(tmp_path)
    created: list[Path] = []
    closed: list[int] = []
    real_mkstemp = api.tempfile.mkstemp

    def mkstemp_in_tmp_path(*args, **kwargs):
        fd, path = real_mkstemp(*args, **{**kwargs, "dir": tmp_path})
        created.append(Path(path))
        return fd, path

    def fdopen_fails(*args, **kwargs):
        raise OSError("cannot open")

    monkeypatch.setattr(api.tempfile, "mkstemp", mkstemp_in_tmp_path)
    monkeypatch.setattr(api.os, "fdopen", fdopen_fails)
    monkeypatch.setattr(api.os, "close", closed.append)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 500
    assert len(created) == 1
    assert closed
    assert not created[0].exists()


def test_post_analyze_bundle_rejects_file_larger_than_50_mib_and_cleans_up(
tmp_path, monkeypatch
):
    client = TestClient(app)
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
    assert len(created) == 1
    assert not created[0].exists()


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
    real_analyze_loaded_input = api.analyze_loaded_input
    on_event_loop: list[bool] = []

    def record_calling_context(loaded, gateway):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            on_event_loop.append(False)
        else:
            on_event_loop.append(True)
        return real_analyze_loaded_input(loaded, gateway)

    monkeypatch.setattr(api, "analyze_loaded_input", record_calling_context)
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
