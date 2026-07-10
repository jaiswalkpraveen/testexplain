import pytest

from testexplain.gateway import FakeGateway, OpenAICompatibleGateway


def test_fake_gateway_returns_canned_response():
    gw = FakeGateway(response="boom")
    assert gw.generate("anything") == "boom"


def test_fake_gateway_records_calls():
    gw = FakeGateway()
    gw.generate("prompt A")
    gw.generate("prompt B")
    assert gw.calls == ["prompt A", "prompt B"]


def test_fake_gateway_plays_scripted_responses_in_order():
    # A scripted fake: reply differently on each call, like a real LLM
    # that misbehaves first and complies after a correction.
    gw = FakeGateway(responses=["garbage", "good JSON"])

    assert gw.generate("first") == "garbage"
    assert gw.generate("second") == "good JSON"


def test_fake_gateway_repeats_last_scripted_response_when_exhausted():
 # If the script runs out, keep returning the final reply instead of
 # crashing -- tests shouldn't fail because a loop called once more.
 gw = FakeGateway(responses=["only reply"])

 gw.generate("first")
 assert gw.generate("second") == "only reply"


def test_openai_compatible_gateway_reads_config_from_env(monkeypatch):
    # The gateway must pick up URL, key and model from the environment --
    # no hardcoded endpoints. No network call happens in __init__, so this
    # test is safe to run offline.
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.example.test:8000/v1")
    monkeypatch.setenv("LLM_API_KEY", "test-key-123")
    monkeypatch.setenv("LLM_MODEL", "dspark")

    gw = OpenAICompatibleGateway()

    assert gw.model == "dspark"
    assert str(gw.client.base_url) == "http://llm.example.test:8000/v1/"
    assert gw.client.api_key == "test-key-123"


def test_openai_compatible_gateway_allows_empty_api_key(monkeypatch):
    # Self-hosted LAN endpoints (e.g. an internal inference cluster) often
    # need no key. An *empty* LLM_API_KEY must still construct fine -- the
    # OpenAI client refuses empty strings, so the gateway substitutes a
    # placeholder (the endpoint ignores it anyway).
    monkeypatch.setenv("LLM_BASE_URL", "http://llm.example.test:8000/v1")
    monkeypatch.setenv("LLM_API_KEY", "")
    monkeypatch.setenv("LLM_MODEL", "dspark")

    gw = OpenAICompatibleGateway()

    assert gw.client.api_key == "unused"


def test_openai_compatible_gateway_fails_fast_when_url_missing(monkeypatch):
    # Fail at construction time with a clear KeyError, not later mid-request.
    monkeypatch.delenv("LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LLM_API_KEY", "test-key-123")
    monkeypatch.setenv("LLM_MODEL", "dspark")

    with pytest.raises(KeyError):
        OpenAICompatibleGateway()


def test_openai_compatible_gateway_accepts_explicit_params():
    gw = OpenAICompatibleGateway(
        model="gpt-4",
        api_key="sk-explicit",
        base_url="https://custom.example.com/v1",
    )

    assert gw.model == "gpt-4"
    assert gw.client.api_key == "sk-explicit"
    assert str(gw.client.base_url) == "https://custom.example.com/v1/"
