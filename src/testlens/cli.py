"""Command-line interface.

Usage:
    testlens analyze ./report.json     # Run against a real Playwright report
    testlens analyze ./report.json --fake  # Dry run with fake gateway (no API key)

The CLI is the integration point: it wires the ingress (report file),
the reasoning (core analysis), and the LLM (gateway) together, then
prints the explanations to the terminal.
"""

import typer

from testlens.core import analyze_report
from testlens.gateway import AnthropicGateway, FakeGateway

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
            gateway = AnthropicGateway()
        except KeyError:
            typer.echo(
                "Error: ANTHROPIC_API_KEY is not set. "
                "Export it, or run with --fake for a no-key dry run.",
                err=True,
            )
            raise typer.Exit(code=1)

    for analysis in analyze_report(report_path, gateway):
        print(f"\n### {analysis.test_title}\n{analysis.explanation}")


if __name__ == "__main__":
    app()