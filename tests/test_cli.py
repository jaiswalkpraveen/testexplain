from pathlib import Path

from typer.testing import CliRunner

from testlens.cli import app

FIXTURE = Path(__file__).parent / "fixtures" / "sample_report.json"

runner = CliRunner()


def test_analyze_command_prints_explanation_with_fake_gateway():
    result = runner.invoke(app, ["analyze", str(FIXTURE), "--fake"])

    # Command exited cleanly.
    assert result.exit_code == 0
    # The failing test's title shows up in the printed output.
    assert "user sees dashboard after login" in result.stdout
    # The fake gateway's canned explanation is printed too.
    assert "FAKE:" in result.stdout
