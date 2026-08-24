"""Demo mode: run against the server's own LLM_* configuration.

Gateway precedence for POST /analyze and POST /analyze-bundle:

    fake > api_key (BYOK) > demo > 422 "api_key is required"

No test here ever performs network I/O: the real gateway class is only
constructed with env vars pointing at a closed local port, and analysis
is always intercepted (patched ``analyze_report`` or a FakeGateway).
"""

import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

import testexplain.api as api
from testexplain.api import app
from testexplain.gateway import AnthropicGateway, FakeGateway, OpenAICompatibleGateway
from testexplain.ingestion.package import create_bundle

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"
REPORT = json.loads(FIXTURE.read_text())
REPORT.update(config={}, errors=[], stats={})
REPORT_TEXT = json.dumps(REPORT)

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
    """Record gateways while bypassing both report and preloaded-input analysis."""
    captured: list = []

    def fake_analysis(input_value, gateway):
        captured.append(gateway)
        return []

    monkeypatch.setattr(api, "analyze_report", fake_analysis)
    monkeypatch.setattr(api, "analyze_loaded_input", fake_analysis)
    return captured


def _forbid_real_gateway(monkeypatch):
    """Make constructing the real gateway an outright test failure."""

    def must_not_construct(*args, **kwargs):
        raise AssertionError(
            f"OpenAICompatibleGateway must not be constructed (args={args}, kwargs={kwargs})"
        )

    monkeypatch.setattr(api, "OpenAICompatibleGateway", must_not_construct)


def _forbid_provider_gateways(monkeypatch):
    """Rejected public BYOK requests must not construct any real gateway."""

    def must_not_construct(*args, **kwargs):
        raise AssertionError(
            f"A provider gateway must not be constructed (args={args}, kwargs={kwargs})"
        )

    monkeypatch.setattr(api, "OpenAICompatibleGateway", must_not_construct)
    monkeypatch.setattr(api, "AnthropicGateway", must_not_construct)


def _forbid_analysis(monkeypatch):
    """Requests rejected during gateway selection must never reach analysis."""

    def must_not_run(*args, **kwargs):
        raise AssertionError("analysis must not run when demo config is incomplete")

    monkeypatch.setattr(api, "analyze_report", must_not_run)


def _set_demo_env_without(monkeypatch, var: str, blank_value: str | None):
    """Demo env with one var either unset (None) or set to an empty string."""
    _set_demo_env(monkeypatch)
    if blank_value is None:
        monkeypatch.delenv(var)
    else:
        monkeypatch.setenv(var, blank_value)


def _request_from_ip(ip: str, forwarded_for: str | None = None):
    headers = {} if forwarded_for is None else {"x-forwarded-for": forwarded_for}
    return SimpleNamespace(client=SimpleNamespace(host=ip), headers=headers)


@pytest.fixture(autouse=True)
def reset_demo_quota():
    api._reset_demo_quota_for_tests()
    yield
    api._reset_demo_quota_for_tests()


def _assert_demo_not_configured(detail: str):
    assert "demo mode is not configured" in detail.lower()
    for var in DEMO_ENV:
        assert var in detail
    assert "api_key" in detail


def _create_native_bundle(tmp_path: Path) -> Path:
    report = json.loads(REPORT_TEXT)
    report.update(config={}, errors=[], stats={})
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(report))
    return create_bundle(report_path, tmp_path / "bundle.zip").output_path


def _create_non_native_bundle(tmp_path: Path) -> Path:
    bundle_path = tmp_path / "not-a-playwright-bundle.zip"
    with zipfile.ZipFile(bundle_path, "w") as archive:
        archive.writestr("not-a-report.json", '{"not": "a report"}')
    return bundle_path


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


def test_post_analyze_demo_allows_ten_requests_then_rejects_before_analysis(
    monkeypatch,
):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    captured = _capture_gateway(monkeypatch)

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})
        assert response.status_code == 200

    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 429
    assert "Demo request limit reached" in response.json()["detail"]
    assert len(captured) == api.DEMO_REQUESTS_PER_HOUR


def test_post_analyze_demo_quota_rejects_before_constructing_gateway(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(_request_from_ip("testclient"))

    _forbid_real_gateway(monkeypatch)
    _forbid_analysis(monkeypatch)
    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 429


def test_demo_quota_uses_separate_client_ip_buckets(monkeypatch):
    _set_demo_env(monkeypatch)
    captured = _capture_gateway(monkeypatch)

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(_request_from_ip("198.51.100.10"))

    api._consume_demo_quota(_request_from_ip("198.51.100.11"))

    assert captured == []


def test_demo_quota_resets_after_one_hour(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: now[0])

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(_request_from_ip("198.51.100.10"))

    now[0] += api.DEMO_WINDOW_SECONDS
    api._consume_demo_quota(_request_from_ip("198.51.100.10"))


def test_demo_quota_prunes_expired_ips_when_another_client_arrives(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(api.time, "monotonic", lambda: now[0])
    api._consume_demo_quota(_request_from_ip("198.51.100.10"))

    now[0] += api.DEMO_WINDOW_SECONDS
    api._consume_demo_quota(_request_from_ip("198.51.100.11"))

    assert set(api._demo_quota) == {"198.51.100.11"}


def test_demo_quota_ignores_forwarded_header_from_untrusted_peer(monkeypatch):
    monkeypatch.delenv("TRUSTED_PROXY_IPS", raising=False)

    for spoofed in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(
            _request_from_ip("203.0.113.5", forwarded_for=f"198.51.100.{spoofed}")
        )

    with pytest.raises(HTTPException) as rejected:
        api._consume_demo_quota(
            _request_from_ip("203.0.113.5", forwarded_for="198.51.100.99")
        )

    assert rejected.value.status_code == 429
    assert set(api._demo_quota) == {"203.0.113.5"}


def test_demo_quota_uses_forwarded_client_from_trusted_proxy(monkeypatch):
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "10.0.0.7")

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(
            _request_from_ip("10.0.0.7", forwarded_for="198.51.100.10, 10.0.0.7")
        )

    api._consume_demo_quota(
        _request_from_ip("10.0.0.7", forwarded_for="198.51.100.11")
    )

    assert set(api._demo_quota) == {"198.51.100.10", "198.51.100.11"}


def test_demo_quota_rejects_new_identity_when_bucket_capacity_is_reached(monkeypatch):
    monkeypatch.setattr(api, "MAX_DEMO_QUOTA_BUCKETS", 2)
    first = _request_from_ip("198.51.100.10")
    api._consume_demo_quota(first)
    api._consume_demo_quota(_request_from_ip("198.51.100.11"))
    api._consume_demo_quota(first)

    with pytest.raises(HTTPException) as rejected:
        api._consume_demo_quota(_request_from_ip("198.51.100.12"))

    assert rejected.value.status_code == 429
    assert set(api._demo_quota) == {"198.51.100.10", "198.51.100.11"}


def test_post_analyze_bundle_demo_quota_rejects_before_gateway_and_analysis(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)

    for _ in range(api.DEMO_REQUESTS_PER_HOUR):
        api._consume_demo_quota(_request_from_ip("testclient"))

    _forbid_real_gateway(monkeypatch)
    _forbid_analysis(monkeypatch)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"demo": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 429


@pytest.mark.parametrize(
    "fields",
    [
        {"demo": "true"},
        {
            "provider": "openai",
            "api_key": "byok-key",
            "model": "test-model",
        },
    ],
)
def test_non_native_bundle_is_rejected_before_quota_or_gateway(
    tmp_path, monkeypatch, fields
):
    client = TestClient(app)
    bundle_path = _create_non_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)
    _forbid_real_gateway(monkeypatch)
    _forbid_analysis(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data=fields,
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    assert api._demo_quota == {}


def test_fake_and_byok_requests_do_not_consume_demo_quota(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    captured = _capture_gateway(monkeypatch)

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": True, "demo": True},
    )
    assert response.status_code == 200

    class CapturingGateway(FakeGateway):
        def __init__(self, **kwargs):
            super().__init__()
            captured.append(self)

    monkeypatch.setattr(api, "OpenAICompatibleGateway", CapturingGateway)
    response = client.post(
        "/analyze",
        json={
            "report": REPORT_TEXT,
            "demo": True,
            "provider": "openai",
            "api_key": "byok-key",
            "model": "test-model",
        },
    )
    assert response.status_code == 200
    assert api._demo_quota == {}


def test_post_analyze_demo_without_server_config_returns_422(monkeypatch):
    client = TestClient(app)
    _clear_demo_env(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 422
    _assert_demo_not_configured(response.json()["detail"])


# Demo mode is a public, server-funded path, so it demands a complete and
# non-empty LLM_* trio. Removing OR blanking any single variable must be
# refused — including LLM_API_KEY alone, which the gateway itself tolerates
# (empty key => "unused") for no-auth LAN endpoints.
@pytest.mark.parametrize("missing", sorted(DEMO_ENV))
@pytest.mark.parametrize("blank_value", [None, "", "   "])
def test_post_analyze_demo_requires_every_server_var(monkeypatch, missing, blank_value):
    client = TestClient(app)
    _set_demo_env_without(monkeypatch, missing, blank_value)
    _forbid_analysis(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, "demo": True})

    assert response.status_code == 422
    _assert_demo_not_configured(response.json()["detail"])


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
            "provider": "openai",
            "api_key": "byok-key",
            "model": "byok-model",
        },
    )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.base_url, g.model) for g in gateways] == [
        ("byok-key", "https://api.openai.com/v1", "byok-model")
    ]


@pytest.mark.parametrize(
    ("provider", "base_url"),
    [
        ("openai", "https://api.openai.com/v1"),
        ("openrouter", "https://openrouter.ai/api/v1"),
    ],
)
def test_post_analyze_byok_uses_fixed_openai_compatible_provider_url(
    monkeypatch, provider, base_url
):
    client = TestClient(app)
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
            "provider": provider,
            "api_key": "byok-key",
            "model": "frontier-model",
        },
    )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.base_url, g.model) for g in gateways] == [
        ("byok-key", base_url, "frontier-model")
    ]


def test_post_analyze_byok_uses_direct_anthropic_gateway(monkeypatch):
    client = TestClient(app)
    gateways = []

    class CapturingGateway(FakeGateway):
        def __init__(self, *, api_key=None, model=None):
            super().__init__()
            self.api_key = api_key
            self.model = model
            gateways.append(self)

    monkeypatch.setattr(api, "AnthropicGateway", CapturingGateway)
    response = client.post(
        "/analyze",
        json={
            "report": REPORT_TEXT,
            "provider": "anthropic",
            "api_key": "anthropic-key",
            "model": "claude-sonnet-4-5",
        },
    )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.model) for g in gateways] == [
        ("anthropic-key", "claude-sonnet-4-5")
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key": "byok-key", "model": "model"},
        {"provider": "unknown", "api_key": "byok-key", "model": "model"},
        {
            "provider": "openai",
            "api_key": "byok-key",
            "model": "model",
            "base_url": "http://169.254.169.254/latest",
        },
    ],
)
def test_post_analyze_rejects_unsafe_byok_provider_configuration(monkeypatch, fields):
    client = TestClient(app)
    _forbid_provider_gateways(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, **fields})

    assert response.status_code == 422


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key": "byok-key"},
        {"api_key": "byok-key", "base_url": "https://llm.example/v1"},
        {"api_key": "byok-key", "model": "byok-model"},
        {
            "api_key": "byok-key",
            "base_url": " ",
            "model": "byok-model",
        },
        {"base_url": "https://llm.example/v1", "demo": True},
        {"model": "byok-model", "demo": True},
        {"api_key": "", "demo": True},
        {
            "api_key": "",
            "base_url": "https://llm.example/v1",
            "model": "byok-model",
            "demo": True,
        },
    ],
)
def test_post_analyze_rejects_partial_byok_before_constructing_gateway(
    monkeypatch, fields
):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    _forbid_real_gateway(monkeypatch)

    response = client.post("/analyze", json={"report": REPORT_TEXT, **fields})

    assert response.status_code == 422
    assert (
        "BYOK requires request-level" in response.json()["detail"]
        or "Custom base_url values" in response.json()["detail"]
    )


def test_post_analyze_fake_takes_precedence_over_byok_and_demo(monkeypatch):
    client = TestClient(app)
    _set_demo_env(monkeypatch)
    # Neither the BYOK branch nor the demo branch may build a real gateway.
    _forbid_real_gateway(monkeypatch)

    response = client.post(
        "/analyze",
        json={
            "report": REPORT_TEXT,
            "fake": True,
            "demo": True,
            "provider": "openai",
            "api_key": "byok-key",
            "model": "byok-model",
        },
    )

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert "FAKE:" in data[0]["summary"]


def test_post_analyze_fake_works_without_any_server_config(monkeypatch):
    client = TestClient(app)
    _clear_demo_env(monkeypatch)  # fake must work fully offline

    response = client.post(
        "/analyze",
        json={"report": REPORT_TEXT, "fake": True},
    )

    assert response.status_code == 200, response.text
    assert "FAKE:" in response.json()[0]["summary"]


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


@pytest.mark.parametrize("missing", sorted(DEMO_ENV))
@pytest.mark.parametrize("blank_value", [None, "", "   "])
def test_post_analyze_bundle_demo_requires_every_server_var_and_deletes_temp_file(
    tmp_path, monkeypatch, missing, blank_value
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env_without(monkeypatch, missing, blank_value)
    created = _capture_temp_uploads(monkeypatch, tmp_path)
    _forbid_analysis(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"demo": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    _assert_demo_not_configured(response.json()["detail"])
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
            "provider": "openai",
            "api_key": "byok-key",
            "model": "byok-model",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.base_url, g.model) for g in gateways] == [
        ("byok-key", "https://api.openai.com/v1", "byok-model")
    ]


def test_post_analyze_bundle_byok_uses_direct_anthropic_gateway(tmp_path, monkeypatch):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    gateways = []

    class CapturingGateway(FakeGateway):
        def __init__(self, *, api_key=None, model=None):
            super().__init__()
            self.api_key = api_key
            self.model = model
            gateways.append(self)

    monkeypatch.setattr(api, "AnthropicGateway", CapturingGateway)
    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={
                "provider": "anthropic",
                "api_key": "anthropic-key",
                "model": "claude-sonnet-4-5",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert [(g.api_key, g.model) for g in gateways] == [
        ("anthropic-key", "claude-sonnet-4-5")
    ]


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key": "byok-key", "model": "model"},
        {"provider": "unknown", "api_key": "byok-key", "model": "model"},
        {
            "provider": "openrouter",
            "api_key": "byok-key",
            "model": "model",
            "base_url": "http://127.0.0.1:9/v1",
        },
    ],
)
def test_post_analyze_bundle_rejects_unsafe_byok_provider_configuration(
    tmp_path, monkeypatch, fields
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _forbid_provider_gateways(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data=fields,
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422


@pytest.mark.parametrize(
    "fields",
    [
        {"api_key": "byok-key"},
        {"api_key": "byok-key", "base_url": "https://llm.example/v1"},
        {"api_key": "byok-key", "model": "byok-model"},
        {
            "api_key": "byok-key",
            "base_url": "https://llm.example/v1",
            "model": " ",
        },
        {"base_url": "https://llm.example/v1", "demo": "true"},
        {"model": "byok-model", "demo": "true"},
        {"api_key": "", "demo": "true"},
        {
            "api_key": "",
            "base_url": "https://llm.example/v1",
            "model": "byok-model",
            "demo": "true",
        },
    ],
)
def test_post_analyze_bundle_rejects_partial_byok_before_constructing_gateway(
    tmp_path, monkeypatch, fields
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)
    _forbid_real_gateway(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data=fields,
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 422
    assert (
        "BYOK requires request-level" in response.json()["detail"]
        or "Custom base_url values" in response.json()["detail"]
    )


def test_post_analyze_bundle_fake_takes_precedence_over_byok_and_demo(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _set_demo_env(monkeypatch)
    # Neither the BYOK branch nor the demo branch may build a real gateway.
    _forbid_real_gateway(monkeypatch)

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
        data={
            "fake": "true",
            "demo": "true",
            "provider": "openai",
            "api_key": "byok-key",
            "model": "byok-model",
            },
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    data = response.json()
    assert len(data) == 1
    assert "FAKE:" in data[0]["summary"]


def test_post_analyze_bundle_fake_works_without_any_server_config(
    tmp_path, monkeypatch
):
    client = TestClient(app)
    bundle_path = _create_native_bundle(tmp_path)
    _clear_demo_env(monkeypatch)  # fake must work fully offline

    with bundle_path.open("rb") as bundle:
        response = client.post(
            "/analyze-bundle",
            data={"fake": "true"},
            files={"bundle": ("bundle.zip", bundle, "application/zip")},
        )

    assert response.status_code == 200, response.text
    assert "FAKE:" in response.json()[0]["summary"]
