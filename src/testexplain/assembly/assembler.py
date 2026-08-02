"""Redact, rank, deduplicate, and budget normalized evidence deterministically."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import floor
from typing import Final

from testexplain.assembly.budget import (
    DEFAULT_TOTAL_TOKEN_BUDGET,
    estimate_tokens,
    section_capacities,
    truncate_text,
)
from testexplain.assembly.redact import redact_all
from testexplain.models import Evidence

SECTION_BY_SOURCE: Final = {
    "action": "actions",
    "console": "console_output",
    "page_error": "console_output",
    "stdout": "console_output",
    "stderr": "console_output",
    "network": "network",
}
EVIDENCE_SECTIONS: Final = ("actions", "console_output", "network")


@dataclass(frozen=True)
class TruncationInfo:
    """Budget facts describing evidence that was shortened or left out."""

    total_budget: int
    used_tokens: int
    section_tokens: dict[str, int]
    omitted_count: int
    truncated_count: int
    deduplicated_count: int


@dataclass(frozen=True)
class AssembledEvidence:
    """Safe, selected evidence plus metadata needed by later prompt composition."""

    evidence: list[Evidence]
    warnings: list[str]
    truncation: TruncationInfo


def assemble_evidence(
    items: Iterable[Evidence],
    *,
    warnings: Iterable[str] = (),
    total_budget: int = DEFAULT_TOTAL_TOKEN_BUDGET,
) -> AssembledEvidence:
    """Return redacted evidence that fits a deterministic total token budget.

    Preferred section capacities prevent one source type from crowding out every
    other source.  They are not reservations: spare capacity is reused by any
    remaining higher-ranked evidence before records are truncated.
    """
    capacities = section_capacities(total_budget)
    ranked = _rank(redact_all(items))
    deduplicated, deduplicated_count = _deduplicate_network(ranked)

    selected: list[tuple[Evidence, bool]] = []
    deferred: list[Evidence] = []
    section_tokens = {section: 0 for section in EVIDENCE_SECTIONS}

    for section in EVIDENCE_SECTIONS:
        section_items = [item for item in deduplicated if _section(item) == section]
        remaining = capacities[section]
        for position, item in enumerate(section_items):
            tokens = estimate_tokens(item.summary)
            if tokens <= remaining:
                selected.append((item, False))
                section_tokens[section] += tokens
                remaining -= tokens
                continue
            deferred.extend(section_items[position:])
            break

    selected_ids = {id(item) for item, _ in selected}
    deferred.extend(item for item in deduplicated if id(item) not in selected_ids and item not in deferred)
    deferred = _rank(deferred)

    used_tokens = sum(estimate_tokens(item.summary) for item, _ in selected)
    remaining = total_budget - used_tokens
    omitted_count = 0
    truncated_count = 0

    for index, item in enumerate(deferred):
        tokens = estimate_tokens(item.summary)
        if tokens <= remaining:
            selected.append((item, False))
            section_tokens[_section(item)] += tokens
            remaining -= tokens
            continue
        if remaining > 0:
            summary, was_truncated = truncate_text(item.summary, remaining)
            selected.append((item.model_copy(update={"summary": summary}), was_truncated))
            section_tokens[_section(item)] += estimate_tokens(summary)
            truncated_count += int(was_truncated)
            remaining = 0
            omitted_count = len(deferred) - index - 1
        else:
            omitted_count = len(deferred) - index
        break

    selected = sorted(selected, key=lambda pair: _ranking_key(pair[0], 0))
    result = [item for item, _ in selected]
    return AssembledEvidence(
        evidence=result,
        warnings=list(warnings),
        truncation=TruncationInfo(
            total_budget=total_budget,
            used_tokens=sum(estimate_tokens(item.summary) for item in result),
            section_tokens=section_tokens,
            omitted_count=omitted_count,
            truncated_count=truncated_count,
            deduplicated_count=deduplicated_count,
        ),
    )


def _rank(items: Iterable[Evidence]) -> list[Evidence]:
    """Sort by diagnostic importance with explicit stable tie-breaking fields."""
    return [
        item
        for _, item in sorted(
            enumerate(items), key=lambda pair: _ranking_key(pair[1], pair[0])
        )
    ]


def _ranking_key(item: Evidence, input_position: int) -> tuple[float | int | str, ...]:
    """Return an order that favors high severity, then recent timestamped evidence."""
    timestamp_missing = item.timestamp_ms is None
    timestamp = 0.0 if timestamp_missing else item.timestamp_ms
    return (
        -item.severity,
        int(timestamp_missing),
        -timestamp,
        item.source,
        item.provenance,
        item.summary,
        input_position,
    )


def _deduplicate_network(items: list[Evidence]) -> tuple[list[Evidence], int]:
    """Keep the ranked representative of equivalent adapter-produced requests."""
    seen: set[tuple[str, str, str, int | None]] = set()
    retained: list[Evidence] = []
    duplicates = 0
    for item in items:
        if item.source != "network":
            retained.append(item)
            continue
        key = _network_key(item)
        if key is None:
            retained.append(item)
            continue
        if key in seen:
            duplicates += 1
            continue
        seen.add(key)
        retained.append(item)
    return retained, duplicates


def _network_key(item: Evidence) -> tuple[str, str, str, int | None] | None:
    """Extract identity only from the stable leading fields of an adapter summary."""
    fields = item.summary.split(" | ")
    if len(fields) < 2 or not fields[1].startswith("status="):
        return None
    method, separator, url = fields[0].partition(" ")
    if not separator or not method or not url:
        return None
    status = fields[1].removeprefix("status=").split(" ", 1)[0]
    rounded_second = (
        None if item.timestamp_ms is None else floor(item.timestamp_ms / 1_000 + 0.5)
    )
    return method, url, status, rounded_second


def _section(item: Evidence) -> str:
    """Map each normalized source to one preferred evidence allocation section."""
    return SECTION_BY_SOURCE[item.source]
