"""Command-line interface.

Usage:
    testlens analyze ./report.json     # Run against a real Playwright report
    testlens analyze ./report.json --fake  # Dry run with fake gateway (no API key)

The CLI is the integration point: it wires the ingress (report file),
the reasoning (core analysis), and the LLM (gateway) together, then
prints the explanations to the terminal.
"""

import typer
from dotenv import load_dotenv

from testlens.core import analyze_report
from testlens.gateway import FakeGateway, OpenAICompatibleGateway

# Read .env from the project root into os.environ (no-op if absent),
# so LLM_* vars work without exporting them in every shell.
load_dotenv()

app = typer.Typer()


@app.callback()
def _main() -> None:
    """TestLens: explain why your tests failed."""


@app.command()
def analyze(report_path: str, fake: bool = False):
    """Analyze a Playwright report and explain each failure.

    Args:
        report_path: Path to the Playwright JSON report file.
        fake: Use a fake gateway (no network, no API key) for testing.
    """
    if fake:
        gateway = FakeGateway()
    else:
        try:
            gateway = OpenAICompatibleGateway()
        except KeyError as exc:
            typer.echo(
                f"Error: missing environment variable {exc}. "
                "Set LLM_BASE_URL, LLM_API_KEY and LLM_MODEL "
                "(e.g. in a .env file), or run with --fake for a no-key dry run.",
                err=True,
            )
            raise typer.Exit(code=1)

    for analysis in analyze_report(report_path, gateway):
        print(f"\n### {analysis.test_title}")
        print(f"[{analysis.suspected_category}] "
              f"(confidence: {analysis.confidence:.0%})")
        print(analysis.summary)
        if analysis.evidence:
            print("Evidence:")
            for item in analysis.evidence:
                print(f"  - {item}")
        if analysis.next_steps:
            print("Next steps:")
            for step in analysis.next_steps:
                print(f"  - {step}")


if __name__ == "__main__":
    app()