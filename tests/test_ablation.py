import json
import shutil
from pathlib import Path

from testexplain.core import analyze_input
from testexplain.gateway import FakeGateway


FIXTURES = Path(__file__).parent / "fixtures" / "m2"
VALID_REPLY = """{
"summary": "The checkout service was unavailable.",
"suspected_category": "api outage",
"evidence": ["503 Service Unavailable"],
"next_steps": ["Check checkout service health"],
"confidence": 0.9
}"""


def _scenario(tmp_path: Path, *artifacts: str) -> Path:
    for name in ("checkout-report.json", *artifacts):
        shutil.copy(FIXTURES / name, tmp_path / name)
    return tmp_path / "checkout-report.json"


def _prompt(tmp_path: Path, *artifacts: str) -> str:
    gateway = FakeGateway(response=VALID_REPLY)
    results = analyze_input(_scenario(tmp_path, *artifacts), gateway)
    assert len(results) == 1
    assert len(gateway.calls) == 1
    return gateway.calls[0]


def test_report_only_prompt_has_report_evidence_but_no_artifact_evidence(tmp_path):
    prompt = _prompt(tmp_path)

    assert "Timeout 30000ms exceeded" in prompt
    assert "[trace]" not in prompt
    assert "[har]" not in prompt
    assert "checkout.trace.zip" in prompt


def test_report_plus_trace_prompt_adds_trace_action_and_network_evidence(tmp_path):
    prompt = _prompt(tmp_path, "checkout.trace.zip")

    assert "Timeout 30000ms exceeded" in prompt
    assert "[trace]" in prompt
    assert "submit checkout order" in prompt
    assert "POST https://shop.test/api/checkout" in prompt
    assert "503 Service Unavailable" in prompt
    assert "[har]" not in prompt


def test_report_plus_trace_and_har_prompt_keeps_both_network_provenances(tmp_path):
    prompt = _prompt(tmp_path, "checkout.trace.zip", "checkout.har")

    assert "[trace]" in prompt
    assert "[har]" in prompt
    assert prompt.count("POST https://shop.test/api/checkout") >= 2
    assert "503 Service Unavailable" in prompt
    assert "time=" in prompt


def test_missing_artifact_warning_does_not_discard_analysis(tmp_path):
    shutil.copy(FIXTURES / "missing-trace-report.json", tmp_path / "report.json")
    gateway = FakeGateway(response=VALID_REPLY)

    results = analyze_input(tmp_path / "report.json", gateway)

    assert len(results) == 1
    assert len(gateway.calls) == 1
    assert "missing.trace.zip" in gateway.calls[0]
    assert "does not exist" in gateway.calls[0]
