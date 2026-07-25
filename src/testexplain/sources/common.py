"""Primitives shared by every artifact adapter.

An adapter reads one kind of test artifact and returns normalized evidence. The
adapters share a result shape and a small set of defensive converters, because
every artifact is untrusted input: a value that should be a number may be a
string, a boolean, or ``NaN``, and a value that should be base64 may be
truncated. These helpers turn each of those cases into ``None`` so a caller can
degrade one field instead of failing a whole artifact.
"""

from __future__ import annotations

import base64
import binascii
import math
from dataclasses import dataclass, field
from typing import Any

from testexplain.models import Evidence


@dataclass
class SourceResult:
    """What one adapter produced: the evidence it read and what it could not read.

    Warnings are data rather than log output so the caller can surface them next
    to the analysis. An adapter that hits a broken record appends a warning and
    keeps going, so a partial artifact still contributes everything it can.
    """

    evidence: list[Evidence] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def finite_float(value: Any) -> float | None:
    """Return ``value`` as a usable float, or ``None`` when it is not a real number.

    Booleans are rejected even though Python treats them as integers, because a
    ``True`` in a duration field is a broken record rather than one millisecond.
    Infinities and ``NaN`` are rejected too: they format as text no reader can
    act on, and they poison any later arithmetic.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        converted = float(value)
    except OverflowError:
        return None
    return converted if math.isfinite(converted) else None


def forgiving_b64decode(encoded: str) -> bytes | None:
    """Decode base64 the way ``atob`` does: ignore whitespace, tolerate lost padding.

    Artifacts are written by browsers and JavaScript tooling, which are laxer
    than Python's decoder. Whitespace is stripped and missing ``=`` padding is
    restored, but a length that leaves one leftover character can never be valid
    base64, so that case returns ``None`` rather than guessing.
    """
    stripped = "".join(encoded.split())
    remainder = len(stripped) % 4
    if remainder == 1:
        return None
    if remainder:
        stripped += "=" * (4 - remainder)
    try:
        return base64.b64decode(stripped, validate=True)
    except (binascii.Error, ValueError):
        return None


def reject_constant(value: str) -> None:
    """Refuse the non-finite literals ``json.loads`` would otherwise accept.

    Python's JSON reader accepts the non-standard ``NaN``, ``Infinity``, and
    ``-Infinity`` literals. Passing this as ``parse_constant`` turns them into a
    parse error, so a hostile artifact cannot smuggle a value that breaks
    comparisons and formatting downstream.
    """
    raise ValueError(f"non-finite JSON number: {value}")
