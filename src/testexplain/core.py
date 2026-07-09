"""Core analysis pipeline.

This is the "reasoning" layer. It turns each failed test into a prompt,
sends it through the gateway (any Gateway-shaped object), and wraps the
answer as a FailureAnalysis. It depends only on the Gateway *shape* --
never on a concrete provider -- so tests pass FakeGateway and production
passes AnthropicGateway with this code unchanged.
"""

import json
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from testexplain.gateway import Gateway
from testexplain.ingestion.playwright import parse_report
from testexplain.models import Category, FailureAnalysis, FailureContext

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


def _strip_markdown_fences(text: str) -> str:
    """Remove a ```json ... ``` (or plain ```) wrapper if present.

    We told the LLM not to add fences, but instructions reduce misbehavior,
    they don't eliminate it -- so we clean up defensively.
    """
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.index("\n")  # end of the ```json line
        text = text[first_newline + 1 :]  # drop the opening fence line
        text = text.removesuffix("```").strip()
    return text


def parse_analysis(raw: str, test_title: str) -> FailureAnalysis:
    """Turn the LLM's raw reply into a validated FailureAnalysis.

    Two failure modes, one exception type (ValueError) for callers:
    - the reply is not JSON at all (prose, apology, truncation)
    - the reply is JSON but violates the schema (bad category, confidence
      out of range, missing keys)

    ``test_title`` is injected by US, not asked from the LLM -- never ask
    the model for facts you already have.
    """
    cleaned = _strip_markdown_fences(raw)

    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM reply is not valid JSON: {exc}") from exc

    data["test_title"] = test_title

    try:
        return FailureAnalysis(**data)
    except ValidationError as exc:
        raise ValueError(f"LLM reply violates the schema: {exc}") from exc


def generate_analysis(
    gateway: Gateway,
    prompt: str,
    test_title: str,
    max_attempts: int = 3,
) -> FailureAnalysis:
    """Ask the LLM for an analysis, retrying with feedback on bad replies.

    The self-correction loop: unlike a flaky HTTP service, an LLM can FIX
    its mistake if you tell it what was wrong. On a bad reply we re-prompt
    with the exact validation error plus the original task, up to
    ``max_attempts`` total calls. If every attempt fails, re-raise the
    last error -- give up loudly, never silently.
    """
    last_error: ValueError | None = None
    current_prompt = prompt

    for _ in range(max_attempts):
        raw = gateway.generate(current_prompt)
        try:
            return parse_analysis(raw, test_title=test_title)
        except ValueError as exc:
            last_error = exc
            current_prompt = (
                f"Your previous reply was invalid: {exc}\n\n"
                f"Original task:\n{prompt}\n\n"
                "Respond again with ONLY the corrected JSON object."
            )

    raise ValueError(
        f"LLM failed to produce valid JSON after {max_attempts} attempts. "
        f"Last error: {last_error}"
    ) from last_error


def analyze_report(path: str | Path, gateway: Gateway) -> list[FailureAnalysis]:
    """Run the full pipeline: parse report -> prompt -> generate -> validate.

    ``gateway`` is typed as the Gateway Protocol, so this function never
    mentions FakeGateway or AnthropicGateway -- that is the seam payoff.
    """
    failures = parse_report(path)

    analyses: list[FailureAnalysis] = []
    for failure in failures:
        prompt = build_prompt(failure)
        analyses.append(
            generate_analysis(gateway, prompt, test_title=failure.test_title)
        )
    return analyses
