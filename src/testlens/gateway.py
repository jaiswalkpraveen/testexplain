"""LLM gateway seam.

All model calls in TestLens go through the ``Gateway`` shape. The core
analysis code depends only on ``.generate()`` -- never on a concrete provider.
That lets tests use ``FakeGateway`` (no network, no API key) while production
uses ``AnthropicGateway`` (real Claude), with the core code unchanged.
"""

from typing import Protocol


class Gateway(Protocol):
    """The contract every gateway must satisfy.

    This is a *shape*, not a class you instantiate. Anything with a matching
    ``generate(self, prompt: str) -> str`` method counts as a Gateway
    (structural / duck typing -- no ``implements`` needed).
    """

    def generate(self, prompt: str) -> str: ...


# Default canned reply: valid JSON matching the FailureAnalysis schema,
# because that's what a well-behaved LLM now returns. "FAKE:" marks it
# as fake in CLI/API output.
_FAKE_JSON_REPLY = """{
  "summary": "FAKE: test failed because of a timeout.",
  "suspected_category": "flaky",
  "evidence": ["Timeout 30000ms exceeded"],
  "next_steps": ["Re-run the test", "Check for slow API responses"],
  "confidence": 0.5
}"""


class FakeGateway:
    """Deterministic test double. No network, no API key.

    Two modes:
    - ``FakeGateway(response="...")`` -- same reply every call.
    - ``FakeGateway(responses=["a", "b"])`` -- scripted: reply "a" on the
      first call, "b" on the second. When the script is exhausted, the
      last reply repeats (so an extra call never crashes a test for a
      fake-internal reason).

    Records every prompt in ``calls`` so tests can assert what was sent.
    """

    def __init__(
        self,
        response: str = _FAKE_JSON_REPLY,
        responses: list[str] | None = None,
    ) -> None:
        self.responses = responses if responses is not None else [response]
        self.calls: list[str] = []

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        # Call 1 -> script[0], call 2 -> script[1], ... then repeat the last.
        index = min(len(self.calls) - 1, len(self.responses) - 1)
        return self.responses[index]


class OpenAICompatibleGateway:
    """Real gateway for any OpenAI-compatible endpoint.

    Works with LLM routers (LiteLLM, OpenRouter, employer proxies...) and
    self-hosted inference servers (vLLM, SGLang...) that expose the standard
    ``/v1/chat/completions`` API. All connection details come from the
    environment:

    - ``LLM_BASE_URL`` -- the endpoint URL, e.g. http://192.168.0.247:8000/v1
    - ``LLM_API_KEY`` -- the key for that endpoint. May be empty for
      no-auth LAN endpoints (the variable must still be set); the OpenAI
      client refuses empty strings, so a placeholder is sent instead.
    - ``LLM_MODEL`` -- default model name, e.g. dspark

    Imports ``openai`` lazily inside ``__init__`` (same reason as
    AnthropicGateway: FakeGateway tests must not require the package).
    Missing env vars raise ``KeyError`` at construction time -- fail fast,
    not mid-request.
    """

    def __init__(self, model: str | None = None) -> None:
        import os

        from openai import OpenAI

        self.model = model or os.environ["LLM_MODEL"]
        self.client = OpenAI(
            base_url=os.environ["LLM_BASE_URL"],
            # "or" kicks in when the env var is set but empty (no-auth
            # LAN endpoint): the client demands *some* string, the server
            # never checks it.
            api_key=os.environ["LLM_API_KEY"] or "unused",
        )

    def generate(self, prompt: str) -> str:
        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.choices[0].message.content


class AnthropicGateway:
    """Real gateway backed by Claude.

    Imports the ``anthropic`` package lazily inside ``__init__`` so that
    importing this module (e.g. for FakeGateway tests) never requires the
    package or an API key.
    """

    def __init__(self, model: str = "claude-3-5-sonnet-latest") -> None:
        import os

        from anthropic import Anthropic

        self.model = model
        self.client = Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def generate(self, prompt: str) -> str:
        message = self.client.messages.create(
            model=self.model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text
