"""Tests for deterministic evidence-budget primitives."""

import pytest

from testexplain.assembly.budget import (
    DEFAULT_SECTION_TARGETS,
    DEFAULT_TOTAL_TOKEN_BUDGET,
    estimate_tokens,
    section_capacities,
    truncate_text,
)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("", 0),
        ("a", 1),
        ("abcd", 1),
        ("abcde", 2),
        ("é猫", 1),
        ("x" * 4_001, 1_001),
    ],
)
def test_estimate_tokens_uses_the_ceiling_of_characters_divided_by_four(
    text: str, expected: int
) -> None:
    """The estimate is deterministic for empty, Unicode, and long strings."""
    assert estimate_tokens(text) == expected


def test_default_section_targets_describe_the_four_thousand_token_budget() -> None:
    """Targets are preferred allocations whose total matches the design budget."""
    assert DEFAULT_TOTAL_TOKEN_BUDGET == 4_000
    assert DEFAULT_SECTION_TARGETS == {
        "actions": 1_400,
        "console_output": 1_000,
        "network": 1_400,
        "notes": 200,
    }
    assert sum(DEFAULT_SECTION_TARGETS.values()) == DEFAULT_TOTAL_TOKEN_BUDGET


def test_section_capacities_scale_preferred_targets_to_a_custom_budget() -> None:
    """Small deterministic test budgets keep each section's relative share."""
    assert section_capacities(40) == {
        "actions": 14,
        "console_output": 10,
        "network": 14,
        "notes": 2,
    }


def test_section_capacities_assign_rounding_remainder_deterministically() -> None:
    """Integer capacity totals must exactly equal the caller's overall budget."""
    capacities = section_capacities(7)
    assert capacities == {
        "actions": 3,
        "console_output": 2,
        "network": 2,
        "notes": 0,
    }
    assert sum(capacities.values()) == 7


@pytest.mark.parametrize("value", [0, -1])
def test_section_capacities_reject_non_positive_total_budgets(value: int) -> None:
    """A selector cannot make a useful result from zero or negative capacity."""
    with pytest.raises(ValueError, match="total_budget must be positive"):
        section_capacities(value)


def test_truncate_text_keeps_text_within_its_token_budget_and_reports_truncation() -> None:
    """The helper returns exact metadata instead of silently changing evidence."""
    text, was_truncated = truncate_text("abcdefghijk", 2)
    assert text == "abcdefg…"
    assert was_truncated is True
    assert estimate_tokens(text) <= 2


def test_truncate_text_never_leaves_the_summary_field_separator_dangling() -> None:
    """A cut respects the adapter's `` | `` field boundary when one fits."""
    text, was_truncated = truncate_text("GET https://app.test/login | status=500", 7)
    assert text == "GET https://app.test/login…"
    assert was_truncated is True


def test_truncate_text_preserves_text_that_already_fits() -> None:
    """A fitting record is emitted byte-for-byte unchanged."""
    assert truncate_text("abcdefgh", 2) == ("abcdefgh", False)
