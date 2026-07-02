from pydantic import BaseModel


class FailureContext(BaseModel):
    """Normalized representation of one failed test, LLM-agnostic."""
    test_title: str
    file: str
    status: str  # "failed" | "timedOut"
    error_message: str
    error_stack: str = ""
    duration_ms: int = 0


class FailureAnalysis(BaseModel):
    """M0: plain-English explanation. Becomes structured in M1."""
    test_title: str
    explanation: str
