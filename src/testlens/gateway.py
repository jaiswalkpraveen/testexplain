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


class FakeGateway:
    """Deterministic test double. No network, no API key.

    Returns a canned response and records every prompt it was given so tests
    can assert what was sent.
    """

    def __init__(self, response: str = "FAKE: test failed because of a timeout.") -> None:
        self.response = response
        self.calls: list[str] = []

    def generate(self, prompt: str) -> str:
        self.calls.append(prompt)
        return self.response


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
