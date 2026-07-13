"""Tests for the M1 and M2 data models.

M0's FailureAnalysis was just (test_title, explanation) -- a text blob.
M1 made it structured: category, evidence, next steps, confidence.
M2 adds normalized multi-source models: Evidence (one record shape for
trace/HAR/report facts), Attachment, and FailedAttempt (attempt-level
identity with deterministic attempt_key and flaky outcome), plus
additive UI-safe fields on FailureAnalysis.
These tests pin down each shape BEFORE we change models.py (TDD).
"""

import pytest
from pydantic import ValidationError

from testexplain.models import (
    Attachment,
    Evidence,
    FailedAttempt,
    FailureAnalysis,
    FailureContext,
)


def make_valid_kwargs() -> dict:
    """One place that knows what a fully-valid analysis looks like.

    Each test copies this and breaks exactly ONE thing, so when a test
    fails you know precisely which rule was violated.
    """
    return {
        "test_title": "login redirects to dashboard",
        "summary": "The login API returned 503, so the page never loaded.",
        "suspected_category": "api outage",
        "evidence": ["Error: connect ECONNREFUSED", "503 Service Unavailable"],
        "next_steps": ["Check auth-service health", "Look at deploy timeline"],
        "confidence": 0.8,
    }


def test_valid_analysis_is_accepted():
    analysis = FailureAnalysis(**make_valid_kwargs())

    assert analysis.suspected_category == "api outage"
    assert analysis.confidence == 0.8
    assert len(analysis.evidence) == 2


def test_invented_category_is_rejected():
    kwargs = make_valid_kwargs()
    kwargs["suspected_category"] = "network issue"  # not in the allowed list

    with pytest.raises(ValidationError):
        FailureAnalysis(**kwargs)


def test_confidence_above_one_is_rejected():
    kwargs = make_valid_kwargs()
    kwargs["confidence"] = 1.5

    with pytest.raises(ValidationError):
        FailureAnalysis(**kwargs)


def test_negative_confidence_is_rejected():
    kwargs = make_valid_kwargs()
    kwargs["confidence"] = -0.1

    with pytest.raises(ValidationError):
        FailureAnalysis(**kwargs)


def test_evidence_and_next_steps_default_to_empty_lists():
    kwargs = make_valid_kwargs()
    del kwargs["evidence"]
    del kwargs["next_steps"]

    analysis = FailureAnalysis(**kwargs)

    assert analysis.evidence == []
    assert analysis.next_steps == []


def test_evidence_accepts_valid_normalized_shape():
    evidence = Evidence(
        source="console",
        provenance="trace",
        summary="Request failed with status 503",
        timestamp_ms=1250.5,
    )

    assert evidence.source == "console"
    assert evidence.provenance == "trace"
    assert evidence.summary == "Request failed with status 503"
    assert evidence.severity == 1
    assert evidence.timestamp_ms == 1250.5


@pytest.mark.parametrize("severity", [0, 5])
def test_evidence_rejects_severity_outside_supported_range(severity):
    with pytest.raises(ValidationError):
        Evidence(
            source="network",
            provenance="har",
            summary="Service unavailable",
            severity=severity,
        )


def test_attachment_defaults_optional_content_fields():
    attachment = Attachment(name="trace.zip")

    assert attachment.content_type == ""
    assert attachment.path is None
    assert attachment.body_b64 is None


def test_failed_attempt_has_advisory_and_collection_defaults():
    attempt = FailedAttempt(status="failed")

    assert attempt.spec_id == ""
    assert attempt.file == ""
    assert attempt.line == 0
    assert attempt.column == 0
    assert attempt.title_path == []
    assert attempt.project_id == ""
    assert attempt.project_name == ""
    assert attempt.test_ordinal == 0
    assert attempt.result_ordinal == 0
    assert attempt.retry == 0
    assert attempt.start_time == ""
    assert attempt.expected_status == "passed"
    assert attempt.aggregate_status == "unexpected"
    assert attempt.error_message == ""
    assert attempt.error_stack == ""
    assert attempt.duration_ms == 0
    assert attempt.stdout == []
    assert attempt.stderr == []
    assert attempt.attachments == []
    assert attempt.warnings == []


def test_failed_attempt_requires_status():
    with pytest.raises(ValidationError):
        FailedAttempt()


def test_attempt_key_is_deterministic_and_preserves_identity_components():
    identity = {
        "spec_id": "spec-7",
        "file": "tests/login.spec.ts",
        "line": 42,
        "column": 9,
        "title_path": ["authentication", "login redirects"],
        "project_id": "chromium",
        "project_name": "Desktop Chromium",
        "test_ordinal": 3,
        "result_ordinal": 2,
        "retry": 1,
        "start_time": "2026-07-12T10:15:30.000Z",
        "status": "failed",
    }

    first = FailedAttempt(**identity)
    second = FailedAttempt(**identity)

    assert first.attempt_key == second.attempt_key
    for component in (
        "spec-7",
        "tests/login.spec.ts",
        "42",
        "9",
        "authentication",
        "login redirects",
        "chromium",
        "Desktop Chromium",
        "3",
        "2",
        "1",
        "2026-07-12T10:15:30.000Z",
    ):
        assert component in first.attempt_key


def test_attempt_key_distinguishes_different_retries():
    first_try = FailedAttempt(status="failed", file="tests/login.spec.ts", retry=0)
    second_try = FailedAttempt(status="failed", file="tests/login.spec.ts", retry=1)

    assert first_try.attempt_key != second_try.attempt_key


@pytest.mark.parametrize(
    "field, value",
    [
        ("source", "telemetry"),
        ("provenance", "guesswork"),
    ],
)
def test_evidence_rejects_values_outside_literals(field, value):
    kwargs = {
        "source": "console",
        "provenance": "trace",
        "summary": "Request failed with status 503",
    }
    kwargs[field] = value

    with pytest.raises(ValidationError):
        Evidence(**kwargs)


def test_eventually_passed_reflects_flaky_aggregate_status():
    assert FailedAttempt(status="failed", aggregate_status="flaky").eventually_passed is True
    assert FailedAttempt(status="failed", aggregate_status="unexpected").eventually_passed is False


def test_failure_context_remains_backward_compatible():
    context = FailureContext(
        test_title="login redirects",
        file="tests/login.spec.ts",
        status="failed",
        error_message="expected dashboard",
    )

    assert context.error_stack == ""
    assert context.duration_ms == 0


def test_failure_analysis_ui_fields_have_safe_defaults():
    analysis = FailureAnalysis(**make_valid_kwargs())

    assert analysis.project == ""
    assert analysis.retry == 0
    assert analysis.flaky is False
