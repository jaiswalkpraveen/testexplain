"""Core analysis pipeline.

This is the "reasoning" layer. It turns each failed test into a prompt,
sends it through the gateway (any Gateway-shaped object), and wraps the
answer as a FailureAnalysis. It depends only on the Gateway *shape* --
never on a concrete provider -- so tests pass FakeGateway and production
passes AnthropicGateway with this code unchanged.
"""

from pathlib import Path
from typing import get_args

from testlens.gateway import Gateway
from testlens.ingestion.playwright import parse_report
from testlens.models import Category, FailureAnalysis, FailureContext

# Derive the category list from the model's Literal type, so the prompt
# and the validator can never drift apart (single source of truth).
_CATEGORY_LIST = ", ".join(f'"{c}"' for c in get_args(Category))


def build_prompt(failure: FailureContext) -> str:
    """Turn one failed test into prompt text that demands structured JSON.

    Pure string building -- no LLM call -- so it is trivially testable.
    The instruction section mirrors the FailureAnalysis model: this is the
    contract we later validate the response against.
    """
    return f"""A Playwright end-to-end test failed. Here is what we know:

Test: {failure.test_title}
File: {failure.file}
Status: {failure.status}
Error message: {failure.error_message}
Stack trace:
{failure.error_stack}

Analyze why this test likely failed. Respond with ONLY a JSON object --
no markdown fences, no text before or after it -- with exactly these keys:

- "summary": a 1-2 sentence plain-English explanation of the failure
- "suspected_category": exactly one of {_CATEGORY_LIST}
- "evidence": a list of short quotes from the error output that support
  your verdict (empty list if none)
- "next_steps": a list of concrete actions the engineer should take,
  most useful first (empty list if none)
- "confidence": a number between 0.0 and 1.0 for how sure you are"""


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
