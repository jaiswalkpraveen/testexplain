import json
import zipfile
from pathlib import Path

import pytest

from testexplain.ingestion import input_reader
from testexplain.ingestion.input_reader import (
    InvalidBundleError,
    LoadedInput,
    load_input,
    resolve_attachment_path,
)
from testexplain.models import Attachment

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"


def write_bundle(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
    return path


def playwright_report() -> dict:
    report = json.loads(FIXTURE.read_text())
    return {
        "config": {},
        "suites": report["suites"],
        "errors": [],
        "stats": {},
    }


def test_load_input_reads_plain_report():
    with load_input(FIXTURE) as loaded:
        assert isinstance(loaded, LoadedInput)
        assert len(loaded.attempts) == 1
        assert loaded.artifact_dir == FIXTURE.parent.resolve()
        assert loaded.warnings == []


def test_load_input_detects_json_object_without_json_suffix(tmp_path):
    report = tmp_path / "playwright-report"
    report.write_bytes(FIXTURE.read_bytes())

    with load_input(report) as loaded:
        assert len(loaded.attempts) == 1
        assert loaded.artifact_dir == tmp_path.resolve()


def test_load_input_rejects_missing_path(tmp_path):
    missing = tmp_path / "missing.json"

    with pytest.raises(FileNotFoundError, match="Input file does not exist"):
        load_input(missing)


def test_load_input_discovers_structured_report_and_preserves_artifacts(tmp_path):
    bundle = write_bundle(
        tmp_path / "results.zip",
        {
            "reports/custom-report-name": json.dumps(playwright_report()),
            "metadata.json": json.dumps({"build": "123"}),
            "artifacts/trace.zip": b"trace data",
            "artifacts/network.har": "{}",
        },
    )

    with load_input(bundle) as loaded:
        assert len(loaded.attempts) == 1
        assert loaded.artifact_dir is not None
        assert (loaded.artifact_dir / "artifacts" / "trace.zip").read_bytes() == b"trace data"
        assert (loaded.artifact_dir / "artifacts" / "network.har").read_text() == "{}"


@pytest.mark.parametrize(
    "entries",
    [
        {"trace.zip": b"trace", "metadata.json": "{}"},
        {
            "one.json": json.dumps(playwright_report()),
            "nested/two.data": json.dumps(playwright_report()),
        },
    ],
)
def test_load_input_requires_exactly_one_playwright_report(tmp_path, entries):
    bundle = write_bundle(tmp_path / "results.zip", entries)

    with pytest.raises(ValueError, match="exactly one Playwright JSON report"):
        load_input(bundle)


def test_load_input_marks_invalid_bundle_errors_with_a_specific_exception(tmp_path):
    bundle = write_bundle(tmp_path / "results.zip", {"metadata.json": "{}"})

    with pytest.raises(InvalidBundleError, match="exactly one Playwright JSON report"):
        load_input(bundle)


@pytest.mark.parametrize(
    "member",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "nested/../../escape.txt",
        "..\\escape.txt",
        "C:\\drive.txt",
        "nested\\..\\..\\escape.txt",
    ],
)
def test_load_input_rejects_unsafe_zip_member_paths(tmp_path, member):
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": "{}", member: "unsafe"},
    )

    with pytest.raises(ValueError, match="unsafe path"):
        load_input(bundle)


def test_load_input_rejects_duplicate_member_paths(tmp_path):
    bundle_path = tmp_path / "results.zip"
    with zipfile.ZipFile(bundle_path, "w") as archive:
        archive.writestr("report.json", json.dumps(playwright_report()))
        archive.writestr("Report.JSON", "{}")

    with pytest.raises(ValueError, match="duplicate member path"):
        load_input(bundle_path)


def test_load_input_enforces_member_count_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(input_reader, "MAX_MEMBERS", 2)
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": "{}", "one.txt": "1", "two.txt": "2"},
    )

    with pytest.raises(ValueError, match="member limit of 2"):
        load_input(bundle)


def test_load_input_enforces_member_uncompressed_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(input_reader, "MAX_MEMBER_UNCOMPRESSED_SIZE", 10)
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": "{}", "artifact.har": "12345678901"},
    )

    with pytest.raises(ValueError, match="member uncompressed size limit of 10 bytes"):
        load_input(bundle)


def test_load_input_enforces_total_uncompressed_size_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(input_reader, "MAX_TOTAL_UNCOMPRESSED_SIZE", 10)
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": "{}", "artifact.har": "123456789"},
    )

    with pytest.raises(ValueError, match="uncompressed size limit of 10 bytes"):
        load_input(bundle)


def test_load_input_rejects_suspicious_compression_ratio(tmp_path, monkeypatch):
    monkeypatch.setattr(input_reader, "MAX_COMPRESSION_RATIO", 2)
    monkeypatch.setattr(input_reader, "COMPRESSION_RATIO_MIN_SIZE", 10)
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": "{}", "artifact.har": "0" * 1000},
    )

    with pytest.raises(ValueError, match="compression ratio limit of 2"):
        load_input(bundle)


def test_loaded_input_context_manager_removes_extraction_directory(tmp_path):
    bundle = write_bundle(
        tmp_path / "results.zip",
        {"report.json": json.dumps(playwright_report())},
    )

    with load_input(bundle) as loaded:
        extraction_dir = loaded.artifact_dir
        assert extraction_dir is not None
        assert extraction_dir.exists()

    assert not extraction_dir.exists()


def test_loaded_input_context_manager_does_not_remove_plain_report_parent():
    parent = FIXTURE.parent.resolve()

    with load_input(FIXTURE):
        pass

    assert parent.exists()


def test_resolve_attachment_path_returns_existing_path_inside_artifact_dir(tmp_path):
    artifact = tmp_path / "nested" / "trace.zip"
    artifact.parent.mkdir()
    artifact.write_bytes(b"trace")
    warnings: list[str] = []

    resolved = resolve_attachment_path(
        tmp_path,
        Attachment(name="trace", path="nested/trace.zip"),
        warnings,
    )

    assert resolved == artifact.resolve()
    assert warnings == []


def test_resolve_attachment_path_does_not_warn_for_inline_body(tmp_path):
    warnings: list[str] = []

    resolved = resolve_attachment_path(
        tmp_path,
        Attachment(name="log", body_b64="aGVsbG8="),
        warnings,
    )

    assert resolved is None
    assert warnings == []


@pytest.mark.parametrize(
    "attachment_path, expected",
    [
        ("../outside.zip", "outside the artifact directory"),
        ("missing.zip", "does not exist"),
        (None, "has no path"),
    ],
)
def test_resolve_attachment_path_warns_for_unusable_paths(
    tmp_path, attachment_path, expected
):
    warnings: list[str] = []

    resolved = resolve_attachment_path(
        tmp_path,
        Attachment(name="trace", path=attachment_path),
        warnings,
    )

    assert resolved is None
    assert len(warnings) == 1
    assert expected in warnings[0]


def test_resolve_attachment_path_rejects_external_absolute_path_for_bundle(tmp_path):
    external = tmp_path.parent / "external-trace.zip"
    external.write_bytes(b"trace")
    warnings: list[str] = []

    try:
        resolved = resolve_attachment_path(
            tmp_path,
            Attachment(name="trace", path=str(external)),
            warnings,
        )
    finally:
        external.unlink()

    assert resolved is None
    assert "outside the artifact directory" in warnings[0]


def test_plain_loaded_input_allows_existing_external_absolute_path(tmp_path):
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report = report_dir / "report.json"
    report.write_text(json.dumps({"suites": []}))
    external = tmp_path / "trace.zip"
    external.write_bytes(b"trace")

    with load_input(report) as loaded:
        resolved = loaded.resolve_attachment_path(
            Attachment(name="trace", path=str(external))
        )

    assert resolved == external.resolve()
    assert loaded.warnings == []
