import json
from pathlib import Path
from typing import get_args

import pytest

from testexplain.core import analyze_input
from testexplain.core import (
analyze_report,
build_prompt,
generate_analysis,
parse_analysis,
)
from testexplain.assembly.assembler import AssembledEvidence, TruncationInfo
from testexplain.gateway import FakeGateway
from testexplain.models import Category, Evidence, FailureAnalysis, FailureContext
from testexplain.sources.common import SourceResult

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


def test_build_prompt_groups_evidence_and_reports_budget_gaps():
    assembled = AssembledEvidence(
        evidence=[
            Evidence(source="action", provenance="trace", summary="click checkout"),
            Evidence(source="stdout", provenance="report", summary="started"),
            Evidence(source="network", provenance="har", summary="503 on /checkout"),
        ],
        warnings=["trace was partially unreadable"],
        truncation=TruncationInfo(
            total_budget=10,
            used_tokens=10,
            section_tokens={"actions": 3, "console_output": 2, "network": 5},
            omitted_count=2,
            truncated_count=1,
            deduplicated_count=1,
        ),
    )

    prompt = build_prompt(make_failure(), evidence=assembled)

    for section in ("[Actions]", "[Console/output]", "[Network]"):
        assert section in prompt
    assert "[trace]" in prompt
    assert "[report]" in prompt
    assert "[har]" in prompt
    assert "trace was partially unreadable" in prompt
    assert "truncated" in prompt
    assert "omitted for budget" in prompt
    assert "duplicate network" in prompt


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


def test_generate_analysis_returns_result_on_first_good_reply():
    gateway = FakeGateway(response=VALID_JSON_REPLY)

    analysis = generate_analysis(gateway, "the prompt", test_title="t")

    assert analysis.suspected_category == "api outage"
    # Happy path must cost exactly ONE llm call -- no wasted retries.
    assert len(gateway.calls) == 1


def test_generate_analysis_recovers_after_one_bad_reply():
    gateway = FakeGateway(responses=["not json at all", VALID_JSON_REPLY])

    analysis = generate_analysis(gateway, "the prompt", test_title="t")

    assert analysis.suspected_category == "api outage"
    assert len(gateway.calls) == 2


def test_generate_analysis_repair_prompt_mentions_the_error():
    gateway = FakeGateway(responses=["not json at all", VALID_JSON_REPLY])

    generate_analysis(gateway, "the prompt", test_title="t")

    repair_prompt = gateway.calls[1]
    # The second prompt must tell the LLM its reply was invalid and why,
    # and repeat the original task so it can actually retry.
    assert "invalid" in repair_prompt.lower()
    assert "JSON" in repair_prompt
    assert "the prompt" in repair_prompt


def test_generate_analysis_gives_up_after_max_attempts():
    gateway = FakeGateway(responses=["garbage"])  # garbage forever

    with pytest.raises(ValueError):
        generate_analysis(gateway, "the prompt", test_title="t", max_attempts=3)

    assert len(gateway.calls) == 3


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


def _write_attempt_report(path: Path, *, attachments=None, status="flaky") -> Path:
    report = {
        "config": {},
        "errors": [],
        "stats": {},
        "suites": [
            {
                "title": "checkout.spec.ts",
                "specs": [
                    {
                        "title": "user checks out",
                        "id": "checkout-1",
                        "tests": [
                            {
                                "projectId": "chromium-id",
                                "projectName": "chromium",
                                "status": status,
                                "results": [
                                    {
                                        "retry": 1,
                                        "status": "failed",
                                        "duration": 1234,
                                        "startTime": "2026-08-15T10:00:00Z",
                                        "error": {
                                            "message": "checkout timed out",
                                            "stack": "TimeoutError: checkout timed out",
                                        },
                                        "stdout": ["checkout started"],
                                        "attachments": attachments or [],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        ],
    }
    path.write_text(json.dumps(report))
    return path


def test_analyze_input_includes_evidence_and_known_attempt_identity(tmp_path, monkeypatch):
    report = _write_attempt_report(
        tmp_path / "report.json",
        attachments=[{"name": "run.trace.zip", "path": "run.trace.zip"}],
    )
    (tmp_path / "run.trace.zip").write_bytes(b"trace")
    evidence = Evidence(
        source="network",
        provenance="trace",
        summary="POST https://app.test/checkout | status=503 Service Unavailable",
        severity=4,
        timestamp_ms=1200,
    )
    monkeypatch.setattr("testexplain.core.read_trace_actions", lambda path: SourceResult([evidence], []))
    gateway = FakeGateway(response=VALID_JSON_REPLY)
    results = analyze_input(report, gateway)

    assert len(results) == 1
    assert results[0].project == "chromium"
    assert results[0].retry == 1
    assert results[0].flaky is True
    assert "POST https://app.test/checkout" in gateway.calls[0]
    assert "Network" in gateway.calls[0]
    assert "[trace]" in gateway.calls[0]


def test_analyze_input_keeps_report_evidence_and_artifact_warnings(tmp_path):
    report = _write_attempt_report(
        tmp_path / "report.json",
        attachments=[{"name": "missing.trace.zip", "path": "missing.trace.zip"}],
        status="unexpected",
    )
    gateway = FakeGateway(response=VALID_JSON_REPLY)

    results = analyze_input(report, gateway)

    assert len(results) == 1
    assert "checkout started" in gateway.calls[0]
    assert "does not exist" in gateway.calls[0]


def test_analyze_input_redacts_report_evidence_before_gateway(tmp_path):
    report = _write_attempt_report(tmp_path / "report.json")
    data = json.loads(report.read_text())
    data["suites"][0]["specs"][0]["tests"][0]["results"][0]["stdout"] = [
        "Authorization: Bearer secret-token"
    ]
    report.write_text(json.dumps(data))
    gateway = FakeGateway(response=VALID_JSON_REPLY)

    analyze_input(report, gateway)

    assert "secret-token" not in gateway.calls[0]
    assert "<redacted>" in gateway.calls[0]


def test_analyze_input_generates_one_analysis_per_failed_attempt(tmp_path):
    report = _write_attempt_report(tmp_path / "report.json")
    data = json.loads(report.read_text())
    results = data["suites"][0]["specs"][0]["tests"][0]["results"]
    results.append(
        {
            "retry": 2,
            "status": "timedOut",
            "duration": 2000,
            "startTime": "2026-08-15T10:01:00Z",
            "error": {"message": "checkout still timed out"},
        }
    )
    report.write_text(json.dumps(data))
    gateway = FakeGateway(response=VALID_JSON_REPLY)

    analyses = analyze_input(report, gateway)

    assert len(analyses) == 2
    assert len(gateway.calls) == 2
    assert "Retry: 1" in gateway.calls[0]
    assert "Retry: 2" in gateway.calls[1]
