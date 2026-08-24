"""Demo mode: run against the server's own LLM_* configuration.

Gateway precedence for POST /analyze and POST /analyze-bundle:

    fake > api_key (BYOK) > demo > 422 "api_key is required"

No test here ever performs network I/O: the real gateway class is only
constructed with env vars pointing at a closed local port, and analysis
is always intercepted (patched ``analyze_report`` or a FakeGateway).
"""

import json
from pathlib import Path

from fastapi.testclient import TestClient

import testexplain.api as api
from testexplain.api import app
from testexplain.gateway import FakeGateway, OpenAICompatibleGateway
from testexplain.ingestion.package import create_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
REPORT_TEXT = FIXTURE.read_text()

DEMO_ENV = {
    "LLM_API_KEY": "demo-server-key",
    # Port 9 (discard) — resolvable but never contacted in these tests.
    "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_MODEL": "demo-model",
}


def _set_demo_env(monkeypatch):
    for name, value in DEMO_ENV.items():
        monkeypatch.setenv(name, value)


def _clear_demo_env(monkeypatch):
    for name in DEMO_ENV:
        monkeypatch.delenv(name, raising=False)


def _capture_gateway(monkeypatch) -> list:
    """Patch analyze_report to record the gateway and skip the LLM call."""
    captured: list = []

    def fake_analyze_report(path, gateway):
        captured.append(gateway)
        return []

    monkeypatch.setattr(api, "analyze_report", fake_analyze_report)
    return captured


def _create_native_bundle(tmp_path: Path) -> Path:
    report = json.loads(REPORT_TEXT)
    report.update(config={}, errors=[], stats={})
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    return create_bundle(report_path, tmp_path / "bundle.zip").output_path


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


# ------------------------------------------------------------------
# POST /analyze
# ------------------------------------------------------------------


def test_post_analyze_demo_uses_server_env_vars(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    captured = _capture_gateway(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    gateway = captured[0]
    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway.model == "demo-model"
    assert str(gateway.client.base_url).startswith("http://127.0.0.1:9/v1")
    assert gateway.client.api_key == "demo-server-key"


def test_post_analyze_demo_without_server_config_returns_422(monkeypatch):
    client = TestClient(app)
    _clear_demo_env(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "demo mode is not configured" in detail.lower()
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        assert var in detail
    assert "api_key" in detail


def test_post_analyze_byok_takes_precedence_over_demo(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    gateways = []

    class CapturingGateway(FakeGateway):
        def __init__(self, *, api_key=None, base_url=None, model=None):
            super().__init__()
            self.api_key = api_key
            self.base_url = base_url
            self.model = model
            gateways.append(self)

    monkeypatch.setattr(api, "OpenAICompatibleGateway", CapturingGateway)
    response = client.post(
        "/analyze",
        json={
            "report": REPORT_TEXT,
            "demo": True,
            "api_key": "byok-key",
            "base_url": "https://llm.example/v1",
            "model": "byok-model",
        },
    )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.base_url, g.model) for g in gateways] == [
        ("byok-key", "https://llm.example/v1", "byok-model")
    ]


def test_post_analyze_fake_takes_precedence_over_demo(monkeypatch):
    client = TestClient(app)
    _clear_demo_env(monkeypatch)  # fake must work with zero server config

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": True, "demo": True},
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert "FAKE:" in data[0]["summary"]


def test_post_analyze_without_key_fake_or_demo_still_requires_api_key(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT})

    assert response.status_code == 422
    assert response.json()["detail"] == "api_key is required when fake is false."


# ------------------------------------------------------------------
# POST /analyze-bundle
# ------------------------------------------------------------------


def test_post_analyze_bundle_demo_uses_server_env_vars_and_deletes_temp_file(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)
    created = _capture_temp_uploads(monkeypatch, tmp_path)
    captured = _capture_gateway(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"demo": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    gateway = captured[0]
    assert isinstance(gateway, OpenAICompatibleGateway)
    assert gateway.model == "demo-model"
    assert str(gateway.client.base_url).startswith("http://127.0.0.1:9/v1")
    assert len(created) == 1
    assert not created[0].exists()


def test_post_analyze_bundle_demo_without_server_config_returns_422_and_deletes_temp_file(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _clear_demo_env(monkeypatch)
    created = _capture_temp_uploads(monkeypatch, tmp_path)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"demo": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "demo mode is not configured" in detail.lower()
    for var in ("LLM_API_KEY", "LLM_BASE_URL", "LLM_MODEL"):
        assert var in detail
    assert "api_key" in detail
    assert len(created) == 1
    assert not created[0].exists()


def test_post_analyze_bundle_byok_takes_precedence_over_demo(tmp_path, monkeypatch):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)
    gateways = []

    class CapturingGateway(FakeGateway):
        def __init__(self, *, api_key=None, base_url=None, model=None):
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
                "demo": "true",
                "api_key": "byok-key",
                "base_url": "https://llm.example/v1",
                "model": "byok-model",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.base_url, g.model) for g in gateways] == [
        ("byok-key", "https://llm.example/v1", "byok-model")
    ]


def test_post_analyze_bundle_fake_takes_precedence_over_demo(tmp_path, monkeypatch):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _clear_demo_env(monkeypatch)  # fake must work with zero server config

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true", "demo": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert "FAKE:" in data[0]["summary"]
