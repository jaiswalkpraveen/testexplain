"""Tests for deterministic, redacting evidence assembly."""

from testexplain.assembly.assembler import assemble_evidence
from testexplain.assembly.budget import estimate_tokens
from testexplain.models import Evidence

REDACTED = "<redacted>"


def _evidence(
    summary: str,
    *,
    source: str = "console",
    provenance: str = "trace",
    severity: int = 1,
    timestamp_ms: float | None = None,
) -> Evidence:
    """Build normalized evidence with deliberately visible ranking fields."""
    return Evidence(
        source=source,
        provenance=provenance,
        summary=summary,
        severity=severity,
        timestamp_ms=timestamp_ms,
    )


def _network(
    method: str,
    url: str,
    status: int,
    timestamp_ms: float,
    *,
    provenance: str = "trace",
    severity: int = 3,
) -> Evidence:
    """Build the stable leading summary fields emitted by the network adapters."""
    return _evidence(
        f"{method} {url} | status={status} Error | mime=application/json | time=1.0ms",
        source="network",
        provenance=provenance,
        severity=severity,
        timestamp_ms=timestamp_ms,
    )


def test_assemble_evidence_returns_empty_metadata_for_no_evidence() -> None:
    """An artifact with no usable records is a valid, explainable result."""
    assembled = assemble_evidence([], warnings=["trace was corrupt"])
    assert assembled.evidence == []
    assert assembled.warnings == ["trace was corrupt"]
    assert assembled.truncation.used_tokens == 0
    assert assembled.truncation.omitted_count == 0
    assert assembled.truncation.truncated_count == 0


def test_assemble_evidence_keeps_usable_evidence_when_another_artifact_warns() -> None:
    """Warnings travel beside, rather than replacing, evidence from good artifacts."""
    usable = _evidence("error: checkout failed", severity=4)
    assembled = assemble_evidence([usable], warnings=(warning for warning in ["bad HAR entry"]))
    assert assembled.evidence == [usable]
    assert assembled.warnings == ["bad HAR entry"]


def test_assemble_evidence_preserves_one_clean_evidence_record() -> None:
    """A fitting record retains every normalized metadata field."""
    raw = _evidence("error: checkout failed", severity=4, timestamp_ms=123.0)
    assembled = assemble_evidence([raw])
    assert assembled.evidence == [raw]
    assert assembled.evidence[0] is not raw
    assert assembled.truncation.used_tokens == estimate_tokens(raw.summary)


def test_assemble_evidence_ranks_higher_severity_then_more_recent_timestamps() -> None:
    """Diagnostic importance wins; equal severity uses later wall-clock evidence."""
    low = _evidence("low", severity=1, timestamp_ms=900.0)
    older = _evidence("older", severity=4, timestamp_ms=100.0)
    newer = _evidence("newer", severity=4, timestamp_ms=200.0)
    assembled = assemble_evidence([low, older, newer])
    assert [item.summary for item in assembled.evidence] == ["newer", "older", "low"]


def test_assemble_evidence_uses_explicit_lexical_tie_breakers_deterministically() -> None:
    """Repeated calls do not depend on input hash order for otherwise equal records."""
    beta = _evidence("beta", source="stderr", severity=2, timestamp_ms=None)
    alpha = _evidence("alpha", source="stdout", severity=2, timestamp_ms=None)
    first = assemble_evidence([beta, alpha])
    second = assemble_evidence([beta, alpha])
    assert [item.summary for item in first.evidence] == ["beta", "alpha"]
    assert first == second


def test_assemble_evidence_collapses_equivalent_network_requests_at_one_second_precision() -> None:
    """Trace and HAR copies in the same rounded second retain the best record once."""
    trace = _network("POST", "https://app.test/login", 500, 1_200.0, severity=4)
    har = _network(
        "POST", "https://app.test/login", 500, 1_499.0, provenance="har", severity=3
    )
    assembled = assemble_evidence([har, trace])
    assert assembled.evidence == [trace]
    assert assembled.truncation.deduplicated_count == 1


def test_assemble_evidence_keeps_network_requests_with_distinct_identity_fields() -> None:
    """Method, URL, status, and rounded-second changes mean separate requests."""
    evidence = [
        _network("GET", "https://app.test/login", 500, 1_200.0),
        _network("POST", "https://app.test/other", 500, 1_200.0),
        _network("POST", "https://app.test/login", 401, 1_200.0),
        _network("POST", "https://app.test/login", 500, 2_501.0),
    ]
    assembled = assemble_evidence(evidence)
    assert len(assembled.evidence) == 4
    assert assembled.truncation.deduplicated_count == 0


def test_assemble_evidence_keeps_network_requests_in_different_rounding_buckets() -> None:
    """Timestamps on opposite sides of the half-second rounding boundary stay distinct."""
    before = _network("POST", "https://app.test/login", 500, 1_499.0)
    after = _network("POST", "https://app.test/login", 500, 1_500.0)
    assembled = assemble_evidence([before, after])
    assert len(assembled.evidence) == 2


def test_assemble_evidence_does_not_deduplicate_malformed_network_summaries() -> None:
    """Only adapter-shaped network summaries have enough fields for a safe identity."""
    first = _evidence("incomplete", source="network", severity=4, timestamp_ms=1_000.0)
    second = _evidence("incomplete", source="network", severity=4, timestamp_ms=1_000.0)
    assembled = assemble_evidence([first, second])
    assert len(assembled.evidence) == 2
    assert assembled.truncation.deduplicated_count == 0


def test_assemble_evidence_never_deduplicates_non_network_evidence() -> None:
    """Identical console lines remain independent observations."""
    first = _evidence("error: connection refused", severity=4, timestamp_ms=100.0)
    second = _evidence("error: connection refused", severity=4, timestamp_ms=100.0)
    assembled = assemble_evidence([first, second])
    assert len(assembled.evidence) == 2
    assert assembled.truncation.deduplicated_count == 0


def test_assemble_evidence_does_not_drop_identical_non_network_records_after_section_fills() -> None:
    """Two identical observations remain distinct even when both are deferred."""
    first = _evidence("x" * 24, severity=4)
    second = _evidence("x" * 24, severity=4)
    assembled = assemble_evidence([first, second], total_budget=15)
    assert len(assembled.evidence) == 2
    assert assembled.truncation.omitted_count == 0


def test_assemble_evidence_redacts_secrets_before_returning_new_records() -> None:
    """Raw evidence survives unchanged while all selected output is safe to compose later."""
    raw = _network("POST", "https://app.test/login", 500, 1_000.0)
    raw.summary += " | Authorization: Bearer fake-token | api_key=fake-key | Cookie: sid=fake-cookie | body={\"password\":\"fake-password\"}"
    assembled = assemble_evidence([raw])
    selected = assembled.evidence[0]
    assert "fake-token" not in selected.summary
    assert "fake-key" not in selected.summary
    assert "fake-cookie" not in selected.summary
    assert "fake-password" not in selected.summary
    assert selected.summary.count(REDACTED) == 4
    assert raw.summary.endswith('body={"password":"fake-password"}')
    assert selected.source == raw.source
    assert selected.provenance == raw.provenance
    assert selected.severity == raw.severity
    assert selected.timestamp_ms == raw.timestamp_ms
    assert selected is not raw


def test_assemble_evidence_reuses_spare_section_capacity() -> None:
    """Unused action, console, and notes targets can admit extra network evidence."""
    network = _network("POST", "https://app.test/login", 500, 1_000.0)
    network.summary = "n" * 120
    assembled = assemble_evidence([network], total_budget=40)
    assert assembled.evidence == [network]
    assert assembled.truncation.used_tokens == 30
    assert assembled.truncation.section_tokens == {
        "actions": 0,
        "console_output": 0,
        "network": 30,
    }


def test_assemble_evidence_reports_deterministic_truncation_and_omission() -> None:
    """The highest-ranked item is safely shortened when it alone exceeds the budget."""
    large = _evidence("a" * 100, severity=4)
    lower = _evidence("low", severity=1)
    assembled = assemble_evidence([lower, large], total_budget=10)
    assert [item.summary for item in assembled.evidence] == ["a" * 39 + "…"]
    assert assembled.truncation.used_tokens == 10
    assert assembled.truncation.truncated_count == 1
    assert assembled.truncation.omitted_count == 1
    assert assembled.truncation.total_budget == 10


def test_assemble_evidence_never_exceeds_the_four_thousand_token_budget() -> None:
    """Large normalized summaries cannot overflow the eventual model context allocation."""
    evidence = [_evidence("x" * 20_000, severity=4)]
    assembled = assemble_evidence(evidence)
    assert assembled.truncation.used_tokens <= 4_000
    assert sum(estimate_tokens(item.summary) for item in assembled.evidence) <= 4_000
    assert assembled.truncation.truncated_count == 1
