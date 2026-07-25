"""Read a recorded HAR archive into network evidence.

Playwright can attach network traffic in two shapes. ``recordHar`` with a plain
``.har`` path writes one JSON document and inlines each response body as
``content.text``. A ``.har.zip`` path writes the same document as the member
``har.har`` and stores each body as a separate member named by
``content._file``. This adapter accepts both and tells them apart by sniffing
the file rather than trusting its suffix.

A HAR entry is the same record a trace stores as a ``resource-snapshot``, so the
summary, severity, and URL sanitation all come from
:mod:`testexplain.sources.network`. What this module adds is the part only a HAR
can supply: a wall-clock timestamp from ``startedDateTime`` and a bounded
preview of the response body of a failed request.

Every failure here is soft. A malformed entry, an absent body member, or an
unsafe archive path produces a warning and the remaining entries are still read,
because one damaged artifact must not discard evidence the pipeline can use.

Body previews are deliberately raw beyond whitespace collapsing. A response body
is arbitrary content rather than a structurally guaranteed secret location like
URL credentials, so pattern-based secret scrubbing belongs to the redaction
stage that runs before any prompt is built.
"""

from __future__ import annotations

import json
import re
import zipfile
import zlib
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from testexplain.sources.common import (
    SourceResult,
    forgiving_b64decode,
    reject_constant,
)
from testexplain.sources.network import (
    MAX_NETWORK_URL_LENGTH,
    har_entry_evidence,
    http_status,
)

# Re-exported so a caller that formats network evidence from a HAR can reach the
# URL budget without importing the shared normalizer directly.
__all__ = ["MAX_NETWORK_URL_LENGTH", "read_har"]

CANONICAL_HAR_MEMBER = "har.har"
HAR_SUFFIX = ".har"
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")
MIN_FAILED_HTTP_STATUS = 400
MAX_HAR_MEMBERS = 2000
MAX_HAR_MEMBER_SIZE = 100 * 1024 * 1024
MAX_HAR_TOTAL_SIZE = 500 * 1024 * 1024
MAX_HAR_ENTRIES = 100_000
MAX_HAR_EVIDENCE = 50_000
MAX_HAR_WARNINGS = 1_000
MAX_HAR_BODY_BYTES = 64 * 1024
MAX_HAR_BODY_PREVIEW = 500

# Reading one member can fail in several unrelated ways: a bad CRC, a truncated
# payload, an unsupported compression method, or a corrupt deflate stream.
MEMBER_ERRORS = (
    EOFError,
    NotImplementedError,
    OSError,
    RuntimeError,
    ValueError,
    zipfile.BadZipFile,
    zlib.error,
)

# Every reader in this module reports problems through one of these, so the
# caller collects warnings in a single place instead of raising mid-parse.
Warn = Callable[[str], None]


def read_har(path: Path) -> SourceResult:
    """Read one HAR artifact, plain or zipped, into network evidence."""
    result = SourceResult()
    suppressed = 0

    def warn(message: str) -> None:
        """Record a warning, keeping the list bounded on hostile input.

        A HAR with a million broken entries would otherwise produce a million
        warnings, which is itself a denial-of-service payload.
        """
        nonlocal suppressed
        if len(result.warnings) >= MAX_HAR_WARNINGS:
            suppressed += 1
            return
        result.warnings.append(message)

    # The shape is sniffed rather than taken from the suffix, because a HAR ZIP
    # may be named ``network.har``. ``is_zipfile`` swallows its own read errors
    # and answers ``False``, so an unreadable file is reported by the plain path.
    if zipfile.is_zipfile(path):
        _read_archive(path, result, warn)
    else:
        _read_plain(path, result, warn)

    if suppressed:
        result.warnings.append(f"{path.name}: {suppressed} warnings suppressed")
    return result


def _read_plain(path: Path, result: SourceResult, warn: Warn) -> None:
    """Read a standalone ``.har`` document written with inlined bodies."""
    try:
        size = path.stat().st_size
        if size > MAX_HAR_MEMBER_SIZE:
            warn(f"{path.name}: size limit exceeded; skipped")
            return
        raw = path.read_bytes()
    except OSError:
        warn(f"{path.name}: unreadable HAR; skipped")
        return

    _read_document(raw, result, warn, label=path.name, bodies=_BodyStore(None, {}, warn, 0))


def _read_archive(path: Path, result: SourceResult, warn: Warn) -> None:
    """Read a HAR ZIP, resolving each ``content._file`` against its members."""
    try:
        with zipfile.ZipFile(path) as archive:
            members = [info for info in archive.infolist() if not info.is_dir()]
            safe = []
            for info in members:
                if _is_safe_member(info.filename):
                    safe.append(info)
                else:
                    warn(f"{path.name}: unsafe member path {info.filename!r}; skipped")
            if len(safe) > MAX_HAR_MEMBERS:
                warn(
                    f"{path.name}: member limit exceeded;"
                    f" only first {MAX_HAR_MEMBERS} members processed"
                )
                safe = safe[:MAX_HAR_MEMBERS]

            document = _find_har_member(safe)
            if document is None:
                warn(f"{path.name}: no {HAR_SUFFIX} member found; skipped")
                return

            label = f"{path.name}/{document.filename}"
            if document.file_size > MAX_HAR_MEMBER_SIZE:
                warn(f"{label}: size limit exceeded; skipped")
                return
            try:
                raw = archive.read(document)
            except MEMBER_ERRORS:
                warn(f"{label}: unreadable member; skipped")
                return

            bodies = {
                info.filename: info for info in safe if info.filename != document.filename
            }
            _read_document(
                raw,
                result,
                warn,
                label=label,
                bodies=_BodyStore(archive, bodies, warn, len(raw)),
            )
    except (zipfile.BadZipFile, OSError, RuntimeError):
        warn(f"{path.name}: unreadable ZIP; skipped")


def _read_document(
    raw: bytes,
    result: SourceResult,
    warn: Warn,
    *,
    label: str,
    bodies: _BodyStore,
) -> None:
    """Validate the HAR 1.2 envelope, then normalize each entry it contains."""
    try:
        # ``utf-8-sig`` drops a byte-order mark, which the HAR specification
        # requires readers to ignore, and ``errors="replace"`` keeps a document
        # with a few bad bytes readable instead of discarding all of it.
        text = raw.decode("utf-8-sig", errors="replace")
        document = json.loads(text, parse_constant=reject_constant)
    except ValueError:
        warn(f"{label}: malformed JSON; skipped")
        return

    if not isinstance(document, dict):
        warn(f"{label}: expected JSON object; skipped")
        return
    log = document.get("log")
    if not isinstance(log, dict):
        warn(f"{label}: missing log object; skipped")
        return
    entries = log.get("entries")
    if not isinstance(entries, list):
        warn(f"{label}: missing log.entries list; skipped")
        return

    if len(entries) > MAX_HAR_ENTRIES:
        warn(
            f"{label}: entry limit exceeded;"
            f" only first {MAX_HAR_ENTRIES} entries processed"
        )
        entries = entries[:MAX_HAR_ENTRIES]

    for index, entry in enumerate(entries, start=1):
        if len(result.evidence) >= MAX_HAR_EVIDENCE:
            warn(f"{label}: evidence limit exceeded; remaining entries skipped")
            return
        prefix = f"{label} entry {index}"
        if not isinstance(entry, dict):
            warn(f"{prefix}: unusable HAR entry; skipped")
            continue
        try:
            result.evidence.append(
                har_entry_evidence(
                    entry,
                    provenance="har",
                    timestamp_ms=_epoch_milliseconds(entry.get("startedDateTime")),
                    body_preview=_body_preview(entry, bodies=bodies, warn=warn, prefix=prefix),
                )
            )
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            warn(f"{prefix}: formatting failed; skipped ({exc})")


def _epoch_milliseconds(value: Any) -> float | None:
    """Convert a HAR ``startedDateTime`` string to epoch milliseconds.

    A HAR entry has no monotonic clock, so this ISO 8601 string is its only
    timestamp. A naive string is read as UTC rather than local time so the same
    artifact yields the same number on every machine, and any unparsable value
    degrades to ``None`` rather than failing the entry.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp() * 1000
    except (OSError, OverflowError, ValueError):
        return None


def _body_preview(
    entry: dict[str, Any], *, bodies: _BodyStore, warn: Warn, prefix: str
) -> str | None:
    """Return a bounded preview of a failed request's response body.

    Only failed requests are previewed. A successful response body is usually
    large and rarely explains a failure, so spending the prompt budget on it
    would crowd out the traffic that does.
    """
    response = entry.get("response")
    response = response if isinstance(response, dict) else {}
    failure_text = response.get("_failureText")
    status = http_status(response.get("status"))
    failed = bool(isinstance(failure_text, str) and failure_text) or (
        status is not None and status >= MIN_FAILED_HTTP_STATUS
    )
    if not failed:
        return None

    content = response.get("content")
    if not isinstance(content, dict):
        return None

    external = content.get("_file")
    if isinstance(external, str) and external:
        # Playwright resolves ``_file`` as a member name exactly as written, so
        # it is looked up verbatim rather than joined onto a directory.
        raw = bodies.read(external, prefix)
        if raw is None:
            return None
        return _format_preview(raw)

    text = content.get("text")
    if not isinstance(text, str) or not text:
        return None
    if content.get("encoding") == "base64":
        decoded = forgiving_b64decode(text)
        if decoded is None:
            warn(f"{prefix}: malformed base64 body; body skipped")
            return None
        return _format_preview(decoded)
    return _format_preview(text.encode("utf-8", errors="replace"))


def _format_preview(raw: bytes) -> str | None:
    """Turn body bytes into one short, single-line, printable field.

    Bytes are decoded permissively because a body may be binary or truncated
    mid-character. Runs of whitespace collapse to single spaces because a body
    is the one field appended to an otherwise compact single-line network
    summary, and a pretty-printed JSON error page would otherwise spend the
    whole preview budget on indentation.
    """
    collapsed = " ".join(raw[:MAX_HAR_BODY_BYTES].decode("utf-8", errors="replace").split())
    if not collapsed:
        return None
    if len(collapsed) > MAX_HAR_BODY_PREVIEW:
        return collapsed[:MAX_HAR_BODY_PREVIEW] + "\u2026"
    return collapsed


def _is_safe_member(name: str) -> bool:
    """Reject archive paths that could escape the archive root.

    This adapter never writes members to disk, but a name is still used to look
    up a body member, so a traversal or absolute path is refused rather than
    resolved. Separators are normalized first because an archive written on
    Windows can use backslashes.
    """
    normalized = name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        return False
    if WINDOWS_DRIVE.match(normalized):
        return False
    return ".." not in normalized.split("/")


def _find_har_member(members: list[zipfile.ZipInfo]) -> zipfile.ZipInfo | None:
    """Locate the HAR document inside an archive.

    Playwright always writes ``har.har`` at the root, so that name wins. Any
    other ``.har`` member is accepted as a fallback, matching how Playwright's
    own reader locates a HAR, and ties are broken by name so the choice is
    deterministic rather than dependent on member order.
    """
    for info in members:
        if info.filename == CANONICAL_HAR_MEMBER:
            return info
    candidates = sorted(
        (info for info in members if info.filename.lower().endswith(HAR_SUFFIX)),
        key=lambda info: info.filename,
    )
    return candidates[0] if candidates else None


class _BodyStore:
    """Resolves ``content._file`` pointers to bytes under the archive's budgets.

    A plain HAR has no archive to read from, so it is constructed with no
    archive and reports that the body is out of reach instead of touching the
    filesystem next to the artifact.
    """

    def __init__(
        self,
        archive: zipfile.ZipFile | None,
        members: dict[str, zipfile.ZipInfo],
        warn: Warn,
        consumed: int,
    ) -> None:
        self._archive = archive
        self._members = members
        self._warn = warn
        self._consumed = consumed

    def read(self, name: str, prefix: str) -> bytes | None:
        """Read at most ``MAX_HAR_BODY_BYTES`` from the member called ``name``."""
        if self._archive is None:
            self._warn(
                f"{prefix}: external body member {name!r} requires a HAR ZIP; body skipped"
            )
            return None
        info = self._members.get(name)
        if info is None:
            self._warn(f"{prefix}: body member {name!r} not found; body skipped")
            return None
        if self._consumed + min(info.file_size, MAX_HAR_BODY_BYTES) > MAX_HAR_TOTAL_SIZE:
            self._warn(f"{prefix}: total size limit exceeded; body skipped")
            return None
        try:
            with self._archive.open(info) as member:
                raw = member.read(MAX_HAR_BODY_BYTES)
        except MEMBER_ERRORS:
            self._warn(f"{prefix}: unreadable body member {name!r}; body skipped")
            return None
        self._consumed += len(raw)
        return raw
