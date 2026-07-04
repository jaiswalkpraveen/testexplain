"""Tests for the M1 structured FailureAnalysis model.

M0's FailureAnalysis was just (test_title, explanation) -- a text blob.
M1 makes it structured: category, evidence, next steps, confidence.
These tests pin down the new shape BEFORE we change models.py (TDD).
"""

import pytest
from pydantic import ValidationError

from testlens.models import FailureAnalysis


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
