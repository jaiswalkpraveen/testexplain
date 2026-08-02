"""Pure, deterministic primitives for evidence context budgeting.

The token count is intentionally an estimate: provider tokenizers differ, while
``ceil(character_count / 4)`` is reproducible without a provider SDK.
"""

from __future__ import annotations

from math import ceil
from typing import Final

DEFAULT_TOTAL_TOKEN_BUDGET: Final = 4_000
DEFAULT_SECTION_TARGETS: Final = {
    "actions": 1_400,
    "console_output": 1_000,
    "network": 1_400,
    "notes": 200,
}


def estimate_tokens(text: str) -> int:
    """Return the deterministic ``ceil(character_count / 4)`` token estimate."""
    return ceil(len(text) / 4)


def section_capacities(total_budget: int) -> dict[str, int]:
    """Scale preferred section targets to ``total_budget`` with stable rounding.

    The targets are preferences rather than reservations.  This function gives
    assembly its first-pass capacities; unused capacity is later reusable.
    """
    if total_budget <= 0:
        raise ValueError("total_budget must be positive")

    target_total = sum(DEFAULT_SECTION_TARGETS.values())
    capacities = {
        name: total_budget * target // target_total
        for name, target in DEFAULT_SECTION_TARGETS.items()
    }
    remainder = total_budget - sum(capacities.values())
    for name in DEFAULT_SECTION_TARGETS:
        if remainder == 0:
            break
        capacities[name] += 1
        remainder -= 1
    return capacities


def truncate_text(text: str, token_budget: int) -> tuple[str, bool]:
    """Return text fitting ``token_budget`` and whether a deterministic cut occurred.

    An ellipsis signals that content was omitted.  It is counted inside the
    supplied budget, so the returned text always fits the stated estimate.
    """
    if token_budget < 0:
        raise ValueError("token_budget must not be negative")
    if estimate_tokens(text) <= token_budget:
        return text, False
    if token_budget == 0:
        return "", True

    character_limit = token_budget * 4
    if character_limit == 1:
        return "…", True
    prefix = text[: character_limit - 1].rstrip()
    if prefix.endswith("|"):
        prefix = prefix[:-1].rstrip()
    return prefix + "…", True
