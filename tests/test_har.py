import base64
import json
import zipfile
from pathlib import Path

import pytest

from testexplain.sources.har import read_har


def _b64(payload: bytes | str) -> str:
    """Encode a body the way Playwright encodes a non-textual HAR response."""
    raw = payload.encode("utf-8") if isinstance(payload, str) else payload
    return base64.b64encode(raw).decode("ascii")


def _entry(**overrides: object) -> dict:
    """Build one HAR entry using harTracer's real sentinel defaults.

    Playwright pre-fills every entry with "unknown" sentinels (``status: -1``,
    ``time: -1``, ``mimeType: "x-unknown"``) and overwrites them only once the
    real values arrive. Keyword overrides are routed to the request, the
    response, the response content, or the entry itself so each test states just
    what it varies.
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
    content = {"size": -1, "mimeType": "x-unknown"}
    response = {
        "status": -1,
        "statusText": "",
        "httpVersion": "HTTP/1.1",
        "headers": [],
        "cookies": [],
        "content": content,
        "redirectURL": "",
        "headersSize": -1,
        "bodySize": -1,
        "_transferSize": -1,
    }
    entry = {
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
        elif key in ("mimeType", "text", "encoding", "_file", "size"):
            content[key] = value
        else:
            entry[key] = value
    return entry


def _har(*entries: object) -> dict:
    """Wrap entries in the HAR 1.2 envelope Playwright writes."""
    return {
        "log": {
            "version": "1.2",
            "creator": {"name": "Playwright", "version": "1.58.2"},
            "browser": {"name": "chromium", "version": "140.0"},
            "entries": list(entries),
        }
    }


def write_har(path: Path, *entries: object) -> Path:
    """Write a plain ``.har`` file, the shape produced by ``content: 'embed'``."""
    path.write_text(json.dumps(_har(*entries), separators=(",", ":")), encoding="utf-8")
    return path


def write_har_zip(
    path: Path,
    *entries: object,
    har_name: str | None = "har.har",
    members: dict | None = None,
    raw: str | None = None,
    stored: tuple = (),
) -> Path:
    """Write a HAR ZIP, the shape produced by ``content: 'attach'``.

    Playwright names the HAR member ``har.har`` at the archive root and stores
    each response body as a sibling member whose name is the ``content._file``
    value, so ``members`` maps those names to their payloads.
    """
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        if har_name is not None:
            payload = raw if raw is not None else json.dumps(_har(*entries), separators=(",", ":"))
            archive.writestr(har_name, payload)
        for name, blob in (members or {}).items():
            compression = zipfile.ZIP_STORED if name in stored else zipfile.ZIP_DEFLATED
            archive.writestr(name, blob, compress_type=compression)
    return path


def corrupt_member(path: Path, member: str) -> Path:
    """Flip one payload byte so reading that member fails its CRC check."""
    with zipfile.ZipFile(path) as archive:
        info = archive.getinfo(member)
        offset = info.header_offset + 30 + len(info.filename) + len(info.extra)
    raw = bytearray(path.read_bytes())
    raw[offset] = raw[offset] ^ 0xFF
    path.write_bytes(bytes(raw))
    return path


def test_read_har_normalizes_a_plain_har_document(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(method="POST", url="https://example.test/login", status=500, statusText="Internal Server Error", mimeType="application/json", time=12.34),
        _entry(url="https://example.test/app.css", status=200, statusText="OK", mimeType="text/css", time=3.0),
    )

    result = read_har(har)

    assert [item.summary for item in result.evidence] == [
        "POST https://example.test/login | status=500 Internal Server Error"
        " | mime=application/json | time=12.3ms",
        "GET https://example.test/app.css | status=200 OK | mime=text/css | time=3.0ms",
    ]
    assert [item.severity for item in result.evidence] == [4, 1]
    assert [(item.source, item.provenance) for item in result.evidence] == [
        ("network", "har"),
        ("network", "har"),
    ]
    assert [item.timestamp_ms for item in result.evidence] == [1_784_937_600_000.0] * 2
    assert result.warnings == []


def test_read_har_normalizes_a_har_zip_document(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(url="https://example.test/api", status=503, statusText="Service Unavailable", mimeType="application/json", time=8.0, _file="body.json"),
        _entry(url="https://example.test/ok", status=200, statusText="OK", mimeType="text/html", time=1.0),
        members={"body.json": '{"error":"upstream down"}'},
    )

    result = read_har(archive)

    assert [item.summary for item in result.evidence] == [
        "GET https://example.test/api | status=503 Service Unavailable"
        ' | mime=application/json | time=8.0ms | body={"error":"upstream down"}',
        "GET https://example.test/ok | status=200 OK | mime=text/html | time=1.0ms",
    ]
    assert [item.provenance for item in result.evidence] == ["har", "har"]
    assert result.warnings == []


def test_read_har_finds_the_har_member_by_suffix(tmp_path):
    archive = write_har_zip(
        tmp_path / "bundle.zip",
        _entry(url="https://example.test/found", status=200, statusText="OK"),
        har_name="recordings/session.har",
    )

    result = read_har(archive)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/found"
    ]
    assert result.warnings == []


def test_read_har_prefers_the_canonical_har_member(tmp_path):
    archive = tmp_path / "network.har.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr(
            "aaa.har",
            json.dumps(_har(_entry(url="https://example.test/decoy", status=200, statusText="OK"))),
        )
        handle.writestr(
            "har.har",
            json.dumps(_har(_entry(url="https://example.test/canonical", status=200, statusText="OK"))),
        )

    result = read_har(archive)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/canonical"
    ]
    assert result.warnings == []


def test_read_har_warns_when_a_zip_has_no_har_member(tmp_path):
    archive = write_har_zip(tmp_path / "bundle.zip", har_name=None, members={"body.json": "{}"})

    result = read_har(archive)

    assert result.evidence == []
    assert result.warnings == ["bundle.zip: no .har member found; skipped"]


def test_read_har_renders_playwright_sentinels_as_unknown(tmp_path):
    har = write_har(tmp_path / "network.har", _entry())

    result = read_har(har)

    assert [item.summary for item in result.evidence] == [
        "GET https://example.test/ | status=unknown | mime=unknown | time=unknown"
    ]
    assert [item.severity for item in result.evidence] == [2]
    assert result.warnings == []


def test_read_har_treats_zero_time_as_a_real_measurement(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=200, statusText="OK", time=0),
        _entry(status=200, statusText="OK", time=-1),
    )

    result = read_har(har)

    assert [item.summary.split(" | ")[3] for item in result.evidence] == [
        "time=0.0ms",
        "time=unknown",
    ]
    assert result.warnings == []


def test_read_har_reports_duration_from_time_regardless_of_timings(tmp_path):
    missing_timings = _entry(status=200, statusText="OK", time=5.0)
    del missing_timings["timings"]
    partial_timings = _entry(status=200, statusText="OK", time=7.5)
    partial_timings["timings"] = {"send": 1.0}
    no_time = _entry(status=200, statusText="OK")
    del no_time["time"]
    broken_timings = _entry(status=200, statusText="OK", time=2.0, timings="broken")

    har = write_har(tmp_path / "network.har", missing_timings, partial_timings, no_time, broken_timings)

    result = read_har(har)

    assert [item.summary.split(" | ")[3] for item in result.evidence] == [
        "time=5.0ms",
        "time=7.5ms",
        "time=unknown",
        "time=2.0ms",
    ]
    assert result.warnings == []


def test_read_har_flags_transport_failures_and_aborted_requests(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(method="POST", url="https://example.test/gone", _failureText="net::ERR_CONNECTION_REFUSED"),
        _entry(url="https://example.test/cancelled", _wasAborted=True),
        _entry(url="https://example.test/mocked", status=200, statusText="OK", time=0.0, _wasFulfilled=True),
        _entry(url="https://example.test/passthrough", status=200, statusText="OK", time=0.0, _wasContinued=True),
    )

    result = read_har(har)

    assert [item.summary for item in result.evidence] == [
        "POST https://example.test/gone | status=unknown | mime=unknown"
        " | time=unknown | failure=net::ERR_CONNECTION_REFUSED",
        "GET https://example.test/cancelled | status=unknown | mime=unknown | time=unknown | aborted",
        "GET https://example.test/mocked | status=200 OK | mime=unknown | time=0.0ms | fulfilled",
        "GET https://example.test/passthrough | status=200 OK | mime=unknown | time=0.0ms | continued",
    ]
    assert [item.severity for item in result.evidence] == [4, 3, 1, 1]
    assert result.warnings == []


def test_read_har_ranks_severity_by_status_class(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error"),
        _entry(status=404, statusText="Not Found"),
        _entry(status=302, statusText="Found"),
        _entry(status=200, statusText="OK"),
        _entry(status=101, statusText="Switching Protocols"),
        _entry(status=-1),
        _entry(status="200"),
        _entry(status=True),
    )

    result = read_har(har)

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


def test_read_har_sanitizes_urls(tmp_path):
    import testexplain.sources.har as har_module

    long_url = "https://example.test/" + "a" * 400
    har = write_har(
        tmp_path / "network.har",
        _entry(url="https://user:secret@example.test/private"),
        _entry(url="https://example.test/search?token=abcdef&id=7&flag"),
        _entry(url="https://example.test/page#access_token=leaked"),
        _entry(url=long_url),
        _entry(url=""),
        _entry(url=None),
        _entry(url="data:text/plain;base64,c2VjcmV0"),
        _entry(url="blob:https://example.test/9f8e"),
    )

    result = read_har(har)
    urls = [item.summary.split(" | ")[0].removeprefix("GET ") for item in result.evidence]

    assert urls == [
        "https://***@example.test/private",
        "https://example.test/search?token=<redacted>&id=<redacted>&flag",
        "https://example.test/page",
        long_url[: har_module.MAX_NETWORK_URL_LENGTH] + "\u2026",
        "unknown",
        "unknown",
        "data:<redacted>",
        "blob:<redacted>",
    ]
    assert not any("secret" in url or "leaked" in url for url in urls)
    assert result.warnings == []


def test_read_har_converts_started_date_time_to_epoch_milliseconds(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00.000Z"),
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00Z"),
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00+05:30"),
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00"),
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00.500Z"),
    )

    result = read_har(har)

    stamps = [item.timestamp_ms for item in result.evidence]
    assert stamps[0] == 1_784_937_600_000.0
    assert stamps[1] == 1_784_937_600_000.0
    assert stamps[2] == 1_784_917_800_000.0
    assert stamps[3] == 1_784_937_600_000.0
    assert stamps[4] == pytest.approx(1_784_937_600_500.0)
    assert result.warnings == []


def test_read_har_degrades_unusable_started_date_time_to_no_timestamp(tmp_path):
    missing = _entry(status=200, statusText="OK")
    del missing["startedDateTime"]

    har = write_har(
        tmp_path / "network.har",
        missing,
        _entry(status=200, statusText="OK", startedDateTime="not-a-date"),
        _entry(status=200, statusText="OK", startedDateTime=""),
        _entry(status=200, statusText="OK", startedDateTime=1_784_937_600_000),
        _entry(status=200, statusText="OK", startedDateTime=None),
        _entry(status=200, statusText="OK", startedDateTime="2026-13-45T99:99:99Z"),
        _entry(status=200, statusText="OK", startedDateTime="+010000-01-01T00:00:00Z"),
    )

    result = read_har(har)

    assert [item.timestamp_ms for item in result.evidence] == [None] * 7
    assert len(result.evidence) == 7
    assert result.warnings == []


def test_read_har_tolerates_nanosecond_precision_started_date_time(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=200, statusText="OK", startedDateTime="2026-07-25T00:00:00.000000000Z"),
    )

    result = read_har(har)

    # Nanosecond fractions parse on newer CPython and are rejected on older
    # ones, so either outcome is acceptable as long as nothing raises.
    assert result.evidence[0].timestamp_ms in (None, 1_784_937_600_000.0)
    assert result.warnings == []


def test_read_har_previews_inline_failed_response_bodies(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(url="https://example.test/boom", status=500, statusText="Internal Server Error", mimeType="application/json", time=1.0, text='{"error":"boom"}'),
        _entry(url="https://example.test/refused", _failureText="net::ERR_CONNECTION_REFUSED", text="partial response"),
    )

    result = read_har(har)

    assert [item.summary.split(" | ")[-1] for item in result.evidence] == [
        'body={"error":"boom"}',
        "body=partial response",
    ]
    assert result.warnings == []


def test_read_har_decodes_base64_failed_response_bodies(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error", text=_b64("encoded failure"), encoding="base64"),
        _entry(status=500, statusText="Internal Server Error", text="plain failure", encoding="utf-8"),
    )

    result = read_har(har)

    assert [item.summary.split(" | ")[-1] for item in result.evidence] == [
        "body=encoded failure",
        "body=plain failure",
    ]
    assert result.warnings == []


def test_read_har_warns_on_malformed_base64_bodies(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error", text="!!!!", encoding="base64"),
        _entry(status=500, statusText="Internal Server Error", text="A", encoding="base64"),
        _entry(status=500, statusText="Internal Server Error", text=7),
        _entry(status=500, statusText="Internal Server Error", text=""),
    )

    result = read_har(har)

    assert [item.summary.split(" | ")[-1] for item in result.evidence] == ["time=unknown"] * 4
    assert sum("malformed base64 body" in warning for warning in result.warnings) == 2
    assert result.warnings == [
        "network.har entry 1: malformed base64 body; body skipped",
        "network.har entry 2: malformed base64 body; body skipped",
    ]


def test_read_har_replaces_undecodable_body_bytes(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error", text=_b64(b"\xff\xfe\x00\x01"), encoding="base64"),
    )

    result = read_har(har)

    assert result.evidence[0].summary.endswith("body=\ufffd\ufffd\x00\x01")
    assert result.warnings == []


def test_read_har_collapses_whitespace_in_body_previews(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=502, statusText="Bad Gateway", text="line one\n\n  line two\ttab  "),
        _entry(status=502, statusText="Bad Gateway", text="   \n\t  "),
    )

    result = read_har(har)

    assert result.evidence[0].summary.endswith("body=line one line two tab")
    assert "body=" not in result.evidence[1].summary
    assert result.warnings == []


def test_read_har_truncates_body_previews_at_the_limit(tmp_path):
    import testexplain.sources.har as har_module

    limit = har_module.MAX_HAR_BODY_PREVIEW
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error", text="x" * (limit - 1)),
        _entry(status=500, statusText="Internal Server Error", text="x" * limit),
        _entry(status=500, statusText="Internal Server Error", text="x" * (limit + 1)),
    )

    result = read_har(har)
    previews = [item.summary.split(" | ")[-1].removeprefix("body=") for item in result.evidence]

    assert previews[0] == "x" * (limit - 1)
    assert previews[1] == "x" * limit
    assert previews[2] == "x" * limit + "\u2026"
    assert len(previews[2]) == limit + 1
    assert result.warnings == []


def test_read_har_previews_bodies_only_for_failed_requests(tmp_path):
    har = write_har(
        tmp_path / "network.har",
        _entry(status=200, statusText="OK", text="successful body"),
        _entry(status=399, statusText="Almost", text="redirect body"),
        _entry(status=400, statusText="Bad Request", text="client error body"),
        _entry(status=-1, text="unknown status body"),
    )

    result = read_har(har)
    summaries = [item.summary for item in result.evidence]

    assert "body=" not in summaries[0]
    assert "body=" not in summaries[1]
    assert summaries[2].endswith("body=client error body")
    assert "body=" not in summaries[3]
    assert result.warnings == []


def test_read_har_reads_external_body_members_from_a_har_zip(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.json"),
        members={"a1b2.json": '{"detail":"external body"}'},
    )

    result = read_har(archive)

    assert result.evidence[0].summary.endswith('body={"detail":"external body"}')
    assert result.warnings == []


def test_read_har_prefers_the_external_body_member_over_inline_text(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.txt", text="inline text"),
        members={"a1b2.txt": "external text"},
    )

    result = read_har(archive)

    assert result.evidence[0].summary.endswith("body=external text")
    assert result.warnings == []


def test_read_har_ignores_content_encoding_for_external_body_members(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.bin", encoding="base64"),
        members={"a1b2.bin": _b64("looks like base64 but is stored raw")},
    )

    result = read_har(archive)

    assert result.evidence[0].summary.endswith(f"body={_b64('looks like base64 but is stored raw')}")
    assert result.warnings == []


def test_read_har_warns_when_an_external_body_member_is_missing(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(url="https://example.test/lost", status=500, statusText="Internal Server Error", _file="missing.json"),
    )

    result = read_har(archive)

    assert [item.summary for item in result.evidence] == [
        "GET https://example.test/lost | status=500 Internal Server Error"
        " | mime=unknown | time=unknown"
    ]
    assert result.warnings == [
        "network.har.zip/har.har entry 1: body member 'missing.json' not found; body skipped"
    ]


def test_read_har_warns_when_an_external_body_member_is_unreadable(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(url="https://example.test/kept", status=500, statusText="Internal Server Error", _file="a1b2.json"),
        members={"a1b2.json": '{"detail":"this payload gets corrupted"}'},
        stored=("a1b2.json",),
    )
    corrupt_member(archive, "a1b2.json")

    result = read_har(archive)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/kept"
    ]
    assert "body=" not in result.evidence[0].summary
    assert any("unreadable body member 'a1b2.json'" in warning for warning in result.warnings)


def test_read_har_warns_when_a_plain_har_points_at_an_external_body_member(tmp_path):
    (tmp_path / "a1b2.json").write_text('{"detail":"sibling"}', encoding="utf-8")
    har = write_har(
        tmp_path / "network.har",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.json"),
    )

    result = read_har(har)

    assert "body=" not in result.evidence[0].summary
    assert result.warnings == [
        "network.har entry 1: external body member 'a1b2.json' requires a HAR ZIP; body skipped"
    ]


def test_read_har_rejects_documents_that_are_not_json(tmp_path):
    har = tmp_path / "network.har"
    har.write_text("{not valid json", encoding="utf-8")

    result = read_har(har)

    assert result.evidence == []
    assert result.warnings == ["network.har: malformed JSON; skipped"]


def test_read_har_rejects_documents_that_are_not_objects(tmp_path):
    for payload in ("[]", '"text"', "5", "null"):
        har = tmp_path / "network.har"
        har.write_text(payload, encoding="utf-8")

        result = read_har(har)

        assert result.evidence == []
        assert result.warnings == ["network.har: expected JSON object; skipped"]


def test_read_har_rejects_documents_without_a_log_object(tmp_path):
    for payload in ("{}", '{"log":null}', '{"log":[]}', '{"log":"text"}'):
        har = tmp_path / "network.har"
        har.write_text(payload, encoding="utf-8")

        result = read_har(har)

        assert result.evidence == []
        assert result.warnings == ["network.har: missing log object; skipped"]


def test_read_har_rejects_documents_without_an_entries_list(tmp_path):
    for payload in ('{"log":{}}', '{"log":{"entries":null}}', '{"log":{"entries":{}}}'):
        har = tmp_path / "network.har"
        har.write_text(payload, encoding="utf-8")

        result = read_har(har)

        assert result.evidence == []
        assert result.warnings == ["network.har: missing log.entries list; skipped"]


def test_read_har_rejects_non_finite_json_numbers(tmp_path):
    har = tmp_path / "network.har"
    har.write_text('{"log":{"entries":[{"time":NaN}]}}', encoding="utf-8")

    result = read_har(har)

    assert result.evidence == []
    assert result.warnings == ["network.har: malformed JSON; skipped"]


def test_read_har_accepts_an_empty_entries_list(tmp_path):
    har = write_har(tmp_path / "network.har")

    result = read_har(har)

    assert result.evidence == []
    assert result.warnings == []


def test_read_har_isolates_unusable_entries(tmp_path):
    har = tmp_path / "network.har"
    document = _har(
        None,
        [],
        "entry",
        {},
        {"request": {"method": "HEAD", "url": "https://example.test/a"}},
        {"request": ["GET"], "response": "broken", "time": 1.0},
        _entry(url="https://example.test/kept", status=200, statusText="OK", time=2.0),
    )
    har.write_text(json.dumps(document, separators=(",", ":")), encoding="utf-8")

    result = read_har(har)

    assert [item.summary for item in result.evidence] == [
        "UNKNOWN unknown | status=unknown | mime=unknown | time=unknown",
        "HEAD https://example.test/a | status=unknown | mime=unknown | time=unknown",
        "UNKNOWN unknown | status=unknown | mime=unknown | time=1.0ms",
        "GET https://example.test/kept | status=200 OK | mime=unknown | time=2.0ms",
    ]
    assert result.warnings == [
        "network.har entry 1: unusable HAR entry; skipped",
        "network.har entry 2: unusable HAR entry; skipped",
        "network.har entry 3: unusable HAR entry; skipped",
    ]


def test_read_har_keeps_evidence_when_one_entry_fails_to_format(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    real_evidence = har_module.har_entry_evidence

    def flaky(entry, **kwargs):
        if entry.get("request", {}).get("url", "").endswith("/boom"):
            raise ValueError("synthetic formatting failure")
        return real_evidence(entry, **kwargs)

    monkeypatch.setattr(har_module, "har_entry_evidence", flaky)

    har = write_har(
        tmp_path / "network.har",
        _entry(url="https://example.test/first", status=200, statusText="OK"),
        _entry(url="https://example.test/boom", status=200, statusText="OK"),
        _entry(url="https://example.test/last", status=200, statusText="OK"),
    )

    result = read_har(har)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/first",
        "GET https://example.test/last",
    ]
    assert result.warnings == [
        "network.har entry 2: formatting failed; skipped (synthetic formatting failure)"
    ]


def test_read_har_ignores_a_byte_order_mark(tmp_path):
    har = tmp_path / "network.har"
    document = json.dumps(_har(_entry(status=200, statusText="OK")), separators=(",", ":"))
    har.write_bytes(b"\xef\xbb\xbf" + document.encode("utf-8"))

    result = read_har(har)

    assert [item.summary.split(" | ")[1] for item in result.evidence] == ["status=200 OK"]
    assert result.warnings == []


def test_read_har_rejects_unsafe_zip_member_paths(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(url="https://example.test/kept", status=200, statusText="OK"),
        members={
            "../escape.txt": "traversal",
            "/absolute.txt": "absolute",
            "C:/windows.txt": "drive",
            "nested/../../escape.txt": "nested traversal",
        },
    )

    result = read_har(archive)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/kept"
    ]
    assert sorted(result.warnings) == [
        "network.har.zip: unsafe member path '../escape.txt'; skipped",
        "network.har.zip: unsafe member path '/absolute.txt'; skipped",
        "network.har.zip: unsafe member path 'C:/windows.txt'; skipped",
        "network.har.zip: unsafe member path 'nested/../../escape.txt'; skipped",
    ]


def test_read_har_skips_a_har_member_reached_only_through_an_unsafe_path(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(url="https://example.test/unreachable", status=200, statusText="OK"),
        har_name="../har.har",
    )

    result = read_har(archive)

    assert result.evidence == []
    assert result.warnings == [
        "network.har.zip: unsafe member path '../har.har'; skipped",
        "network.har.zip: no .har member found; skipped",
    ]


def test_read_har_bounds_the_number_of_zip_members(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.json"),
        members={"a1b2.json": '{"detail":"dropped"}'},
    )

    monkeypatch.setattr(har_module, "MAX_HAR_MEMBERS", 1)
    result = read_har(archive)

    assert len(result.evidence) == 1
    assert "body=" not in result.evidence[0].summary
    assert result.warnings == [
        "network.har.zip: member limit exceeded; only first 1 members processed",
        "network.har.zip/har.har entry 1: body member 'a1b2.json' not found; body skipped",
    ]


def test_read_har_bounds_the_har_size(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    plain = write_har(tmp_path / "network.har", _entry(status=200, statusText="OK"))
    archive = write_har_zip(
        tmp_path / "network.har.zip", _entry(status=200, statusText="OK")
    )

    monkeypatch.setattr(har_module, "MAX_HAR_MEMBER_SIZE", 10)

    plain_result = read_har(plain)
    assert plain_result.evidence == []
    assert plain_result.warnings == ["network.har: size limit exceeded; skipped"]

    zip_result = read_har(archive)
    assert zip_result.evidence == []
    assert zip_result.warnings == ["network.har.zip/har.har: size limit exceeded; skipped"]


def test_read_har_bounds_the_total_uncompressed_size(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.json"),
        members={"a1b2.json": '{"detail":"too big to add"}'},
    )

    monkeypatch.setattr(har_module, "MAX_HAR_TOTAL_SIZE", 16)
    result = read_har(archive)

    assert len(result.evidence) == 1
    assert "body=" not in result.evidence[0].summary
    assert result.warnings == [
        "network.har.zip/har.har entry 1: total size limit exceeded; body skipped"
    ]


def test_read_har_bounds_the_number_of_entries(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    har = write_har(
        tmp_path / "network.har",
        _entry(url="https://example.test/first", status=200, statusText="OK"),
        _entry(url="https://example.test/second", status=200, statusText="OK"),
    )

    monkeypatch.setattr(har_module, "MAX_HAR_ENTRIES", 1)
    result = read_har(har)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/first"
    ]
    assert result.warnings == [
        "network.har: entry limit exceeded; only first 1 entries processed"
    ]


def test_read_har_bounds_the_amount_of_evidence(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    har = write_har(
        tmp_path / "network.har",
        _entry(url="https://example.test/first", status=200, statusText="OK"),
        _entry(url="https://example.test/second", status=200, statusText="OK"),
    )

    monkeypatch.setattr(har_module, "MAX_HAR_EVIDENCE", 1)
    result = read_har(har)

    assert [item.summary.split(" | ")[0] for item in result.evidence] == [
        "GET https://example.test/first"
    ]
    assert result.warnings == [
        "network.har: evidence limit exceeded; remaining entries skipped"
    ]


def test_read_har_bounds_the_number_of_warnings(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    har = tmp_path / "network.har"
    har.write_text(json.dumps(_har(*([None] * 5)), separators=(",", ":")), encoding="utf-8")

    monkeypatch.setattr(har_module, "MAX_HAR_WARNINGS", 2)
    result = read_har(har)

    assert result.evidence == []
    assert result.warnings == [
        "network.har entry 1: unusable HAR entry; skipped",
        "network.har entry 2: unusable HAR entry; skipped",
        "network.har: 3 warnings suppressed",
    ]


def test_read_har_bounds_the_bytes_read_for_a_body_preview(tmp_path, monkeypatch):
    import testexplain.sources.har as har_module

    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=500, statusText="Internal Server Error", _file="a1b2.txt"),
        _entry(status=500, statusText="Internal Server Error", text="inline-body-is-also-bounded"),
        members={"a1b2.txt": "external-body-is-bounded"},
    )

    monkeypatch.setattr(har_module, "MAX_HAR_BODY_BYTES", 8)
    result = read_har(archive)

    assert [item.summary.split(" | ")[-1] for item in result.evidence] == [
        "body=external",
        "body=inline-b",
    ]
    assert result.warnings == []


def test_read_har_warns_when_the_archive_is_unreadable(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=200, statusText="OK"),
        members={"body.json": "{}"},
    )
    # ``is_zipfile`` only validates the first central-directory entry, so
    # corrupting the second produces a file that sniffs as a ZIP and then fails
    # to open, which is the case the adapter has to survive.
    raw = bytearray(archive.read_bytes())
    first = raw.index(b"PK\x01\x02")
    raw[raw.index(b"PK\x01\x02", first + 1)] = 0x00
    archive.write_bytes(bytes(raw))

    result = read_har(archive)

    assert zipfile.is_zipfile(archive)
    assert result.evidence == []
    assert result.warnings == ["network.har.zip: unreadable ZIP; skipped"]


def test_read_har_warns_when_the_file_is_missing(tmp_path):
    result = read_har(tmp_path / "absent.har")

    assert result.evidence == []
    assert result.warnings == ["absent.har: unreadable HAR; skipped"]


def test_read_har_warns_when_the_har_member_is_unreadable(tmp_path):
    archive = write_har_zip(
        tmp_path / "network.har.zip",
        _entry(status=200, statusText="OK"),
        stored=("har.har",),
    )
    corrupt_member(archive, "har.har")

    result = read_har(archive)

    assert result.evidence == []
    assert result.warnings == ["network.har.zip/har.har: unreadable member; skipped"]
