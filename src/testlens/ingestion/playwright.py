import json
from pathlib import Path

from testlens.models import FailureContext

FAILED_STATUSES = {"failed", "timedOut"}


def parse_report(path: str | Path) -> list[FailureContext]:
    """Read a Playwright JSON report and return only the failed tests
    as flat FailureContext objects (the only thing the LLM ever sees)."""
    data = json.loads(Path(path).read_text())

    failures: list[FailureContext] = []
    for suite in data.get("suites", []):
        for spec in suite.get("specs", []):
            for test in spec.get("tests", []):
                for result in test.get("results", []):
                    if result.get("status") not in FAILED_STATUSES:
                        continue  # skip passed tests
                    error = result.get("error") or {}
                    failures.append(
                        FailureContext(
                            test_title=spec.get("title", ""),
                            file=spec.get("file", suite.get("file", "")),
                            status=result.get("status", "failed"),
                            error_message=error.get("message", ""),
                            error_stack=error.get("stack", ""),
                            duration_ms=result.get("duration", 0),
                        )
                    )
    return failures
