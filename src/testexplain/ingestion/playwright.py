import json
from pathlib import Path
from typing import Any

from testexplain.models import Attachment, FailedAttempt, FailureContext

FAILED_STATUSES = {"failed", "timedOut"}


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{location} must be an object")
    return value


def _list_field(container: dict[str, Any], field: str, location: str) -> list[Any]:
    value = container.get(field, [])
    if not isinstance(value, list):
        raise ValueError(f"{location}.{field} must be a list")
    return value


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return ""


def _number(value: Any, default: int | float) -> int | float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        return value
    return default


def _output(entries: Any) -> list[str]:
    if not isinstance(entries, list):
        return []
    normalized: list[str] = []
    for entry in entries:
        if isinstance(entry, str):
            normalized.append(entry)
        elif isinstance(entry, dict):
            for field in ("text", "base64", "buffer", "body"):
                value = entry.get(field)
                if isinstance(value, str):
                    normalized.append(value)
                    break
    return normalized


def _attachments(entries: Any) -> list[Attachment]:
    if not isinstance(entries, list):
        return []
    attachments: list[Attachment] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path")
        body = entry.get("body", entry.get("base64"))
        attachments.append(
            Attachment(
                name=_text(entry.get("name")),
                content_type=_text(entry.get("contentType")),
                path=path if isinstance(path, str) else None,
                body_b64=body if isinstance(body, str) else None,
            )
        )
    return attachments


def _error(result: dict[str, Any]) -> tuple[str, str]:
    candidates = [result.get("error")]
    errors = result.get("errors", [])
    if isinstance(errors, list):
        candidates.extend(errors)
    for candidate in candidates:
        if isinstance(candidate, str) and candidate:
            return candidate, ""
        if isinstance(candidate, dict):
            message = _text(candidate.get("message"))
            stack = _text(candidate.get("stack"))
            if message or stack:
                return message, stack
    return "", ""


def _parse_suite(
    suite_value: Any,
    location: str,
    parent_titles: list[str],
    inherited_file: str = "",
    inherited_line: int = 0,
    inherited_column: int = 0,
) -> list[FailedAttempt]:
    suite = _object(suite_value, location)
    suite_title = _text(suite.get("title"))
    titles = [*parent_titles, suite_title] if suite_title else parent_titles
    suite_file = _text(suite.get("file")) or inherited_file
    suite_line = int(_number(suite.get("line"), inherited_line))
    suite_column = int(_number(suite.get("column"), inherited_column))
    attempts: list[FailedAttempt] = []

    for spec_ordinal, spec_value in enumerate(_list_field(suite, "specs", location)):
        spec_location = f"{location}.specs[{spec_ordinal}]"
        spec = _object(spec_value, spec_location)
        spec_title = _text(spec.get("title"))
        title_path = [*titles, spec_title] if spec_title else titles
        file = _text(spec.get("file")) or suite_file
        line = int(_number(spec.get("line"), suite_line))
        column = int(_number(spec.get("column"), suite_column))

        for test_ordinal, test_value in enumerate(_list_field(spec, "tests", spec_location)):
            test_location = f"{spec_location}.tests[{test_ordinal}]"
            test = _object(test_value, test_location)
            expected_status = _text(test.get("expectedStatus")) or "passed"
            aggregate_status = _text(test.get("status")) or "unexpected"
            if expected_status == "failed":
                continue

            for result_ordinal, result_value in enumerate(
                _list_field(test, "results", test_location)
            ):
                result_location = f"{test_location}.results[{result_ordinal}]"
                result = _object(result_value, result_location)
                status = _text(result.get("status"))
                if status not in FAILED_STATUSES or status == expected_status:
                    continue
                error_message, error_stack = _error(result)
                attempts.append(
                    FailedAttempt(
                        spec_id=_text(spec.get("id")),
                        file=file,
                        line=line,
                        column=column,
                        title_path=title_path,
                        project_id=_text(test.get("projectId")),
                        project_name=_text(test.get("projectName")),
                        test_ordinal=test_ordinal,
                        result_ordinal=result_ordinal,
                        retry=int(_number(result.get("retry"), 0)),
                        start_time=_text(result.get("startTime")),
                        status=status,
                        expected_status=expected_status,
                        aggregate_status=aggregate_status,
                        error_message=error_message,
                        error_stack=error_stack,
                        duration_ms=float(_number(result.get("duration"), 0.0)),
                        stdout=_output(result.get("stdout")),
                        stderr=_output(result.get("stderr")),
                        attachments=_attachments(result.get("attachments")),
                    )
                )

    for child_ordinal, child in enumerate(_list_field(suite, "suites", location)):
        attempts.extend(
            _parse_suite(
                child,
                f"{location}.suites[{child_ordinal}]",
                titles,
                suite_file,
                suite_line,
                suite_column,
            )
        )
    return attempts


def parse_attempts(path: str | Path) -> list[FailedAttempt]:
    data = json.loads(Path(path).read_text())
    report = _object(data, "top-level report")
    attempts: list[FailedAttempt] = []
    for suite_ordinal, suite in enumerate(_list_field(report, "suites", "report")):
        attempts.extend(_parse_suite(suite, f"report.suites[{suite_ordinal}]", []))
    return attempts


def parse_report(path: str | Path) -> list[FailureContext]:
    return [
        FailureContext(
            test_title=attempt.title_path[-1] if attempt.title_path else "",
            file=attempt.file,
            status=attempt.status,
            error_message=attempt.error_message,
            error_stack=attempt.error_stack,
            duration_ms=int(attempt.duration_ms),
        )
        for attempt in parse_attempts(path)
    ]
