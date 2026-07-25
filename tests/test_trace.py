import base64
import json
import zipfile
from pathlib import Path

from testexplain.sources.trace import read_trace_actions


def _b64(text: str) -> str:
    """Encode text the way the Playwright runner encodes a Node Buffer chunk."""
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


def _json_lines(*events: object) -> str:
    """Build JSONL exactly like the observed Playwright schema-v8 trace members."""
    lines = [
        event if isinstance(event, str) else json.dumps(event, separators=(",", ":"))
        for event in events
    ]
    return "\n".join(lines) + "\n"


def write_real_derived_trace(path: Path) -> Path:
    """Write a tiny, sanitized trace shaped from a real Playwright 1.58.2 trace.

    The records preserve event names, schema version, clocks, and callId pairing,
    but contain no real URLs, selectors, credentials, source paths, screenshots,
    or network data.
    """
    runner_events = [
        {
            "version": 8,
            "type": "context-options",
            "origin": "testRunner",
            "wallTime": 1_000_000,
            "monotonicTime": 100.0,
        },
        {
            "type": "before",
            "callId": "shared-call",
            "startTime": 120.0,
            "class": "Test",
            "method": "step",
            "title": "runner setup",
            "params": {"phase": "setup"},
        },
        {
            "type": "after",
            "callId": "shared-call",
            "endTime": 130.0,
            "result": {"status": "ok"},
        },
    ]
    browser_events = [
        {
            "version": 8,
            "type": "context-options",
            "origin": "library",
            "wallTime": 2_000_000,
            "monotonicTime": 500.0,
        },
        {
            "type": "before",
            "callId": "shared-call",
            "startTime": 510.0,
            "class": "Frame",
            "method": "click",
            "title": "click safe control",
            "params": {"selector": "[data-testid=sample-control]"},
        },
        {
            "type": "log",
            "callId": "shared-call",
            "time": 511.0,
            "message": "locator resolved to one element",
        },
        {
            "type": "after",
            "callId": "shared-call",
            "endTime": 520.0,
            "result": {"status": "ok"},
        },
        {
            "type": "before",
            "callId": "failed-call",
            "startTime": 530.0,
            "class": "Frame",
            "method": "expect",
            "title": "verify visible state",
            "params": {},
        },
        {
            "type": "after",
            "callId": "failed-call",
            "endTime": 540.0,
            "error": {"name": "Expect", "message": "expected visible state"},
            "result": {"matches": False, "timedOut": True},
        },
        {
            "type": "before",
            "callId": "unfinished-call",
            "startTime": 550.0,
            "class": "Page",
            "method": "waitForEventInfo",
            "title": "wait for safe event",
            "params": {},
        },
        "{not valid json",
        {
            "type": "log",
            "callId": "missing-call",
            "time": 560.0,
            "message": "orphan progress",
        },
        {"type": "after", "callId": "missing-call", "endTime": 570.0},
    ]
    unsupported_events = [
        {
            "version": 999,
            "type": "context-options",
            "wallTime": 3_000_000,
            "monotonicTime": 1.0,
        }
    ]

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("test.trace", _json_lines(*runner_events))
        archive.writestr("0-trace.trace", _json_lines(*browser_events))
        archive.writestr("1-trace.trace", _json_lines(*unsupported_events))
    return path


def test_read_trace_actions_normalizes_independent_schema_v8_streams(tmp_path):
    result = read_trace_actions(write_real_derived_trace(tmp_path / "trace.zip"))

    assert len(result.evidence) == 4

    runner, successful, failed, incomplete = result.evidence
    assert runner.source == "action"
    assert runner.provenance == "trace"
    assert runner.summary == 'runner setup | params={"phase":"setup"} | result={"status":"ok"}'
    assert runner.severity == 1
    assert runner.timestamp_ms == 1_000_020.0

    assert successful.summary == (
        'click safe control | params={"selector":"[data-testid=sample-control]"} '
        '| logs=locator resolved to one element | result={"status":"ok"}'
    )
    assert successful.severity == 1
    assert successful.timestamp_ms == 2_000_010.0

    assert failed.summary == (
        'verify visible state | params={} | result={"matches":false,"timedOut":true} '
        '| error=Expect: expected visible state'
    )
    assert failed.severity == 4
    assert failed.timestamp_ms == 2_000_030.0

    assert incomplete.summary == "wait for safe event | params={} | incomplete"
    assert incomplete.severity == 4
    assert incomplete.timestamp_ms == 2_000_050.0


def test_read_trace_actions_warns_without_discarding_supported_evidence(tmp_path):
    result = read_trace_actions(write_real_derived_trace(tmp_path / "trace.zip"))

    assert len(result.evidence) == 4
    assert any("0-trace.trace line 8" in warning and "malformed JSON" in warning for warning in result.warnings)
    assert any("0-trace.trace" in warning and "orphan log" in warning for warning in result.warnings)
    assert any("0-trace.trace" in warning and "orphan after" in warning for warning in result.warnings)
    assert any("1-trace.trace" in warning and "unsupported trace version 999" in warning for warning in result.warnings)


def test_read_trace_actions_keeps_actions_without_a_usable_clock_anchor(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options"},
        {
            "type": "before",
            "callId": "call@1",
            "startTime": 10.0,
            "title": "action without anchor",
        },
        {"type": "after", "callId": "call@1", "endTime": 20.0},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert len(result.evidence) == 1
    assert result.evidence[0].timestamp_ms is None
    assert result.warnings == ["test.trace: missing usable clock anchor"]


def test_read_trace_actions_returns_warning_for_an_unreadable_zip(tmp_path):
    trace = tmp_path / "trace.zip"
    trace.write_text("not a zip archive")

    result = read_trace_actions(trace)

    assert result.evidence == []
    assert result.warnings == ["trace.zip: unreadable ZIP; skipped"]


def test_read_trace_actions_rejects_a_stream_with_duplicate_or_late_context(tmp_path):
    trace = tmp_path / "trace.zip"
    genuinely_late = [
        {"type": "before", "callId": "early", "title": "before schema"},
        {"version": 8, "type": "context-options", "wallTime": 500, "monotonicTime": 5},
        {"type": "before", "callId": "later", "startTime": 6, "title": "must not survive"},
        {"type": "after", "callId": "later"},
    ]
    accepted_then_rejected = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {"type": "before", "callId": "call@1", "startTime": 11, "title": "discard me"},
        {"type": "after", "callId": "call@1", "result": {"ok": True}},
        {"version": 999, "type": "context-options"},
    ]
    duplicate_context = [
        {"version": 8, "type": "context-options", "wallTime": 2_000, "monotonicTime": 20},
        {"version": 8, "type": "context-options", "wallTime": 3_000, "monotonicTime": 30},
    ]
    healthy = [
        {"version": 8, "type": "context-options", "wallTime": 4_000, "monotonicTime": 40},
        {"type": "before", "callId": "call@2", "startTime": 41, "title": "keep me"},
        {"type": "after", "callId": "call@2"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("genuinely-late.trace", _json_lines(*genuinely_late))
        archive.writestr("late.trace", _json_lines(*accepted_then_rejected))
        archive.writestr("duplicate.trace", _json_lines(*duplicate_context))
        archive.writestr("healthy.trace", _json_lines(*healthy))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == ["keep me"]
    assert any("genuinely-late.trace" in warning and "late context-options" in warning for warning in result.warnings)
    assert any("late.trace" in warning and "duplicate context-options" in warning for warning in result.warnings)
    assert any("duplicate.trace" in warning and "duplicate context-options" in warning for warning in result.warnings)


def test_read_trace_actions_warns_and_skips_events_before_or_without_context(tmp_path):
    trace = tmp_path / "trace.zip"
    before_context = [{"type": "before", "callId": "call@1", "title": "too early"}]
    no_context = [{"type": "before", "callId": "call@2", "title": "no schema"}]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("early.trace", _json_lines(*before_context, {"version": 8, "type": "context-options"}))
        archive.writestr("missing.trace", _json_lines(*no_context))

    result = read_trace_actions(trace)

    assert result.evidence == []
    assert any("early.trace" in warning and "before context-options" in warning for warning in result.warnings)
    assert any("missing.trace" in warning and "missing context-options" in warning for warning in result.warnings)


def test_read_trace_actions_preserves_duplicate_before_and_proves_stream_isolation(tmp_path):
    trace = tmp_path / "trace.zip"
    duplicate_before = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {"type": "before", "callId": "duplicate", "startTime": 11, "title": "first"},
        {"type": "before", "callId": "duplicate", "startTime": 12, "title": "second"},
        {"type": "after", "callId": "duplicate"},
    ]
    first_stream = [
        {"version": 8, "type": "context-options", "wallTime": 2_000, "monotonicTime": 20},
        {"type": "before", "callId": "shared", "startTime": 21, "title": "stream one"},
    ]
    second_stream = [
        {"version": 8, "type": "context-options", "wallTime": 3_000, "monotonicTime": 30},
        {"type": "after", "callId": "shared"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("duplicate.trace", _json_lines(*duplicate_before))
        archive.writestr("first.trace", _json_lines(*first_stream))
        archive.writestr("second.trace", _json_lines(*second_stream))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == [
        "first | incomplete",
        "second",
        "stream one | incomplete",
    ]
    assert any("duplicate before for duplicate" in warning for warning in result.warnings)
    assert any("second.trace" in warning and "orphan after for shared" in warning for warning in result.warnings)


def test_read_trace_actions_preserves_present_empty_fields_and_empty_error(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {"type": "before", "callId": "call@1", "startTime": 11, "title": "empty fields", "params": {}},
        {"type": "after", "callId": "call@1", "result": {}, "error": {}},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert result.evidence[0].summary == "empty fields | params={} | result={} | error={}"
    assert result.evidence[0].severity == 4


def test_read_trace_actions_isolates_a_corrupt_member_and_rejects_nonstandard_numbers(tmp_path):
    trace = tmp_path / "trace.zip"
    corrupt_events = [{"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10}]
    healthy_events = [
        {"version": 8, "type": "context-options", "wallTime": 2_000, "monotonicTime": 20},
        {"type": "before", "callId": "call@1", "startTime": 21, "title": "healthy"},
        {"type": "after", "callId": "call@1"},
        '{"type":"before","callId":"nan","startTime":NaN}',
    ]
    with zipfile.ZipFile(trace, "w", compression=zipfile.ZIP_STORED) as archive:
        archive.writestr("corrupt.trace", _json_lines(*corrupt_events))
        archive.writestr("healthy.trace", _json_lines(*healthy_events))

    with zipfile.ZipFile(trace) as archive:
        info = archive.getinfo("corrupt.trace")
        payload_offset = info.header_offset + 30 + len(info.filename) + len(info.extra)
    with trace.open("r+b") as file:
        file.seek(payload_offset)
        file.write(b"x")

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == ["healthy"]
    assert any("corrupt.trace" in warning and "unreadable trace member" in warning for warning in result.warnings)
    assert any("healthy.trace line 4" in warning and "malformed JSON" in warning for warning in result.warnings)


def test_read_trace_actions_validates_clocks_and_enforces_configurable_limits(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {"type": "before", "callId": "bool", "startTime": True, "title": "bad clock"},
        {"type": "after", "callId": "bool"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("bad-clock.trace", _json_lines(*events))
        archive.writestr("too-many.trace", _json_lines({"version": 8, "type": "context-options"}))

    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBERS", 1)
    result = read_trace_actions(trace)

    assert result.evidence[0].timestamp_ms is None
    assert any("trace member limit" in warning for warning in result.warnings)

    line_limited = tmp_path / "line-limited.zip"
    with zipfile.ZipFile(line_limited, "w") as archive:
        archive.writestr("test.trace", _json_lines({"version": 8, "type": "context-options"}))
    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBERS", 10)
    monkeypatch.setattr(trace_module, "MAX_TRACE_LINE_LENGTH", 10)

    line_result = read_trace_actions(line_limited)

    assert line_result.evidence == []
    assert any("test.trace line 1" in warning and "line length limit" in warning for warning in line_result.warnings)


def test_read_trace_actions_enforces_member_total_event_and_evidence_limits(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    oversized = tmp_path / "oversized.zip"
    with zipfile.ZipFile(oversized, "w") as archive:
        archive.writestr("large.trace", _json_lines({"version": 8, "type": "context-options"}))
    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBER_SIZE", 1)

    oversized_result = read_trace_actions(oversized)

    assert oversized_result.evidence == []
    assert any("large.trace" in warning and "member size limit" in warning for warning in oversized_result.warnings)

    total_limited = tmp_path / "total-limited.zip"
    with zipfile.ZipFile(total_limited, "w") as archive:
        archive.writestr("first.trace", _json_lines({"version": 8, "type": "context-options"}))
        archive.writestr("second.trace", _json_lines({"version": 8, "type": "context-options"}))
    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBER_SIZE", 1_000)
    monkeypatch.setattr(trace_module, "MAX_TRACE_TOTAL_SIZE", 50)

    total_result = read_trace_actions(total_limited)

    assert any("total size limit" in warning for warning in total_result.warnings)

    events_limited = tmp_path / "events-limited.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {"type": "before", "callId": "call@1", "startTime": 11, "title": "first"},
        {"type": "after", "callId": "call@1"},
    ]
    with zipfile.ZipFile(events_limited, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))
    monkeypatch.setattr(trace_module, "MAX_TRACE_TOTAL_SIZE", 1_000)
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 2)

    events_result = read_trace_actions(events_limited)

    assert events_result.evidence == []
    assert any("event limit" in warning for warning in events_result.warnings)

    evidence_limited = tmp_path / "evidence-limited.zip"
    with zipfile.ZipFile(evidence_limited, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 10)
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVIDENCE", 0)

    evidence_result = read_trace_actions(evidence_limited)

    assert evidence_result.evidence == []
    assert any("evidence limit" in warning for warning in evidence_result.warnings)


def test_read_trace_actions_counts_every_physical_line_and_bounds_warnings(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", "bad\n[]\nbad\nbad\n")
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 3)
    monkeypatch.setattr(trace_module, "MAX_TRACE_WARNINGS", 2)

    result = read_trace_actions(trace)

    assert len(result.warnings) <= 3
    assert any("event limit" in warning for warning in result.warnings)
    assert any("warnings suppressed" in warning for warning in result.warnings)


def test_read_trace_actions_accepts_only_exact_integer_schema_version_8(tmp_path):
    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        for name, version in (("float", 8.0), ("string", "8"), ("bool", True), ("missing", None)):
            context = {"type": "context-options"}
            if version is not None:
                context["version"] = version
            archive.writestr(f"{name}.trace", _json_lines(context))
        archive.writestr(
            "valid.trace",
            _json_lines(
                {"version": 8, "type": "context-options", "wallTime": 100, "monotonicTime": 10},
                {"type": "before", "callId": "ok", "startTime": 11, "title": "valid"},
                {"type": "after", "callId": "ok"},
            ),
        )

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == ["valid"]
    assert sum("unsupported trace version" in warning for warning in result.warnings) == 4


def test_read_trace_actions_preserves_empty_title_and_null_error(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 100, "monotonicTime": 10},
        {"type": "before", "callId": "call@1", "startTime": 11, "title": "", "method": "fallback"},
        {"type": "log", "callId": "call@1", "message": ""},
        {"type": "after", "callId": "call@1", "error": None},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert result.evidence[0].summary == " | logs= | error=null"
    assert result.evidence[0].severity == 4


def test_read_trace_actions_isolates_a_corrupt_deflated_member(tmp_path):
    trace = tmp_path / "trace.zip"
    corrupt_payload = _json_lines(
        {"version": 8, "type": "context-options", "wallTime": 100, "monotonicTime": 10}
    ) * 20
    healthy = _json_lines(
        {"version": 8, "type": "context-options", "wallTime": 200, "monotonicTime": 20},
        {"type": "before", "callId": "ok", "startTime": 21, "title": "healthy"},
        {"type": "after", "callId": "ok"},
    )
    with zipfile.ZipFile(trace, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("corrupt.trace", corrupt_payload)
        archive.writestr("healthy.trace", healthy)

    with zipfile.ZipFile(trace) as archive:
        info = archive.getinfo("corrupt.trace")
        payload_offset = info.header_offset + 30 + len(info.filename) + len(info.extra)
    with trace.open("r+b") as file:
        file.seek(payload_offset + max(1, info.compress_size // 2))
        original = file.read(1)
        file.seek(-1, 1)
        file.write(bytes([original[0] ^ 0xFF]))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == ["healthy"]
    assert any("corrupt.trace" in warning and "unreadable trace member" in warning for warning in result.warnings)


def test_read_trace_actions_returns_none_when_timestamp_arithmetic_overflows(tmp_path):
    trace = tmp_path / "trace.zip"
    arithmetic_overflow = [
        {"version": 8, "type": "context-options", "wallTime": 1e308, "monotonicTime": -1e308},
        {"type": "before", "callId": "call@1", "startTime": 1e308, "title": "overflow"},
        {"type": "after", "callId": "call@1"},
    ]
    integer_overflow = [
        {"version": 8, "type": "context-options", "wallTime": 10**400, "monotonicTime": 10},
        {"type": "before", "callId": "call@2", "startTime": 11, "title": "huge integer clock"},
        {"type": "after", "callId": "call@2"},
    ]
    healthy = [
        {"version": 8, "type": "context-options", "wallTime": 300, "monotonicTime": 30},
        {"type": "before", "callId": "call@3", "startTime": 31, "title": "healthy"},
        {"type": "after", "callId": "call@3"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("arithmetic.trace", _json_lines(*arithmetic_overflow))
        archive.writestr("integer.trace", _json_lines(*integer_overflow))
        archive.writestr("healthy.trace", _json_lines(*healthy))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == [
        "overflow",
        "huge integer clock",
        "healthy",
    ]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [None, None, 301.0]
    assert any("integer.trace" in warning and "missing usable clock anchor" in warning for warning in result.warnings)


def test_read_trace_actions_normalizes_console_severity_and_location(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 1_000, "monotonicTime": 10},
        {
            "type": "console",
            "time": 11,
            "messageType": "error",
            "text": "POST /api/profile returned 500",
            "location": {
                "url": "https://app.test/profile.js",
                "lineNumber": 87,
                "columnNumber": 14,
            },
            "pageId": "page@1",
        },
        {
            "type": "console",
            "time": 12,
            "messageType": "warning",
            "text": "slow response",
            "location": {"url": "", "lineNumber": 0, "columnNumber": 0},
        },
        {"type": "console", "time": 13, "messageType": "warn", "text": "deprecated call"},
        {"type": "console", "time": 14, "messageType": "ERROR", "text": "upper case type"},
        {"type": "console", "time": 15, "messageType": "info", "text": "profile loaded"},
        {"type": "console", "time": 16, "messageType": "log", "text": "render complete"},
        {"type": "console", "time": 17, "messageType": "debug", "text": "cache hit"},
        {"type": "console", "time": 18, "messageType": "count", "text": "unknown type"},
        {"type": "console", "time": 19, "text": "type is missing"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [evidence.source for evidence in result.evidence] == ["console"] * 9
    assert {evidence.provenance for evidence in result.evidence} == {"trace"}
    assert [evidence.summary for evidence in result.evidence] == [
        "error: POST /api/profile returned 500 | location=https://app.test/profile.js:87:14",
        "warning: slow response",
        "warn: deprecated call",
        "ERROR: upper case type",
        "info: profile loaded",
        "log: render complete",
        "debug: cache hit",
        "count: unknown type",
        "console: type is missing",
    ]
    assert [evidence.severity for evidence in result.evidence] == [4, 3, 3, 4, 2, 1, 1, 1, 1]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [
        1_001.0,
        1_002.0,
        1_003.0,
        1_004.0,
        1_005.0,
        1_006.0,
        1_007.0,
        1_008.0,
        1_009.0,
    ]
    assert result.warnings == []


def test_read_trace_actions_normalizes_page_errors_from_serialized_events(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 2_000, "monotonicTime": 20},
        {
            "type": "event",
            "time": 21,
            "class": "BrowserContext",
            "method": "pageError",
            "params": {
                "error": {
                    "error": {
                        "name": "TypeError",
                        "message": "Cannot read properties of undefined (reading 'id')",
                        "stack": "at profile.js:87",
                    }
                }
            },
            "pageId": "page@1",
        },
        {
            "type": "event",
            "time": 22,
            "class": "BrowserContext",
            "method": "pageError",
            "params": {
                "error": {"error": {"name": "Error", "message": "with location"}},
                "location": {"url": "https://app.test/profile.js", "line": 87, "column": 14},
            },
        },
        {
            "type": "event",
            "time": 23,
            "class": "BrowserContext",
            "method": "pageError",
            "params": {"error": {"value": {"s": "thrown string"}}},
        },
        {
            "type": "event",
            "time": 24,
            "class": "BrowserContext",
            "method": "pageError",
            "params": {"error": {"name": "FlatError", "message": "already unwrapped"}},
        },
        {
            "type": "event",
            "time": 25,
            "class": "BrowserContext",
            "method": "dialog",
            "params": {"type": "alert", "message": "ignored by task 5"},
        },
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [evidence.source for evidence in result.evidence] == ["page_error"] * 4
    assert [evidence.summary for evidence in result.evidence] == [
        "TypeError: Cannot read properties of undefined (reading 'id')",
        "Error: with location | location=https://app.test/profile.js:87:14",
        '{"s":"thrown string"}',
        "FlatError: already unwrapped",
    ]
    assert [evidence.severity for evidence in result.evidence] == [4, 4, 4, 4]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [
        2_001.0,
        2_002.0,
        2_003.0,
        2_004.0,
    ]
    assert result.warnings == []


def test_read_trace_actions_normalizes_runner_stdout_and_stderr(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 3_000, "monotonicTime": 30},
        {"type": "stdout", "timestamp": 31, "text": "profile payload prepared\n"},
        {"type": "stderr", "timestamp": 32, "text": "save request failed after retry\n"},
        {"type": "stdout", "text": "output without a clock"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [evidence.source for evidence in result.evidence] == ["stdout", "stderr", "stdout"]
    assert [evidence.summary for evidence in result.evidence] == [
        "profile payload prepared\n",
        "save request failed after retry\n",
        "output without a clock",
    ]
    assert [evidence.severity for evidence in result.evidence] == [1, 3, 1]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [3_001.0, 3_002.0, None]
    assert result.warnings == []


def test_read_trace_actions_decodes_base64_output_and_isolates_bad_payloads(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 4_000, "monotonicTime": 40},
        {"type": "stderr", "timestamp": 41, "base64": _b64("save request failed after retry\n")},
        {
            "type": "stdout",
            "timestamp": 42,
            "text": "plain text wins",
            "base64": _b64("ignored"),
        },
        {
            "type": "stdout",
            "timestamp": 43,
            "base64": base64.b64encode(b"\xff\xfebytes").decode("ascii"),
        },
        {"type": "stdout", "timestamp": 44, "base64": "aGVs\nbG8g"},
        {"type": "stdout", "timestamp": 45, "base64": _b64("no padding").rstrip("=")},
        {"type": "stdout", "timestamp": 46, "base64": ""},
        {"type": "stderr", "timestamp": 47, "base64": "not*valid*base64"},
        {"type": "stderr", "timestamp": 48, "base64": "abcde"},
        {"type": "stdout", "timestamp": 49},
        {"type": "stderr", "timestamp": 50, "base64": 12},
        {"type": "stdout", "timestamp": 51, "text": "still parsing after bad payloads"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("test.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == [
        "save request failed after retry\n",
        "plain text wins",
        "\ufffd\ufffdbytes",
        "hello ",
        "no padding",
        "",
        "still parsing after bad payloads",
    ]
    assert [evidence.severity for evidence in result.evidence] == [3, 1, 1, 1, 1, 1, 1]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [
        4_001.0,
        4_002.0,
        4_003.0,
        4_004.0,
        4_005.0,
        4_006.0,
        4_011.0,
    ]
    assert sum("unusable stderr payload" in warning for warning in result.warnings) == 3
    assert sum("unusable stdout payload" in warning for warning in result.warnings) == 1
    assert any("test.trace line 8" in warning and "unusable stderr" in warning for warning in result.warnings)
    assert any("test.trace line 9" in warning and "unusable stderr" in warning for warning in result.warnings)
    assert any("test.trace line 10" in warning and "unusable stdout" in warning for warning in result.warnings)
    assert any("test.trace line 11" in warning and "unusable stderr" in warning for warning in result.warnings)


def test_read_trace_actions_interleaves_output_without_breaking_action_pairing(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        {"version": 8, "type": "context-options", "wallTime": 5_000, "monotonicTime": 50},
        {"type": "stdout", "timestamp": 51, "text": "starting"},
        {"type": "before", "callId": "call@1", "startTime": 52, "title": "click Save"},
        {"type": "console", "time": 53, "messageType": "error", "text": "returned 500"},
        {"type": "log", "callId": "call@1", "time": 54, "message": "locator resolved"},
        {
            "type": "event",
            "time": 55,
            "class": "BrowserContext",
            "method": "pageError",
            "params": {"error": {"error": {"name": "TypeError", "message": "undefined id"}}},
        },
        {"type": "after", "callId": "call@1", "endTime": 56, "result": {"status": "ok"}},
        {"type": "stderr", "timestamp": 57, "text": "save failed"},
        {"type": "before", "callId": "call@2", "startTime": 58, "title": "expect confirmation"},
        {"type": "console", "time": 59, "messageType": "info", "text": "still waiting"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [(evidence.source, evidence.summary) for evidence in result.evidence] == [
        ("stdout", "starting"),
        ("console", "error: returned 500"),
        ("page_error", "TypeError: undefined id"),
        ("action", 'click Save | logs=locator resolved | result={"status":"ok"}'),
        ("stderr", "save failed"),
        ("console", "info: still waiting"),
        ("action", "expect confirmation | incomplete"),
    ]
    assert [evidence.timestamp_ms for evidence in result.evidence] == [
        5_001.0,
        5_003.0,
        5_005.0,
        5_002.0,
        5_007.0,
        5_009.0,
        5_008.0,
    ]
    assert result.warnings == []


def test_read_trace_actions_applies_task_4_safety_rules_to_console_and_output(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    early = [
        {"type": "console", "time": 1, "messageType": "error", "text": "before schema"},
        {"version": 8, "type": "context-options", "wallTime": 100, "monotonicTime": 10},
    ]
    unsupported = [
        {"version": 999, "type": "context-options", "wallTime": 200, "monotonicTime": 20},
        {"type": "stderr", "timestamp": 21, "text": "unsupported stream"},
    ]
    healthy = [
        {"version": 8, "type": "context-options", "wallTime": 300, "monotonicTime": 30},
        {"type": "console", "time": 31, "messageType": "error", "text": "kept"},
        {"type": "stdout", "timestamp": 32, "text": "kept too"},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("early.trace", _json_lines(*early))
        archive.writestr("unsupported.trace", _json_lines(*unsupported))
        archive.writestr("healthy.trace", _json_lines(*healthy))

    result = read_trace_actions(trace)

    assert [evidence.summary for evidence in result.evidence] == ["error: kept", "kept too"]
    assert any("early.trace" in warning and "before context-options" in warning for warning in result.warnings)
    assert any("unsupported.trace" in warning and "unsupported trace version 999" in warning for warning in result.warnings)

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVIDENCE", 1)
    capped = read_trace_actions(trace)

    assert [evidence.summary for evidence in capped.evidence] == ["error: kept"]
    assert any("evidence limit exceeded" in warning for warning in capped.warnings)
