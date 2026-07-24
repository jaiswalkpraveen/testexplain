import json
import zipfile
from pathlib import Path

from testexplain.sources.trace import read_trace_actions


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
