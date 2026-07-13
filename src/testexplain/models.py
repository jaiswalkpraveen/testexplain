from typing import Literal

from pydantic import BaseModel, Field, computed_field

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


EvidenceSource = Literal["action", "console", "page_error", "stdout", "stderr", "network"]

EvidenceProvenance = Literal["trace", "har", "report"]


class Evidence(BaseModel):
    source: EvidenceSource
    provenance: EvidenceProvenance
    summary: str
    severity: int = Field(default=1, ge=1, le=4)
    timestamp_ms: float | None = None


class Attachment(BaseModel):
    name: str
    content_type: str = ""
    path: str | None = None
    body_b64: str | None = None


class FailedAttempt(BaseModel):
    spec_id: str = ""
    file: str = ""
    line: int = 0
    column: int = 0
    title_path: list[str] = Field(default_factory=list)
    project_id: str = ""
    project_name: str = ""
    test_ordinal: int = 0
    result_ordinal: int = 0
    retry: int = 0
    start_time: str = ""
    status: str
    expected_status: str = "passed"
    aggregate_status: str = "unexpected"
    error_message: str = ""
    error_stack: str = ""
    duration_ms: float = 0.0
    stdout: list[str] = Field(default_factory=list)
    stderr: list[str] = Field(default_factory=list)
    attachments: list[Attachment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def attempt_key(self) -> str:
        return "::".join(
            [
                self.spec_id,
                self.file,
                str(self.line),
                str(self.column),
                " > ".join(self.title_path),
                self.project_id,
                self.project_name,
                str(self.test_ordinal),
                str(self.result_ordinal),
                str(self.retry),
                self.start_time,
            ]
        )

    @computed_field
    @property
    def eventually_passed(self) -> bool:
        return self.aggregate_status == "flaky"


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
    confidence: float = Field(ge=0.0, le=1.0) # 0 = guessing, 1 = certain
    project: str = ""
    retry: int = 0
    flaky: bool = False
