import json
import zipfile
from pathlib import Path

import pytest

from testexplain.ingestion.input_reader import load_input
from testexplain.ingestion.package import create_bundle


def native_report(tests: list[dict]) -> dict:
    return {
        "config": {},
        "suites": [
            {
                "title": "root",
                "specs": [{"title": "example", "tests": tests}],
                "suites": [],
            }
        ],
        "errors": [],
        "stats": {},
    }


def make_case(*results: dict, expected_status: str = "passed") -> dict:
    return {"expectedStatus": expected_status, "results": list(results)}


def result(status: str, attachments: list[object]) -> dict:
    return {"status": status, "attachments": attachments}


def write_report(path: Path, report: dict) -> Path:
    path.write_text(json.dumps(report))
    return path


def read_bundle(path: Path) -> tuple[list[str], dict]:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        report = json.loads(archive.read("report.json"))
    return names, report


def attachment_paths(report: dict) -> list[str | None]:
    return [
        attachment.get("path")
        for test in report["suites"][0]["specs"][0]["tests"]
        for attempt in test["results"]
        for attachment in attempt.get("attachments", [])
        if isinstance(attachment, dict)
    ]


def test_create_bundle_copies_only_unexpected_attempt_artifacts_and_rewrites_paths(tmp_path):
    trace = tmp_path / "trace.zip"
    trace.write_bytes(b"trace")
    ignored = tmp_path / "ignored.txt"
    ignored.write_bytes(b"ignored")
    report = native_report(
        [
            make_case(result("failed", [{"name": "trace", "path": "trace.zip"}])),
            make_case(result("passed", [{"name": "pass", "path": "ignored.txt"}])),
            make_case(
                result("timedOut", [{"name": "expected", "path": "ignored.txt"}]),
                expected_status="failed",
            ),
        ]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json", "artifacts/000-trace.zip"]
    assert bundle.artifact_count == 1
    assert bundle.warnings == []
    assert attachment_paths(rewritten) == ["artifacts/000-trace.zip", "ignored.txt", "ignored.txt"]
    assert json.loads(report_path.read_text()) == report


def test_create_bundle_warns_for_missing_artifact_and_keeps_its_path(tmp_path):
    report = native_report(
        [make_case(result("failed", [{"name": "trace", "path": "missing.zip"}]))]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json"]
    assert attachment_paths(rewritten) == ["missing.zip"]
    assert "does not exist" in bundle.warnings[0]
    assert not any("no unexpected failed or timedOut attempts" in warning for warning in bundle.warnings)


def test_create_bundle_deduplicates_sources_and_uses_unique_names_for_duplicate_basenames(tmp_path):
    first = tmp_path / "one" / "trace.zip"
    first.parent.mkdir()
    first.write_bytes(b"first")
    second = tmp_path / "two" / "trace.zip"
    second.parent.mkdir()
    second.write_bytes(b"second")
    report = native_report(
        [
            make_case(
                result(
                    "failed",
                    [
                        {"name": "first", "path": "one/trace.zip"},
                        {"name": "again", "path": "one/trace.zip"},
                        {"name": "second", "path": "two/trace.zip"},
                    ],
                )
            )
        ]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == [
        "report.json",
        "artifacts/000-trace.zip",
        "artifacts/001-trace.zip",
    ]
    assert attachment_paths(rewritten) == [
        "artifacts/000-trace.zip",
        "artifacts/000-trace.zip",
        "artifacts/001-trace.zip",
    ]
    assert bundle.artifact_count == 2


def test_create_bundle_is_byte_deterministic(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"trace")
    report = native_report(
        [make_case(result("failed", [{"name": "trace", "path": "trace.zip"}]))]
    )
    report_path = write_report(tmp_path / "report.json", report)

    first = create_bundle(report_path, tmp_path / "one.zip")
    second = create_bundle(report_path, tmp_path / "two.zip")

    assert first.output_path.read_bytes() == second.output_path.read_bytes()


def test_create_bundle_warns_when_report_has_no_packageable_attempts(tmp_path):
    report_path = write_report(
        tmp_path / "report.json",
        native_report([make_case(result("passed", []))]),
    )

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, _ = read_bundle(bundle.output_path)
    assert names == ["report.json"]
    assert bundle.artifact_count == 0
    assert bundle.warnings == [
        "Report contains no unexpected failed or timedOut attempts to package"
    ]


def test_create_bundle_warns_for_inline_body_without_path(tmp_path):
    report = native_report(
        [
            make_case(
                result("failed", [{"name": "log", "body": "aGVsbG8="}])
            )
        ]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    _, rewritten = read_bundle(bundle.output_path)
    assert rewritten == report
    assert any("inline body and was not copied" in warning for warning in bundle.warnings)


def test_create_bundle_does_not_copy_attachment_with_both_body_and_path(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"trace")
    report = native_report(
        [
            make_case(
                result(
                    "failed",
                    [{"name": "log", "path": "trace.zip", "body": "aGVsbG8="}],
                )
            )
        ]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json"]
    assert bundle.artifact_count == 0
    assert rewritten == report
    assert any("inline body and was not copied" in warning for warning in bundle.warnings)


def test_create_bundle_copies_external_absolute_path_for_direct_report(tmp_path):
    external = tmp_path / "external" / "trace.zip"
    external.parent.mkdir()
    external.write_bytes(b"trace")
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report = native_report(
        [make_case(result("failed", [{"name": "trace", "path": str(external)}]))]
    )
    report_path = write_report(report_dir / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json", "artifacts/000-trace.zip"]
    assert attachment_paths(rewritten) == ["artifacts/000-trace.zip"]
    assert bundle.warnings == []


def test_create_bundle_warns_for_path_outside_report_directory(tmp_path):
    outside = tmp_path / "outside.zip"
    outside.write_bytes(b"outside")
    report_dir = tmp_path / "report"
    report_dir.mkdir()
    report = native_report(
        [make_case(result("failed", [{"name": "trace", "path": "../outside.zip"}]))]
    )
    report_path = write_report(report_dir / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json"]
    assert rewritten == report
    assert "outside the artifact directory" in bundle.warnings[0]


def test_create_bundle_warns_for_attachment_without_path_or_body(tmp_path):
    report = native_report([make_case(result("failed", [{"name": "empty"}]))])
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json"]
    assert rewritten == report
    assert "has no path" in bundle.warnings[0]


def test_create_bundle_writes_entries_with_fixed_timestamps(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"trace")
    report = native_report(
        [make_case(result("failed", [{"name": "trace", "path": "trace.zip"}]))]
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    with zipfile.ZipFile(bundle.output_path) as archive:
        infos = archive.infolist()

    assert [info.filename for info in infos] == ["report.json", "artifacts/000-trace.zip"]
    assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)


@pytest.mark.parametrize(
    "contents",
    [
        "not json",
        json.dumps({"config": {}, "suites": []}),
        json.dumps({"config": [], "suites": [], "errors": [], "stats": {}}),
        json.dumps({"config": {}, "suites": {}, "errors": [], "stats": {}}),
    ],
)
def test_create_bundle_rejects_non_native_or_bad_reports(tmp_path, contents):
    report_path = tmp_path / "report.json"
    report_path.write_text(contents)

    with pytest.raises(ValueError, match="native Playwright JSON report"):
        create_bundle(report_path, tmp_path / "bundle.zip")


def test_create_bundle_rejects_missing_report(tmp_path):
    with pytest.raises(FileNotFoundError, match="Report file does not exist"):
        create_bundle(tmp_path / "missing.json", tmp_path / "bundle.zip")


def test_create_bundle_rejects_report_that_cannot_be_parsed_into_attempts(tmp_path):
    report_path = write_report(
        tmp_path / "report.json",
        {
            "config": {},
            "suites": [{"title": "root", "specs": "not-a-list", "suites": []}],
            "errors": [],
            "stats": {},
        },
    )
    destination = tmp_path / "bundle.zip"

    with pytest.raises(ValueError, match="native Playwright JSON report"):
        create_bundle(report_path, destination)

    assert not destination.exists()


def test_create_bundle_rejects_using_report_as_output_path(tmp_path):
    report = native_report([])
    report_path = write_report(tmp_path / "report.json", report)

    with pytest.raises(ValueError, match="output path must differ"):
        create_bundle(report_path, report_path)

    assert json.loads(report_path.read_text()) == report


def test_create_bundle_rejects_output_path_that_overwrites_an_artifact(tmp_path):
    artifact = tmp_path / "bundle.zip"
    artifact.write_bytes(b"original artifact bytes")
    report_path = write_report(
        tmp_path / "report.json",
        native_report(
            [make_case(result("failed", [{"name": "trace", "path": "bundle.zip"}]))]
        ),
    )

    with pytest.raises(ValueError, match="must not overwrite an attachment artifact"):
        create_bundle(report_path, artifact)

    assert artifact.read_bytes() == b"original artifact bytes"


def test_create_bundle_sanitizes_separators_in_artifact_member_names(tmp_path):
    artifact = tmp_path / "trace\\part.zip"
    artifact.write_bytes(b"trace")
    report_path = write_report(
        tmp_path / "report.json",
        native_report(
            [make_case(result("failed", [{"name": "trace", "path": "trace\\part.zip"}]))]
        ),
    )

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, rewritten = read_bundle(bundle.output_path)
    assert names == ["report.json", "artifacts/000-trace_part.zip"]
    assert attachment_paths(rewritten) == ["artifacts/000-trace_part.zip"]

    with load_input(bundle.output_path) as loaded:
        resolved = loaded.resolve_attachment_path(loaded.attempts[0].attachments[0])
        assert resolved is not None
        assert resolved.read_bytes() == b"trace"


def test_create_bundle_finds_artifacts_in_nested_suites(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"trace")
    report = native_report([])
    report["suites"][0]["suites"].append(
        {
            "title": "nested",
            "specs": [
                {
                    "title": "example",
                    "tests": [
                        make_case(
                            result("timedOut", [{"name": "trace", "path": "trace.zip"}])
                        )
                    ],
                }
            ],
            "suites": [],
        }
    )
    report_path = write_report(tmp_path / "report.json", report)

    bundle = create_bundle(report_path, tmp_path / "bundle.zip")

    names, _ = read_bundle(bundle.output_path)
    assert names == ["report.json", "artifacts/000-trace.zip"]
