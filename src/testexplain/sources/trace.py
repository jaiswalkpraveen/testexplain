"""Read Playwright trace action events into normalized evidence."""

from __future__ import annotations

import json
import math
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testexplain.models import Evidence

SUPPORTED_TRACE_VERSIONS = {8}
MAX_TRACE_MEMBERS = 100
MAX_TRACE_MEMBER_SIZE = 100 * 1024 * 1024
MAX_TRACE_TOTAL_SIZE = 500 * 1024 * 1024
MAX_TRACE_LINE_LENGTH = 1024 * 1024
MAX_TRACE_EVENTS = 100_000
MAX_TRACE_EVIDENCE = 50_000
MAX_TRACE_WARNINGS = 1_000


@dataclass
class TraceResult:
    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class _PendingAction:
    before: dict[str, Any]
    logs: list[str] = field(default_factory=list)


def read_trace_actions(path: Path) -> TraceResult:
    """Return normalized action evidence from each ``*.trace`` ZIP member."""
    result = TraceResult()
    try:
        with zipfile.ZipFile(path) as archive:
            trace_members = [
                member
                for member in archive.infolist()
                if not member.is_dir() and member.filename.endswith(".trace")
            ]
            if len(trace_members) > MAX_TRACE_MEMBERS:
                result.warnings.append(
                    f"{path.name}: trace member limit exceeded; only first "
                    f"{MAX_TRACE_MEMBERS} members processed"
                )
                trace_members = trace_members[:MAX_TRACE_MEMBERS]

            total_size = 0
            for member in trace_members:
                if member.file_size > MAX_TRACE_MEMBER_SIZE:
                    result.warnings.append(
                        f"{member.filename}: trace member size limit exceeded; skipped"
                    )
                    continue
                if total_size + member.file_size > MAX_TRACE_TOTAL_SIZE:
                    result.warnings.append(
                        f"{member.filename}: trace total size limit exceeded; skipped"
                    )
                    continue
                total_size += member.file_size
                try:
                    with archive.open(member) as stream:
                        _read_stream(member.filename, stream, result)
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error) as exc:
                    result.warnings.append(
                        f"{member.filename}: unreadable trace member; skipped ({exc})"
                    )
    except (zipfile.BadZipFile, OSError, RuntimeError):
        result.warnings.append(f"{path.name}: unreadable ZIP; skipped")
    return result


def _read_stream(member_name: str, stream: Any, result: TraceResult) -> None:
    context: dict[str, Any] | None = None
    pending: dict[str, _PendingAction] = {}
    member_evidence: list[Evidence] = []
    member_warnings: list[str] = []
    suppressed_warnings = 0
    suppressed_evidence = 0
    line_number = 0
    saw_event_before_context = False

    def warn(message: str, *, terminal: bool = False) -> None:
        nonlocal suppressed_warnings
        if terminal and len(member_warnings) >= MAX_TRACE_WARNINGS:
            member_warnings.pop()
            suppressed_warnings += 1
        if terminal or len(member_warnings) < MAX_TRACE_WARNINGS:
            member_warnings.append(message)
        else:
            suppressed_warnings += 1

    def flush_warnings() -> None:
        if suppressed_warnings:
            member_warnings.append(
                f"{member_name}: {suppressed_warnings} warnings suppressed"
            )
        result.warnings.extend(member_warnings)

    def emit(action: _PendingAction, after: dict[str, Any] | None) -> None:
        nonlocal suppressed_evidence
        if len(result.evidence) + len(member_evidence) >= MAX_TRACE_EVIDENCE:
            suppressed_evidence += 1
            return
        try:
            member_evidence.append(_to_evidence(action, after, context))
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            warn(f"{member_name}: action formatting failed; skipped ({exc})")

    while True:
        raw_line = stream.readline(MAX_TRACE_LINE_LENGTH + 1)
        if not raw_line:
            break

        line_number += 1
        if line_number > MAX_TRACE_EVENTS:
            warn(f"{member_name}: event limit exceeded; stream skipped", terminal=True)
            flush_warnings()
            return

        if len(raw_line) > MAX_TRACE_LINE_LENGTH:
            while raw_line and not raw_line.endswith(b"\n"):
                raw_line = stream.readline(MAX_TRACE_LINE_LENGTH + 1)
            warn(f"{member_name} line {line_number}: line length limit exceeded; skipped")
            continue

        try:
            event = json.loads(raw_line, parse_constant=_reject_constant)
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError, RecursionError):
            warn(f"{member_name} line {line_number}: malformed JSON; skipped")
            continue
        if not isinstance(event, dict):
            warn(f"{member_name} line {line_number}: expected JSON object; skipped")
            continue

        event_type = event.get("type")
        if event_type == "context-options":
            if context is not None:
                warn(f"{member_name}: duplicate context-options; stream skipped", terminal=True)
                flush_warnings()
                return
            if saw_event_before_context:
                warn(f"{member_name}: late context-options; stream skipped", terminal=True)
                flush_warnings()
                return
            version = event.get("version")
            if type(version) is not int or version not in SUPPORTED_TRACE_VERSIONS:
                warn(
                    f"{member_name}: unsupported trace version {version!r}; stream skipped",
                    terminal=True,
                )
                flush_warnings()
                return
            context = event
            if not _has_usable_anchor(context):
                warn(f"{member_name}: missing usable clock anchor")
            continue

        if context is None:
            saw_event_before_context = True
            warn(f"{member_name} line {line_number}: event before context-options; skipped")
            continue

        call_id = event.get("callId")
        if event_type == "before" and isinstance(call_id, str):
            if call_id in pending:
                warn(
                    f"{member_name}: duplicate before for {call_id}; "
                    "previous action preserved as incomplete"
                )
                emit(pending.pop(call_id), None)
            pending[call_id] = _PendingAction(before=event)
        elif event_type == "log" and isinstance(call_id, str):
            action = pending.get(call_id)
            if action is None:
                warn(f"{member_name}: orphan log for {call_id}; skipped")
            elif isinstance(event.get("message"), str):
                action.logs.append(event["message"])
        elif event_type == "after" and isinstance(call_id, str):
            action = pending.pop(call_id, None)
            if action is None:
                warn(f"{member_name}: orphan after for {call_id}; skipped")
            else:
                emit(action, event)

    if context is None:
        warn(f"{member_name}: missing context-options; stream skipped", terminal=True)
        flush_warnings()
        return

    for action in pending.values():
        emit(action, None)
    if suppressed_evidence:
        warn(f"{member_name}: evidence limit exceeded; excess evidence skipped", terminal=True)

    result.evidence.extend(member_evidence)
    flush_warnings()


def _reject_constant(value: str) -> None:
    raise ValueError(f"non-finite JSON number: {value}")


def _has_usable_anchor(context: dict[str, Any]) -> bool:
    return all(_finite_float(context.get(key)) is not None for key in ("wallTime", "monotonicTime"))


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


def _to_evidence(
    action: _PendingAction,
    after: dict[str, Any] | None,
    context: dict[str, Any] | None,
) -> Evidence:
    before = action.before
    if "title" in before:
        title = _format_value(before["title"])
    elif "method" in before:
        title = _format_value(before["method"])
    else:
        title = "unknown action"
    parts = [title]

    if "params" in before:
        parts.append(f"params={_compact_json(before['params'])}")
    if action.logs:
        parts.append(f"logs={' ; '.join(action.logs)}")

    has_error = False
    if after is None:
        parts.append("incomplete")
    else:
        if "result" in after:
            parts.append(f"result={_compact_json(after['result'])}")
        if "error" in after:
            parts.append(f"error={_format_error(after['error'])}")
            has_error = True

    timestamp_ms = _wall_timestamp(before.get("startTime"), context)
    failed = after is None or has_error
    return Evidence(
        source="action",
        provenance="trace",
        summary=" | ".join(parts),
        severity=4 if failed else 1,
        timestamp_ms=timestamp_ms,
    )


def _compact_json(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True, allow_nan=False)


def _format_value(value: Any) -> str:
    return value if isinstance(value, str) else _compact_json(value)


def _format_error(error: Any) -> str:
    if error is None:
        return "null"
    if not isinstance(error, dict):
        return _format_value(error)
    name = error.get("name")
    message = error.get("message")
    return f"{name}: {message}" if name and message else str(message or name or _compact_json(error))


def _wall_timestamp(start_time: Any, context: dict[str, Any] | None) -> float | None:
    if context is None:
        return None
    start = _finite_float(start_time)
    wall = _finite_float(context.get("wallTime"))
    monotonic = _finite_float(context.get("monotonicTime"))
    if start is None or wall is None or monotonic is None:
        return None
    timestamp = wall + (start - monotonic)
    return timestamp if math.isfinite(timestamp) else None
