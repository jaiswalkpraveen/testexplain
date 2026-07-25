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


def _snapshot(**overrides: object) -> dict:
    """Build one ``resource-snapshot`` event using harTracer's real sentinel defaults.

    Playwright creates every HAR entry pre-filled with "unknown" sentinels
    (``status: -1``, ``time: -1``, ``mimeType: "x-unknown"``) and overwrites them
    only once the real values arrive. Keyword overrides are routed to the request,
    the response, or the entry itself so each test states just what it varies.
    """
    request = {
        "method": "GET",
        "url": "https://example.test/",
        "httpVersion": "HTTP/1.1",
        "headers": [],
        "cookies": [],
        "queryString": [],
        "headersSize": -1,
        "bodySize": -1,
    }
    response = {
        "status": -1,
        "statusText": "",
        "httpVersion": "HTTP/1.1",
        "headers": [],
        "cookies": [],
        "content": {"size": -1, "mimeType": "x-unknown"},
        "redirectURL": "",
        "headersSize": -1,
        "bodySize": -1,
        "_transferSize": -1,
    }
    snapshot = {
        "_frameref": "frame@1",
        "_monotonicTime": 520.0,
        "cache": {},
        "pageref": "page@1",
        "request": request,
        "response": response,
        "startedDateTime": "2026-07-25T00:00:00.000Z",
        "time": -1,
        "timings": {"send": -1, "wait": -1, "receive": -1},
    }
    for key, value in overrides.items():
        if key in ("method", "url", "postData"):
            request[key] = value
        elif key in ("status", "statusText", "_failureText"):
            response[key] = value
        elif key == "mimeType":
            response["content"]["mimeType"] = value
        else:
            snapshot[key] = value
    return {"type": "resource-snapshot", "snapshot": snapshot}


NETWORK_CONTEXT = {
    "version": 8,
    "type": "context-options",
    "origin": "library",
    "wallTime": 2_000_000,
    "monotonicTime": 500.0,
}


def write_paired_network_trace(
    path: Path,
    *snapshots: object,
    stem: str = "0-trace",
    trace_events: tuple = (),
) -> Path:
    """Write a trace whose ``<stem>.network`` member is paired with ``<stem>.trace``.

    Playwright never stores ``resource-snapshot`` records in the ``.trace`` member.
    It writes them to a sibling ``.network`` stream that carries no
    ``context-options`` header of its own, so the reader must inherit the schema
    version and the clock anchor from the paired ``.trace`` member.
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            f"{stem}.trace", _json_lines(*(trace_events or (NETWORK_CONTEXT,)))
        )
        archive.writestr(f"{stem}.network", _json_lines(*snapshots))
    return path


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


def test_read_trace_actions_normalizes_paired_network_members(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(
            url="https://example.test/dead-endpoint",
            _failureText="net::ERR_UNSAFE_PORT",
            _monotonicTime=501.0,
        ),
        _snapshot(
            url="https://example.test/",
            status=200,
            statusText="OK",
            mimeType="text/html",
            time=1.3,
            _monotonicTime=502.0,
        ),
        _snapshot(
            url="https://example.test/api/slow-ok",
            status=200,
            statusText="OK",
            mimeType="application/json",
            time=351.6,
            _monotonicTime=503.0,
        ),
        _snapshot(
            url="https://example.test/api/missing",
            status=404,
            statusText="Not Found",
            mimeType="application/json",
            time=2.5,
            _monotonicTime=504.0,
        ),
        _snapshot(
            url="https://example.test/api/broken",
            status=500,
            statusText="Internal Server Error",
            mimeType="application/json",
            time=2.3,
            _monotonicTime=505.0,
        ),
    )

    result = read_trace_actions(trace)

    assert [(item.source, item.provenance) for item in result.evidence] == [("network", "trace")] * 5
    assert [item.summary for item in result.evidence] == [
        "GET https://example.test/dead-endpoint | status=unknown | mime=unknown"
        " | time=unknown | failure=net::ERR_UNSAFE_PORT",
        "GET https://example.test/ | status=200 OK | mime=text/html | time=1.3ms",
        "GET https://example.test/api/slow-ok | status=200 OK | mime=application/json | time=351.6ms",
        "GET https://example.test/api/missing | status=404 Not Found | mime=application/json | time=2.5ms",
        "GET https://example.test/api/broken | status=500 Internal Server Error"
        " | mime=application/json | time=2.3ms",
    ]
    assert [item.severity for item in result.evidence] == [4, 1, 1, 3, 4]
    assert result.warnings == []


def test_read_trace_actions_inherits_the_clock_anchor_for_network_members(tmp_path):
    trace = tmp_path / "trace.zip"
    anchored = tmp_path / "anchored.zip"
    write_paired_network_trace(
        trace,
        _snapshot(status=200, statusText="OK", _monotonicTime=520.0),
        _snapshot(status=200, statusText="OK", _monotonicTime=None),
    )

    result = read_trace_actions(trace)

    assert [item.timestamp_ms for item in result.evidence] == [2_000_020.0, None]
    assert result.warnings == []

    write_paired_network_trace(
        anchored,
        _snapshot(status=200, statusText="OK", _monotonicTime=520.0),
        trace_events=({"version": 8, "type": "context-options"},),
    )

    unanchored = read_trace_actions(anchored)

    assert [item.timestamp_ms for item in unanchored.evidence] == [None]
    assert unanchored.warnings == ["0-trace.trace: missing usable clock anchor"]


def test_read_trace_actions_skips_a_network_member_without_a_paired_trace_stream(tmp_path):
    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.network", _json_lines(_snapshot(status=200, statusText="OK")))
        archive.writestr("1-trace.trace", _json_lines(NETWORK_CONTEXT))

    result = read_trace_actions(trace)

    assert result.evidence == []
    assert result.warnings == [
        "0-trace.network: network member has no paired trace stream; skipped"
    ]


def test_read_trace_actions_excludes_network_members_of_an_unusable_trace_stream(tmp_path):
    trace = tmp_path / "trace.zip"
    snapshot = _snapshot(status=500, statusText="Internal Server Error", time=1.0)
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr(
            "0-trace.trace",
            _json_lines({"version": 999, "type": "context-options", "wallTime": 1, "monotonicTime": 1}),
        )
        archive.writestr("0-trace.network", _json_lines(snapshot))
        archive.writestr("1-trace.trace", _json_lines({"type": "console", "time": 1, "text": "no context"}))
        archive.writestr("1-trace.network", _json_lines(snapshot))
        archive.writestr("2-trace.trace", _json_lines(NETWORK_CONTEXT))
        archive.writestr("2-trace.network", _json_lines(snapshot))

    result = read_trace_actions(trace)

    assert [item.source for item in result.evidence] == ["network"]
    assert result.evidence[0].timestamp_ms == 2_000_020.0
    assert any("unsupported trace version 999" in warning for warning in result.warnings)
    assert any("1-trace.trace: missing context-options" in warning for warning in result.warnings)
    assert not any("network member has no paired trace stream" in warning for warning in result.warnings)


def test_read_trace_actions_flags_transport_failures_and_aborted_requests(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(method="POST", url="https://example.test/gone", _failureText="net::ERR_CONNECTION_REFUSED"),
        _snapshot(url="https://example.test/cancelled", _wasAborted=True),
        _snapshot(url="https://example.test/mocked", status=200, statusText="OK", time=0.0, _wasFulfilled=True),
        _snapshot(url="https://example.test/passthrough", status=200, statusText="OK", time=0.0, _wasContinued=True),
    )

    result = read_trace_actions(trace)

    assert [item.summary for item in result.evidence] == [
        "POST https://example.test/gone | status=unknown | mime=unknown"
        " | time=unknown | failure=net::ERR_CONNECTION_REFUSED",
        "GET https://example.test/cancelled | status=unknown | mime=unknown | time=unknown | aborted",
        "GET https://example.test/mocked | status=200 OK | mime=unknown | time=0.0ms | fulfilled",
        "GET https://example.test/passthrough | status=200 OK | mime=unknown | time=0.0ms | continued",
    ]
    assert [item.severity for item in result.evidence] == [4, 3, 1, 1]
    assert result.warnings == []


def test_read_trace_actions_ranks_network_severity_by_status_class(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(status=500, statusText="Internal Server Error"),
        _snapshot(status=404, statusText="Not Found"),
        _snapshot(status=302, statusText="Found"),
        _snapshot(status=200, statusText="OK"),
        _snapshot(status=101, statusText="Switching Protocols"),
        _snapshot(status=-1),
        _snapshot(status="200"),
        _snapshot(status=True),
    )

    result = read_trace_actions(trace)

    assert [item.severity for item in result.evidence] == [4, 3, 2, 1, 1, 2, 2, 2]
    assert [item.summary.split(" | ")[1] for item in result.evidence] == [
        "status=500 Internal Server Error",
        "status=404 Not Found",
        "status=302 Found",
        "status=200 OK",
        "status=101 Switching Protocols",
        "status=unknown",
        "status=unknown",
        "status=unknown",
    ]
    assert result.warnings == []


def test_read_trace_actions_reports_unknown_network_timing_and_mime_type(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(status=200, statusText="OK", time=-1, mimeType="x-unknown"),
        _snapshot(status=200, statusText="OK", time=0, mimeType="text/css"),
        _snapshot(status=200, statusText="OK", time=12.34, mimeType=""),
        _snapshot(status=200, statusText="OK", time="12", mimeType=7),
        _snapshot(status=200, statusText="OK", time=True, mimeType="application/json; charset=utf-8"),
    )

    result = read_trace_actions(trace)

    assert [item.summary.split(" | ")[2:] for item in result.evidence] == [
        ["mime=unknown", "time=unknown"],
        ["mime=text/css", "time=0.0ms"],
        ["mime=unknown", "time=12.3ms"],
        ["mime=unknown", "time=unknown"],
        ["mime=application/json; charset=utf-8", "time=unknown"],
    ]
    assert result.warnings == []


def test_read_trace_actions_sanitizes_network_urls(tmp_path):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    long_url = "https://example.test/" + "a" * 400
    write_paired_network_trace(
        trace,
        _snapshot(url="https://user:secret@example.test/private"),
        _snapshot(url="https://example.test/search?token=abcdef&id=7&flag"),
        _snapshot(url="https://example.test/page#access_token=leaked"),
        _snapshot(url=long_url),
        _snapshot(url=""),
        _snapshot(url=None),
        _snapshot(url="http://[::1"),
        _snapshot(url="https://user:secret@[::1?token=abcdef"),
    )

    result = read_trace_actions(trace)
    urls = [item.summary.split(" | ")[0].removeprefix("GET ") for item in result.evidence]

    assert urls[0] == "https://***@example.test/private"
    assert urls[1] == "https://example.test/search?token=<redacted>&id=<redacted>&flag"
    assert urls[2] == "https://example.test/page"
    assert urls[3] == long_url[: trace_module.MAX_NETWORK_URL_LENGTH] + "\u2026"
    assert len(urls[3]) == trace_module.MAX_NETWORK_URL_LENGTH + 1
    assert urls[4] == "unknown"
    assert urls[5] == "unknown"
    assert urls[6] == "http://[::1"
    assert "secret" not in urls[7] and "abcdef" not in urls[7]
    assert result.warnings == []


def test_read_trace_actions_isolates_malformed_network_records(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        {"type": "resource-snapshot", "snapshot": None},
        {"type": "resource-snapshot", "snapshot": []},
        {"type": "resource-snapshot"},
        {"type": "resource-snapshot", "snapshot": {"response": {"status": 200}}},
        {"type": "resource-snapshot", "snapshot": {"request": ["GET"]}},
        {"type": "resource-snapshot", "snapshot": {"request": {"method": "HEAD", "url": "https://example.test/a"}}},
        {
            "type": "resource-snapshot",
            "snapshot": {"request": {"method": 5, "url": 7}, "response": "broken", "time": 1.0},
        },
        "{not valid json",
        [],
        '{"type":"resource-snapshot","snapshot":{"request":{"method":"GET","url":"https://example.test/b"},"_monotonicTime":NaN}}',
    )

    result = read_trace_actions(trace)

    assert [item.summary for item in result.evidence] == [
        "HEAD https://example.test/a | status=unknown | mime=unknown | time=unknown",
        "UNKNOWN unknown | status=unknown | mime=unknown | time=1.0ms",
    ]
    assert sum("unusable resource-snapshot payload" in warning for warning in result.warnings) == 5
    assert any("0-trace.network line 8" in warning and "malformed JSON" in warning for warning in result.warnings)
    assert any("0-trace.network line 9" in warning and "expected JSON object" in warning for warning in result.warnings)
    assert any("0-trace.network line 10" in warning and "malformed JSON" in warning for warning in result.warnings)


def test_read_trace_actions_applies_safety_limits_to_network_members(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(url="https://example.test/first", status=200, statusText="OK"),
        _snapshot(url="https://example.test/second", status=200, statusText="OK"),
    )

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVIDENCE", 1)
    capped = read_trace_actions(trace)

    assert len(capped.evidence) == 1
    assert "https://example.test/first" in capped.evidence[0].summary
    assert any("evidence limit exceeded" in warning for warning in capped.warnings)

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVIDENCE", 50_000)
    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 1)
    event_capped = read_trace_actions(trace)

    assert event_capped.evidence == []
    assert any("0-trace.network" in warning and "event limit" in warning for warning in event_capped.warnings)

    monkeypatch.setattr(trace_module, "MAX_TRACE_EVENTS", 100_000)
    monkeypatch.setattr(trace_module, "MAX_TRACE_LINE_LENGTH", 200)
    line_capped = read_trace_actions(trace)

    assert line_capped.evidence == []
    assert any(
        "0-trace.network line 1" in warning and "line length limit" in warning
        for warning in line_capped.warnings
    )

    monkeypatch.setattr(trace_module, "MAX_TRACE_LINE_LENGTH", 1024 * 1024)
    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBER_SIZE", 200)
    size_capped = read_trace_actions(trace)

    assert size_capped.evidence == []
    assert any(
        "0-trace.network" in warning and "member size limit" in warning
        for warning in size_capped.warnings
    )


def test_read_trace_actions_keeps_network_streams_isolated_per_ordinal(tmp_path):
    trace = tmp_path / "trace.zip"
    first_context = {"version": 8, "type": "context-options", "wallTime": 1_000_000, "monotonicTime": 100.0}
    second_context = {"version": 8, "type": "context-options", "wallTime": 5_000_000, "monotonicTime": 900.0}
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(first_context))
        archive.writestr("0-trace.network", _json_lines(_snapshot(status=200, statusText="OK", _monotonicTime=110.0)))
        archive.writestr("1-trace.trace", _json_lines(second_context))
        archive.writestr("1-trace.network", _json_lines(_snapshot(status=200, statusText="OK", _monotonicTime=910.0)))

    result = read_trace_actions(trace)

    assert [item.timestamp_ms for item in result.evidence] == [1_000_010.0, 5_000_010.0]
    assert result.warnings == []


def test_read_trace_actions_parses_inline_resource_snapshots(tmp_path):
    trace = tmp_path / "trace.zip"
    events = [
        NETWORK_CONTEXT,
        {"type": "before", "callId": "call@1", "startTime": 510.0, "title": "navigate"},
        _snapshot(url="https://example.test/inline", status=503, statusText="Service Unavailable", time=4.0),
        {"type": "after", "callId": "call@1", "endTime": 520.0, "result": {"status": "ok"}},
    ]
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(*events))

    result = read_trace_actions(trace)

    assert [(item.source, item.severity) for item in result.evidence] == [("network", 4), ("action", 1)]
    assert result.evidence[0].summary == (
        "GET https://example.test/inline | status=503 Service Unavailable | mime=unknown | time=4.0ms"
    )
    assert result.warnings == []


def test_read_trace_actions_bounds_the_number_of_network_members(tmp_path, monkeypatch):
    import testexplain.sources.trace as trace_module

    trace = tmp_path / "trace.zip"
    snapshot = _snapshot(status=200, statusText="OK")
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(NETWORK_CONTEXT))
        archive.writestr("0-trace.network", _json_lines(snapshot))
        archive.writestr("9-orphan.network", _json_lines(snapshot))

    monkeypatch.setattr(trace_module, "MAX_TRACE_MEMBERS", 1)
    result = read_trace_actions(trace)

    assert [item.source for item in result.evidence] == ["network"]
    assert any("network member limit exceeded" in warning for warning in result.warnings)
    assert not any("no paired trace stream" in warning for warning in result.warnings)


def test_read_trace_actions_pairs_network_members_by_exact_ordinal(tmp_path):
    trace = tmp_path / "trace.zip"
    snapshot = _snapshot(status=200, statusText="OK")
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(NETWORK_CONTEXT))
        archive.writestr("10-trace.network", _json_lines(snapshot))
        archive.writestr("resources/0-trace.network", _json_lines(snapshot))
        archive.writestr(".network", _json_lines(snapshot))

    result = read_trace_actions(trace)

    assert result.evidence == []
    assert sorted(result.warnings) == [
        ".network: network member has no paired trace stream; skipped",
        "10-trace.network: network member has no paired trace stream; skipped",
        "resources/0-trace.network: network member has no paired trace stream; skipped",
    ]


def test_read_trace_actions_keeps_network_evidence_when_a_sibling_member_is_unreadable(tmp_path):
    trace = tmp_path / "trace.zip"
    with zipfile.ZipFile(trace, "w") as archive:
        archive.writestr("0-trace.trace", _json_lines(NETWORK_CONTEXT))
        archive.writestr(
            "0-trace.network",
            _json_lines(_snapshot(url="https://example.test/kept", status=200, statusText="OK")),
        )
        archive.writestr("1-trace.trace", _json_lines(NETWORK_CONTEXT))
        archive.writestr(
            "1-trace.network",
            _json_lines(_snapshot(url="https://example.test/lost", status=200, statusText="OK")),
            compress_type=zipfile.ZIP_STORED,
        )

    with zipfile.ZipFile(trace) as archive:
        info = archive.getinfo("1-trace.network")
    payload = info.header_offset + 30 + len(info.filename) + len(info.extra)
    raw = bytearray(trace.read_bytes())
    raw[payload : payload + 1] = b"\x00"
    trace.write_bytes(bytes(raw))

    result = read_trace_actions(trace)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/kept"
    ]
    assert any("1-trace.network" in warning for warning in result.warnings)


def test_read_trace_actions_redacts_opaque_and_pathological_urls(tmp_path):
    trace = tmp_path / "trace.zip"
    write_paired_network_trace(
        trace,
        _snapshot(url="#access_token=leaked"),
        _snapshot(url="data:text/plain;base64,c2VjcmV0"),
        _snapshot(url="javascript:alert('secret')"),
        _snapshot(url="/api/profile?token=secret"),
        _snapshot(url="\\\\user:secret@host\\share"),
        _snapshot(url="https://example.test/@scope/pkg"),
        _snapshot(url="blob:https://example.test/9f8e"),
    )

    result = read_trace_actions(trace)
    urls = [item.summary.split(" | ")[0].removeprefix("GET ") for item in result.evidence]

    assert urls == [
        "unknown",
        "data:<redacted>",
        "javascript:<redacted>",
        "/api/profile?token=<redacted>",
        "\\\\***@host\\share",
        "https://example.test/@scope/pkg",
        "blob:<redacted>",
    ]
    assert not any("secret" in url for url in urls)
    assert result.warnings == []
