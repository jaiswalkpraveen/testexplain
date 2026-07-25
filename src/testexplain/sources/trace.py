"""Read Playwright trace action, console, output, and network events into evidence."""

from __future__ import annotations

import json
import math
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from testexplain.models import Evidence
from testexplain.sources.common import (
    SourceResult,
    finite_float,
    forgiving_b64decode,
    reject_constant,
)
from testexplain.sources.network import (
    MAX_NETWORK_URL_LENGTH,
    har_entry_evidence,
)

SUPPORTED_TRACE_VERSIONS = {8}
TRACE_SUFFIX = ".trace"
NETWORK_SUFFIX = ".network"
CONSOLE_SEVERITY = {"error": 4, "warning": 3, "warn": 3, "info": 2}
DEFAULT_CONSOLE_SEVERITY = 1
STDIO_SEVERITY = {"stderr": 3, "stdout": 1}
MAX_TRACE_MEMBERS = 100
MAX_TRACE_MEMBER_SIZE = 100 * 1024 * 1024
MAX_TRACE_TOTAL_SIZE = 500 * 1024 * 1024
MAX_TRACE_LINE_LENGTH = 1024 * 1024
MAX_TRACE_EVENTS = 100_000
MAX_TRACE_EVIDENCE = 50_000
MAX_TRACE_WARNINGS = 1_000

# Re-exported so callers and tests that reach for the network URL cap through this
# adapter keep working after the shared normalizer moved to ``sources.network``.
__all__ = ["MAX_NETWORK_URL_LENGTH", "TraceResult", "read_trace_actions"]

# The trace adapter used to own this result shape. Every adapter returns it now,
# so the name here is an alias kept for readability at the call sites.
TraceResult = SourceResult


@dataclass
class _PendingAction:
    before: dict[str, Any]
    logs: list[str] = field(default_factory=list)


def read_trace_actions(path: Path) -> TraceResult:
    """Return normalized action, console, output, and network evidence per trace stream.

    Playwright splits one browser context across sibling members that share an
    ordinal: ``<ordinal>.trace`` holds actions, console, and output records while
    ``<ordinal>.network`` holds the resource snapshots. Only the ``.trace`` member
    carries the ``context-options`` header, so each network stream is read with the
    schema version and clock anchor inherited from its paired trace stream.
    """
    result = TraceResult()
    try:
        with zipfile.ZipFile(path) as archive:
            members = [member for member in archive.infolist() if not member.is_dir()]
            trace_members = _limit_members(
                [member for member in members if member.filename.endswith(TRACE_SUFFIX)],
                f"{path.name}: trace member limit exceeded",
                result,
            )
            network_members = _limit_members(
                [member for member in members if member.filename.endswith(NETWORK_SUFFIX)],
                f"{path.name}: network member limit exceeded",
                result,
            )
            network_by_stem = {
                member.filename[: -len(NETWORK_SUFFIX)]: member for member in network_members
            }

            total_size = 0

            def within_size_limits(member: zipfile.ZipInfo) -> bool:
                """Charge one member against the per-member and whole-archive budgets."""
                nonlocal total_size
                if member.file_size > MAX_TRACE_MEMBER_SIZE:
                    result.warnings.append(
                        f"{member.filename}: trace member size limit exceeded; skipped"
                    )
                    return False
                if total_size + member.file_size > MAX_TRACE_TOTAL_SIZE:
                    result.warnings.append(
                        f"{member.filename}: trace total size limit exceeded; skipped"
                    )
                    return False
                total_size += member.file_size
                return True

            def read_member(
                member: zipfile.ZipInfo,
                inherited_context: dict[str, Any] | None = None,
            ) -> dict[str, Any] | None:
                """Read one member, returning its context when the stream was usable."""
                try:
                    with archive.open(member) as stream:
                        return _read_stream(
                            member.filename,
                            stream,
                            result,
                            inherited_context=inherited_context,
                        )
                except (zipfile.BadZipFile, OSError, RuntimeError, EOFError, zlib.error) as exc:
                    result.warnings.append(
                        f"{member.filename}: unreadable trace member; skipped ({exc})"
                    )
                    return None

            paired_stems = set()
            for member in trace_members:
                stem = member.filename[: -len(TRACE_SUFFIX)]
                paired_stems.add(stem)
                if not within_size_limits(member):
                    continue
                context = read_member(member)
                network_member = network_by_stem.get(stem)
                if context is None or network_member is None:
                    continue
                if not within_size_limits(network_member):
                    continue
                read_member(network_member, inherited_context=context)

            for stem, member in network_by_stem.items():
                if stem not in paired_stems:
                    result.warnings.append(
                        f"{member.filename}: network member has no paired trace stream; skipped"
                    )
    except (zipfile.BadZipFile, OSError, RuntimeError):
        result.warnings.append(f"{path.name}: unreadable ZIP; skipped")
    return result


def _limit_members(
    members: list[zipfile.ZipInfo],
    message: str,
    result: TraceResult,
) -> list[zipfile.ZipInfo]:
    """Cap one member family so a crafted archive cannot flood work or warnings."""
    if len(members) <= MAX_TRACE_MEMBERS:
        return members
    result.warnings.append(f"{message}; only first {MAX_TRACE_MEMBERS} members processed")
    return members[:MAX_TRACE_MEMBERS]


def _read_stream(
    member_name: str,
    stream: Any,
    result: TraceResult,
    *,
    inherited_context: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read one stream, returning its context when the stream produced usable evidence.

    A network stream has no ``context-options`` header of its own, so it is handed the
    context of its paired trace stream. Returning ``None`` marks a stream the caller
    must not pair anything with: its schema version, clocks, or framing were unusable.
    """
    context: dict[str, Any] | None = inherited_context
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

    def stage(build: Callable[[], Evidence], description: str) -> None:
        """Add one evidence record, honoring the evidence cap and formatting faults."""
        nonlocal suppressed_evidence
        if len(result.evidence) + len(member_evidence) >= MAX_TRACE_EVIDENCE:
            suppressed_evidence += 1
            return
        try:
            member_evidence.append(build())
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            warn(f"{member_name}: {description} formatting failed; skipped ({exc})")

    def emit(action: _PendingAction, after: dict[str, Any] | None) -> None:
        stage(lambda: _to_evidence(action, after, context), "action")

    while True:
        raw_line = stream.readline(MAX_TRACE_LINE_LENGTH + 1)
        if not raw_line:
            break

        line_number += 1
        if line_number > MAX_TRACE_EVENTS:
            warn(f"{member_name}: event limit exceeded; stream skipped", terminal=True)
            flush_warnings()
            return None

        if len(raw_line) > MAX_TRACE_LINE_LENGTH:
            while raw_line and not raw_line.endswith(b"\n"):
                raw_line = stream.readline(MAX_TRACE_LINE_LENGTH + 1)
            warn(f"{member_name} line {line_number}: line length limit exceeded; skipped")
            continue

        try:
            event = json.loads(raw_line, parse_constant=reject_constant)
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
                return None
            if saw_event_before_context:
                warn(f"{member_name}: late context-options; stream skipped", terminal=True)
                flush_warnings()
                return None
            version = event.get("version")
            if type(version) is not int or version not in SUPPORTED_TRACE_VERSIONS:
                warn(
                    f"{member_name}: unsupported trace version {version!r}; stream skipped",
                    terminal=True,
                )
                flush_warnings()
                return None
            context = event
            if not _has_usable_anchor(context):
                warn(f"{member_name}: missing usable clock anchor")
            continue

        if context is None:
            saw_event_before_context = True
            warn(f"{member_name} line {line_number}: event before context-options; skipped")
            continue

        if event_type == "console":
            stage(lambda event=event: _console_evidence(event, context), "console")
            continue
        if event_type == "event":
            if event.get("method") == "pageError":
                stage(lambda event=event: _page_error_evidence(event, context), "page error")
            continue
        if event_type == "resource-snapshot":
            snapshot = event.get("snapshot")
            # The request is required because it supplies the method and URL that make
            # the record meaningful. A missing or broken response is tolerated: a
            # request that never completed is itself a useful triage signal, and every
            # response-derived field degrades to "unknown".
            request = snapshot.get("request") if isinstance(snapshot, dict) else None
            if not isinstance(snapshot, dict) or not isinstance(request, dict):
                warn(
                    f"{member_name} line {line_number}: "
                    "unusable resource-snapshot payload; skipped"
                )
                continue
            stage(lambda snapshot=snapshot: _network_evidence(snapshot, context), "network")
            continue
        if event_type in STDIO_SEVERITY:
            text = _stdio_text(event)
            if text is None:
                warn(
                    f"{member_name} line {line_number}: "
                    f"unusable {event_type} payload; skipped"
                )
                continue
            stage(lambda event=event, text=text: _stdio_evidence(event, text, context), "output")
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
        return None

    for action in pending.values():
        emit(action, None)
    if suppressed_evidence:
        warn(f"{member_name}: evidence limit exceeded; excess evidence skipped", terminal=True)

    result.evidence.extend(member_evidence)
    flush_warnings()
    return context


def _has_usable_anchor(context: dict[str, Any]) -> bool:
    return all(finite_float(context.get(key)) is not None for key in ("wallTime", "monotonicTime"))


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


def _console_evidence(event: dict[str, Any], context: dict[str, Any] | None) -> Evidence:
    """Normalize one browser console record into ranked console evidence."""
    message_type = event.get("messageType")
    label = message_type if isinstance(message_type, str) else "console"
    summary = f"{label}: {_format_value(event.get('text', ''))}"
    location = _format_location(event.get("location"), ("lineNumber", "columnNumber"))
    if location:
        summary = f"{summary} | location={location}"
    return Evidence(
        source="console",
        provenance="trace",
        summary=summary,
        severity=CONSOLE_SEVERITY.get(label.lower(), DEFAULT_CONSOLE_SEVERITY),
        timestamp_ms=_wall_timestamp(event.get("time"), context),
    )


def _page_error_evidence(event: dict[str, Any], context: dict[str, Any] | None) -> Evidence:
    """Normalize one uncaught page exception into high-severity evidence."""
    params = event.get("params")
    params = params if isinstance(params, dict) else {}
    summary = _format_error(_unwrap_serialized_error(params.get("error")))
    location = _format_location(params.get("location"), ("line", "column"))
    if location:
        summary = f"{summary} | location={location}"
    return Evidence(
        source="page_error",
        provenance="trace",
        summary=summary,
        severity=4,
        timestamp_ms=_wall_timestamp(event.get("time"), context),
    )


def _stdio_evidence(
    event: dict[str, Any],
    text: str,
    context: dict[str, Any] | None,
) -> Evidence:
    """Normalize one runner stdout/stderr chunk into evidence."""
    stream = event["type"]
    return Evidence(
        source=stream,
        provenance="trace",
        summary=text,
        severity=STDIO_SEVERITY[stream],
        timestamp_ms=_wall_timestamp(event.get("timestamp"), context),
    )


def _network_evidence(snapshot: dict[str, Any], context: dict[str, Any] | None) -> Evidence:
    """Normalize one trace ``resource-snapshot`` into network evidence.

    A trace resource snapshot is literally a HAR entry, so the shared HAR
    normalizer does the work and this wrapper only supplies what is specific to
    traces: the ``trace`` provenance and a wall-clock time derived from the
    monotonic stamp against the paired stream's clock anchor.
    """
    return har_entry_evidence(
        snapshot,
        provenance="trace",
        timestamp_ms=_wall_timestamp(snapshot.get("_monotonicTime"), context),
    )


def _stdio_text(event: dict[str, Any]) -> str | None:
    """Return the chunk text, preferring plain text over base64 bytes."""
    text = event.get("text")
    if isinstance(text, str):
        return text
    encoded = event.get("base64")
    if not isinstance(encoded, str):
        return None
    decoded = forgiving_b64decode(encoded)
    if decoded is None:
        return None
    return decoded.decode("utf-8", errors="replace")


def _unwrap_serialized_error(error: Any) -> Any:
    """Unwrap Playwright's ``serializeError`` envelope around a page error."""
    if not isinstance(error, dict):
        return error
    if isinstance(error.get("error"), dict):
        return error["error"]
    if "value" in error:
        return error["value"]
    return error


def _format_location(location: Any, keys: tuple[str, str]) -> str | None:
    """Render ``url:line:column`` when the record carries a usable source location."""
    if not isinstance(location, dict):
        return None
    url = location.get("url")
    if not isinstance(url, str) or not url:
        return None
    line, column = (location.get(key) for key in keys)
    line = line if isinstance(line, int) and not isinstance(line, bool) else 0
    column = column if isinstance(column, int) and not isinstance(column, bool) else 0
    return f"{url}:{line}:{column}"


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
    start = finite_float(start_time)
    wall = finite_float(context.get("wallTime"))
    monotonic = finite_float(context.get("monotonicTime"))
    if start is None or wall is None or monotonic is None:
        return None
    timestamp = wall + (start - monotonic)
    return timestamp if math.isfinite(timestamp) else None
