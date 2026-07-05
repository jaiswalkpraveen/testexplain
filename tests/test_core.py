from pathlib import Path
from typing import get_args

import pytest

from testlens.core import analyze_report, build_prompt, parse_analysis
from testlens.gateway import FakeGateway
from testlens.models import Category, FailureAnalysis, FailureContext

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"

# A well-formed LLM reply, used across the parsing tests.
VALID_JSON_REPLY = """{
  "summary": "The login API returned 503, so the page never loaded.",
  "suspected_category": "api outage",
  "evidence": ["503 Service Unavailable"],
  "next_steps": ["Check auth-service health"],
  "confidence": 0.8
}"""


def make_failure() -> FailureContext:
    return FailureContext(
        test_title="user sees dashboard after login",
        file="tests/auth.spec.ts",
        status="failed",
        error_message="Timeout 30000ms exceeded",
        error_stack="TimeoutError: Timeout 30000ms exceeded",
    )


def test_build_prompt_includes_failure_details():
    prompt = build_prompt(make_failure())

    # The prompt must carry the evidence the LLM needs to reason.
    assert "user sees dashboard after login" in prompt
    assert "tests/auth.spec.ts" in prompt
    assert "Timeout 30000ms exceeded" in prompt


def test_build_prompt_lists_every_allowed_category():
    prompt = build_prompt(make_failure())

    # The LLM can only pick a valid category if the prompt names them all.
    # get_args(Category) reads the values off the Literal type, so this
    # test updates itself when we add a category to the model.
    for category in get_args(Category):
        assert category in prompt


def test_build_prompt_demands_json_only_response():
    prompt = build_prompt(make_failure())

    # The contract: respond with JSON, nothing else.
    assert "JSON" in prompt
    for field in ("summary", "suspected_category", "evidence",
                  "next_steps", "confidence"):
        assert f'"{field}"' in prompt


def test_parse_analysis_accepts_clean_json():
    analysis = parse_analysis(VALID_JSON_REPLY, test_title="login test")

    assert analysis.test_title == "login test"
    assert analysis.suspected_category == "api outage"
    assert analysis.confidence == 0.8


def test_parse_analysis_strips_markdown_fences():
    # LLMs often wrap JSON in ```json ... ``` despite being told not to.
    fenced = f"```json\n{VALID_JSON_REPLY}\n```"

    analysis = parse_analysis(fenced, test_title="login test")

    assert analysis.suspected_category == "api outage"


def test_parse_analysis_rejects_non_json():
    with pytest.raises(ValueError):
        parse_analysis("Sorry, I cannot help with that.", test_title="t")


def test_parse_analysis_rejects_json_violating_schema():
    bad = VALID_JSON_REPLY.replace('"api outage"', '"network issue"')

    with pytest.raises(ValueError):
        parse_analysis(bad, test_title="t")


def test_analyze_report_runs_pipeline_with_fake_gateway():
    gateway = FakeGateway(response=VALID_JSON_REPLY)

    results = analyze_report(FIXTURE, gateway)

    # Fixture has exactly one failing test.
    assert len(results) == 1
    assert isinstance(results[0], FailureAnalysis)
    assert results[0].test_title == "user sees dashboard after login"
    assert results[0].suspected_category == "api outage"
    assert results[0].confidence == 0.8

    # The gateway was actually called once, with a prompt containing evidence.
    assert len(gateway.calls) == 1
    assert "Timeout 30000ms exceeded" in gateway.calls[0]
