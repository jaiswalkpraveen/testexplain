from typing import Literal

from pydantic import BaseModel, Field

# The only category values the LLM is allowed to answer with.
# Literal = "exactly one of these strings, nothing else".
# If the LLM invents "network issue", validation fails loudly
# instead of silently corrupting downstream filters/counts.
Category = Literal[
    "product bug",
    "api outage",
    "environment",
    "infrastructure",
    "network",
    "automation bug",
    "flaky",
    "unknown",
]


class FailureContext(BaseModel):
    """Normalized representation of one failed test, LLM-agnostic."""
    test_title: str
    file: str
    status: str  # "failed" | "timedOut"
    error_message: str
    error_stack: str = ""
    duration_ms: int = 0


class FailureAnalysis(BaseModel):
    """M1: structured triage verdict for one failed test.

    Every field is machine-usable: you can filter by category,
    sort by confidence, and render evidence/next_steps as lists --
    none of which was possible with M0's free-text explanation.
    """

    test_title: str
    summary: str  # 1-2 sentence plain-English explanation
    suspected_category: Category
    evidence: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)  # 0 = guessing, 1 = certain
