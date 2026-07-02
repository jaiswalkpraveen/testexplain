"""Core analysis pipeline.

This is the "reasoning" layer. It turns each failed test into a prompt,
sends it through the gateway (any Gateway-shaped object), and wraps the
answer as a FailureAnalysis. It depends only on the Gateway *shape* --
never on a concrete provider -- so tests pass FakeGateway and production
passes AnthropicGateway with this code unchanged.
"""

from pathlib import Path

from testlens.gateway import Gateway
from testlens.ingestion.playwright import parse_report
from testlens.models import FailureAnalysis, FailureContext


def build_prompt(failure: FailureContext) -> str:
    """Turn one failed test into plain-English prompt text for the LLM.

    Pure string building -- no LLM call -- so it is trivially testable.
    """
    return f"""A Playwright end-to-end test failed. Here is what we know:

Test: {failure.test_title}
File: {failure.file}
Status: {failure.status}
Error message: {failure.error_message}
Stack trace:
{failure.error_stack}

Explain in plain English why this test likely failed and what the engineer
should check first."""


def analyze_report(path: str | Path, gateway: Gateway) -> list[FailureAnalysis]:
    """Run the full M0 pipeline: parse -> prompt -> generate -> wrap.

    ``gateway`` is typed as the Gateway Protocol, so this function never
    mentions FakeGateway or AnthropicGateway -- that is the seam payoff.
    """
    failures = parse_report(path)

    analyses: list[FailureAnalysis] = []
    for failure in failures:
        prompt = build_prompt(failure)
        explanation = gateway.generate(prompt)
        analyses.append(
            FailureAnalysis(
                test_title=failure.test_title,
                explanation=explanation,
            )
        )
    return analyses
