from pathlib import Path

from testlens.models import FailureContext
from testlens.ingestion.playwright import parse_report

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def test_failure_context_minimal():
    fc = FailureContext(
        test_title="login works",
        file="tests/login.spec.ts",
        status="failed",
        error_message="Timeout 30000ms exceeded",
    )
    assert fc.test_title == "login works"
    assert fc.error_stack == ""
    assert fc.duration_ms == 0


def test_parse_report_extracts_only_failures():
    failures = parse_report(FIXTURE)

    # the fixture has 1 passed + 1 failed test; only the failure is returned
    assert len(failures) == 1

    fc = failures[0]
    assert isinstance(fc, FailureContext)
    assert fc.test_title == "user sees dashboard after login"
    assert fc.file == "tests/auth.spec.ts"
    assert fc.status == "failed"
    assert "Timeout 30000ms exceeded" in fc.error_message
    assert "TimeoutError" in fc.error_stack
    assert fc.duration_ms == 30000
