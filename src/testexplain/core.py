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
from testexplain.assembly.assembler import (
    EVIDENCE_SECTIONS,
    SECTION_BY_SOURCE,
    AssembledEvidence,
    assemble_evidence,
)
from testexplain.assembly.budget import DEFAULT_TOTAL_TOKEN_BUDGET
from testexplain.ingestion.input_reader import LoadedInput, load_input
from testexplain.models import (
    Category,
    Evidence,
    FailedAttempt,
    FailureAnalysis,
    FailureContext,
)
from testexplain.sources.common import SourceResult
from testexplain.sources.har import read_har
from testexplain.sources.trace import read_trace_actions

# Derive the category list from the model's Literal type, so the prompt
# and the validator can never drift apart (single source of truth).
_CATEGORY_LIST = ", ".join(f'"{c}"' for c in get_args(Category))


def build_prompt(
    failure: FailureContext,
    *,
    evidence: AssembledEvidence | None = None,
    project: str = "",
    retry: int = 0,
    flaky: bool = False,
) -> str:
    """Turn one failed test into prompt text that demands structured JSON.

    Pure string building -- no LLM call -- so it is trivially testable.
    The instruction section mirrors the FailureAnalysis model: this is the
    contract we later validate the response against.

    ``evidence`` is already redacted, ranked, and budgeted by the assembler,
    so this function only phrases it: prompt code never re-reads artifacts.
    """
    return f"""A Playwright end-to-end test failed. Here is what we know:

Test: {failure.test_title}
File: {failure.file}
Status: {failure.status}
Project: {project or "(not reported)"}
Retry: {retry}
Eventually passed (flaky): {flaky}
Error message: {failure.error_message}
Stack trace:
{failure.error_stack}

{_format_evidence(evidence)}

Analyze why this test likely failed. Respond with ONLY a JSON object --
no markdown fences, no text before or after it -- with exactly these keys:

- "summary": a 1-2 sentence plain-English explanation of the failure
- "suspected_category": exactly one of {_CATEGORY_LIST}
- "evidence": a list of short quotes from the error output that support
  your verdict (empty list if none)
- "next_steps": a list of concrete actions the engineer should take,
  most useful first (empty list if none)
- "confidence": a number between 0.0 and 1.0 for how sure you are"""


SECTION_TITLES = {
    "actions": "Actions",
    "console_output": "Console/output",
    "network": "Network",
}


def _format_evidence(assembled: AssembledEvidence | None) -> str:
    """Phrase already-safe evidence in labelled sections the model can cite.

    Sections are always present, including empty ones: "none" is itself a fact
    about the run, and a stable layout stops the model from inferring meaning
    from a section's absence.
    """
    if assembled is None:
        return "Evidence from artifacts: none collected."

    grouped: dict[str, list[str]] = {section: [] for section in EVIDENCE_SECTIONS}
    for item in assembled.evidence:
        grouped[SECTION_BY_SOURCE[item.source]].append(
            f"- [{item.provenance}] {item.summary}"
        )

    lines = ["Evidence from artifacts (already redacted and truncated to a budget):"]
    for section in EVIDENCE_SECTIONS:
        lines.append(f"[{SECTION_TITLES[section]}]")
        lines.extend(grouped[section] or ["- none"])

    truncation = assembled.truncation
    notes = list(assembled.warnings)
    if truncation.truncated_count:
        notes.append(f"{truncation.truncated_count} evidence item(s) truncated")
    if truncation.omitted_count:
        notes.append(f"{truncation.omitted_count} evidence item(s) omitted for budget")
    if truncation.deduplicated_count:
        notes.append(
            f"{truncation.deduplicated_count} duplicate network item(s) collapsed"
        )
    if notes:
        lines.append("[Evidence gaps]")
        lines.extend(f"- {note}" for note in notes)
    return "\n".join(lines)


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


def _report_evidence(attempt: FailedAttempt) -> list[Evidence]:
    """Normalize report stdout/stderr before they enter the assembly boundary."""
    return [
        Evidence(source=source, provenance="report", summary=value, severity=1)
        for source, values in (("stdout", attempt.stdout), ("stderr", attempt.stderr))
        for value in values
    ]


def _read_attachment(path: Path, name: str, content_type: str) -> SourceResult | None:
    """Dispatch only from the report's attachment metadata."""
    label = f"{name} {content_type}".lower()
    if "trace" in label:
        return read_trace_actions(path)
    if "har" in label:
        return read_har(path)
    return None


def _assemble_attempt_evidence(
    loaded: LoadedInput,
    attempt: FailedAttempt,
    total_token_budget: int,
    base_warnings: list[str],
) -> AssembledEvidence:
    items = _report_evidence(attempt)
    warnings = [*base_warnings, *attempt.warnings]
    for attachment in attempt.attachments:
        warning_count = len(loaded.warnings)
        path = loaded.resolve_attachment_path(attachment)
        warnings.extend(loaded.warnings[warning_count:])
        if path is None:
            if attachment.body_b64 is not None:
                warnings.append(
                    f"Attachment {attachment.name!r} inline bodies are unsupported"
                )
            continue
        result = _read_attachment(path, attachment.name, attachment.content_type)
        if result is None:
            warnings.append(f"Attachment {attachment.name!r} has an unsupported type")
            continue
        items.extend(result.evidence)
        warnings.extend(result.warnings)
    return assemble_evidence(items, warnings=warnings, total_budget=total_token_budget)


def analyze_input(
    path: str | Path,
    gateway: Gateway,
    *,
    total_token_budget: int = DEFAULT_TOTAL_TOKEN_BUDGET,
) -> list[FailureAnalysis]:
    """Analyze every unexpected failed attempt in a report or safe bundle.

    The report owns correlation: only attachments listed by the current result
    are read. One broken artifact becomes a prompt warning, not a lost attempt.
    """
    analyses: list[FailureAnalysis] = []
    with load_input(path) as loaded:
        base_warnings = loaded.warnings[:]
        for attempt in loaded.attempts:
            failure = FailureContext(
                test_title=attempt.title_path[-1] if attempt.title_path else "",
                file=attempt.file,
                status=attempt.status,
                error_message=attempt.error_message,
                error_stack=attempt.error_stack,
                duration_ms=int(attempt.duration_ms),
            )
            assembled = _assemble_attempt_evidence(
                loaded, attempt, total_token_budget, base_warnings
            )
            prompt = build_prompt(
                failure,
                evidence=assembled,
                project=attempt.project_name,
                retry=attempt.retry,
                flaky=attempt.eventually_passed,
            )
            analysis = generate_analysis(
                gateway, prompt, test_title=failure.test_title
            )
            analyses.append(
                analysis.model_copy(
                    update={
                        "project": attempt.project_name,
                        "retry": attempt.retry,
                        "flaky": attempt.eventually_passed,
                    }
                )
            )
    return analyses


def analyze_report(path: str | Path, gateway: Gateway) -> list[FailureAnalysis]:
    """Compatibility wrapper for the original report-only entry point."""
    return analyze_input(path, gateway)
