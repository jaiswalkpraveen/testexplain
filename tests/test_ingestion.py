from testlens.models import FailureContext


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
