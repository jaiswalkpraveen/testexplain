import json
from pathlib import Path

import pytest

from testexplain.ingestion.playwright import parse_attempts, parse_report
from testexplain.models import FailedAttempt, FailureContext

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


def write_report(tmp_path: Path, report: object) -> Path:
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    return path


def test_parse_attempts_recurses_and_preserves_attempt_details(tmp_path):
    report = {
        "suites": [
            {
                "title": "root",
                "file": "tests/fallback.spec.ts",
                "line": 2,
                "column": 3,
                "suites": [
                    {
                        "title": "nested",
                        "specs": [
                            {
                                "id": "shared-spec",
                                "title": "duplicate title",
                                "file": "tests/edge.spec.ts",
                                "line": 17,
                                "column": 8,
                                "tests": [
                                    {
                                        "projectId": "chromium",
                                        "projectName": "Desktop Chromium",
                                        "expectedStatus": "passed",
                                        "status": "flaky",
                                        "results": [
                                            {
                                                "status": "failed",
                                                "retry": 0,
                                                "startTime": "2026-07-12T10:00:00.000Z",
                                                "duration": 12.75,
                                                "error": {
                                                    "message": "first failure",
                                                    "stack": "first stack",
                                                },
                                                "stdout": [
                                                    "plain output",
                                                    {"text": "object output"},
                                                    {"buffer": "b3V0"},
                                                ],
                                                "stderr": [{"base64": "ZXJy"}],
                                                "attachments": [
                                                    {
                                                        "name": "trace",
                                                        "contentType": "application/zip",
                                                        "path": "trace-0.zip",
                                                        "body": "Ym9keQ==",
                                                    }
                                                ],
                                            },
                                            {
                                                "status": "passed",
                                                "retry": 1,
                                                "attachments": [{"name": "not-failed"}],
                                            },
                                        ],
                                    },
                                    {
                                        "projectId": "firefox",
                                        "projectName": "Desktop Firefox",
                                        "expectedStatus": "passed",
                                        "status": "unexpected",
                                        "results": [
                                            {
                                                "status": "timedOut",
                                                "retry": 0,
                                                "startTime": "2026-07-12T10:01:00.000Z",
                                                "duration": 5.5,
                                                "errors": [
                                                    {},
                                                    {
                                                        "message": "useful timeout",
                                                        "stack": "timeout stack",
                                                    },
                                                ],
                                                "attachments": [
                                                    {"name": "screenshot", "path": "shot.png"}
                                                ],
                                            },
                                            {
                                                "status": "failed",
                                                "retry": 1,
                                                "error": {"message": "second failure"},
                                            },
                                        ],
                                    },
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    attempts = parse_attempts(write_report(tmp_path, report))

    assert len(attempts) == 3
    assert all(isinstance(attempt, FailedAttempt) for attempt in attempts)
    first, timed_out, second_failure = attempts
    assert first.spec_id == "shared-spec"
    assert first.file == "tests/edge.spec.ts"
    assert (first.line, first.column) == (17, 8)
    assert first.title_path == ["root", "nested", "duplicate title"]
    assert (first.project_id, first.project_name) == ("chromium", "Desktop Chromium")
    assert (first.test_ordinal, first.result_ordinal, first.retry) == (0, 0, 0)
    assert first.start_time == "2026-07-12T10:00:00.000Z"
    assert first.status == "failed"
    assert first.expected_status == "passed"
    assert first.aggregate_status == "flaky"
    assert first.eventually_passed is True
    assert (first.error_message, first.error_stack) == ("first failure", "first stack")
    assert first.duration_ms == 12.75
    assert first.stdout == ["plain output", "object output", "b3V0"]
    assert first.stderr == ["ZXJy"]
    assert first.attachments[0].model_dump() == {
        "name": "trace",
        "content_type": "application/zip",
        "path": "trace-0.zip",
        "body_b64": "Ym9keQ==",
    }
    assert timed_out.title_path == ["root", "nested", "duplicate title"]
    assert (timed_out.project_id, timed_out.test_ordinal, timed_out.result_ordinal) == (
        "firefox",
        1,
        0,
    )
    assert timed_out.status == "timedOut"
    assert (timed_out.error_message, timed_out.error_stack) == (
        "useful timeout",
        "timeout stack",
    )
    assert [attachment.name for attachment in timed_out.attachments] == ["screenshot"]
    assert (
        second_failure.test_ordinal,
        second_failure.result_ordinal,
        second_failure.retry,
    ) == (1, 1, 1)
    assert second_failure.error_message == "second failure"
    assert second_failure.attachments == []


def test_parse_attempts_uses_suite_location_when_spec_omits_it(tmp_path):
    report = {
        "suites": [
            {
                "title": "suite",
                "file": "tests/fallback.spec.ts",
                "line": 4,
                "column": 2,
                "specs": [
                    {
                        "title": "no own location",
                        "tests": [{"results": [{"status": "failed"}]}],
                    }
                ],
            }
        ]
    }

    attempt = parse_attempts(write_report(tmp_path, report))[0]

    assert attempt.file == "tests/fallback.spec.ts"
    assert (attempt.line, attempt.column) == (4, 2)


def test_parse_attempts_excludes_expected_failures(tmp_path):
    report = {
        "suites": [
            {
                "title": "expected behavior",
                "specs": [
                    {
                        "title": "known failure",
                        "tests": [
                            {
                                "expectedStatus": "failed",
                                "status": "expected",
                                "results": [
                                    {"status": "failed", "error": {"message": "expected"}}
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    assert parse_attempts(write_report(tmp_path, report)) == []


def test_parse_attempts_excludes_timed_out_result_for_expected_failure(tmp_path):
 report = {
 "suites": [
 {
 "specs": [
 {
 "tests": [
 {
 "expectedStatus": "failed",
 "results": [{"status": "timedOut"}],
 }
 ]
 }
 ]
 }
 ]
 }

 assert parse_attempts(write_report(tmp_path, report)) == []


@pytest.mark.parametrize(
    "report, message",
    [
        ([], "top-level"),
        ({"suites": {}}, "suites"),
        ({"suites": ["bad suite"]}, "suite"),
        ({"suites": [{"specs": {}}]}, "specs"),
        ({"suites": [{"specs": [{"tests": {}}]}]}, "tests"),
        ({"suites": [{"specs": [{"tests": [{"results": {}}]}]}]}, "results"),
    ],
)
def test_parse_attempts_rejects_malformed_structural_shapes(tmp_path, report, message):
    with pytest.raises(ValueError, match=message):
        parse_attempts(write_report(tmp_path, report))


def test_parse_report_maps_every_attempt_to_legacy_context(tmp_path):
    report = {
        "suites": [
            {
                "title": "outer",
                "file": "tests/fallback.spec.ts",
                "line": 4,
                "column": 2,
                "specs": [
                    {
                        "title": "leaf title",
                        "tests": [
                            {
                                "status": "unexpected",
                                "results": [
                                    {
                                        "status": "failed",
                                        "duration": 10.75,
                                        "errors": [
                                            {"message": "legacy error", "stack": "legacy stack"}
                                        ],
                                    },
                                    {"status": "timedOut", "duration": 10.25},
                                ],
                            }
                        ],
                    }
                ],
            }
        ]
    }

    failures = parse_report(write_report(tmp_path, report))

    assert [failure.test_title for failure in failures] == ["leaf title", "leaf title"]
    assert [failure.file for failure in failures] == [
        "tests/fallback.spec.ts",
        "tests/fallback.spec.ts",
    ]
    assert [failure.status for failure in failures] == ["failed", "timedOut"]
    assert failures[0].error_message == "legacy error"
    assert failures[0].error_stack == "legacy stack"
    assert [failure.duration_ms for failure in failures] == [10, 10]
