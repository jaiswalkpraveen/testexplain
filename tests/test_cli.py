import json
import zipfile
from pathlib import Path

from typer.testing import CliRunner

from testexplain.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"

runner = CliRunner()


def write_report(path: Path, attachment_path: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "config": {},
                "suites": [
                    {
                        "title": "root",
                        "specs": [
                            {
                                "title": "failed example",
                                "tests": [
                                    {
                                        "expectedStatus": "passed",
                                        "results": [
                                            {
                                                "status": "failed",
                                                "duration": 1000,
                                                "error": {
                                                    "message": "expected true to be false",
                                                    "stack": "Error: expected true to be false",
                                                },
                                                "attachments": [
                                                    {
                                                        "name": "trace",
                                                        "path": attachment_path,
                                                    }
                                                ],
                                            }
                                        ],
                                    }
                                ],
                            }
                        ],
                        "suites": [],
                    }
                ],
                "errors": [],
                "stats": {},
            }
        )
    )
    return path


def test_analyze_command_prints_explanation_with_fake_gateway():
    result = runner.invoke(app, ["analyze", str(FIXTURE), "--fake"])

    # Command exited cleanly.
    assert result.exit_code == 0
    # The failing test's title shows up in the printed output.
    assert "user sees dashboard after login" in result.stdout
    # The fake gateway's canned explanation is printed too.
    assert "FAKE:" in result.stdout


def test_bundle_command_creates_bundle_and_reports_artifact_count(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"not a real trace")
    report = write_report(tmp_path / "report.json", artifact.name)
    output = tmp_path / "bundle.zip"

    result = runner.invoke(app, ["bundle", str(report), "--output", str(output)])

    assert result.exit_code == 0
    assert output.is_file()
    with zipfile.ZipFile(output) as bundle:
        assert bundle.namelist() == ["report.json", "artifacts/000-trace.zip"]
    assert result.stdout == f"Bundled 1 artifacts into {output}\n"
    assert result.stderr == ""


def test_bundle_command_warns_for_missing_artifact_but_succeeds(tmp_path):
    report = write_report(tmp_path / "report.json", "missing.zip")
    output = tmp_path / "bundle.zip"

    result = runner.invoke(app, ["bundle", str(report), "--output", str(output)])

    assert result.exit_code == 0
    assert output.is_file()
    assert result.stdout == f"Bundled 0 artifacts into {output}\n"
    assert result.stderr == "Warning: Attachment 'trace' path 'missing.zip' does not exist\n"


def test_bundle_command_reports_missing_report_as_hard_error(tmp_path):
    output = tmp_path / "bundle.zip"

    result = runner.invoke(
        app, ["bundle", str(tmp_path / "missing.json"), "--output", str(output)]
    )

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: Report file does not exist:")


def test_bundle_command_reports_non_native_json_as_hard_error(tmp_path):
    report = tmp_path / "invalid.json"
    report.write_text("not valid JSON")
    output = tmp_path / "bundle.zip"

    result = runner.invoke(app, ["bundle", str(report), "--output", str(output)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: Input must be a native Playwright JSON report\n"


def test_bundle_command_reports_valid_but_non_native_json_as_hard_error(tmp_path):
    report = tmp_path / "invalid.json"
    report.write_text("{}")
    output = tmp_path / "bundle.zip"

    result = runner.invoke(app, ["bundle", str(report), "--output", str(output)])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr == "Error: Input must be a native Playwright JSON report\n"


def test_analyze_command_accepts_bundle_with_fake_gateway(tmp_path):
    artifact = tmp_path / "trace.zip"
    artifact.write_bytes(b"not a real trace")
    report = write_report(tmp_path / "report.json", artifact.name)
    bundle = tmp_path / "bundle.zip"

    bundle_result = runner.invoke(
        app, ["bundle", str(report), "--output", str(bundle)]
    )
    result = runner.invoke(app, ["analyze", str(bundle), "--fake"])

    assert bundle_result.exit_code == 0
    assert result.exit_code == 0
    assert "FAKE:" in result.stdout
