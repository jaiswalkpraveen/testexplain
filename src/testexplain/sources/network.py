"""Turn one HAR entry into network evidence.

Playwright writes the same record shape in two places. A trace stores each
request as a ``resource-snapshot`` whose payload is literally a HAR entry
(``packages/trace/src/snapshot.ts`` declares ``ResourceSnapshot = HAREntry``),
and a recorded ``.har`` file stores the same entry directly. So both adapters
share this module and differ only in what they can supply: a trace knows a
monotonic timestamp and no body, a HAR knows a wall-clock string and a body.

This module is pure formatting. It performs no I/O, so it does not know whether
an entry came from an archive, a file, or a test fixture.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from testexplain.models import Evidence, EvidenceProvenance
from testexplain.sources.common import finite_float

NETWORK_SEVERITY_BY_STATUS_CLASS = {5: 4, 4: 3, 3: 2}
NETWORK_FAILURE_SEVERITY = 4
NETWORK_ABORTED_SEVERITY = 3
NETWORK_UNKNOWN_SEVERITY = 2
DEFAULT_NETWORK_SEVERITY = 1
UNKNOWN_MIME_TYPE = "x-unknown"
UNKNOWN_FIELD = "unknown"
REDACTED_FIELD = "<redacted>"
MIN_HTTP_STATUS = 100
MAX_NETWORK_URL_LENGTH = 300
NETWORK_FLAGS = (
    ("_wasAborted", "aborted"),
    ("_wasFulfilled", "fulfilled"),
    ("_wasContinued", "continued"),
)


def har_entry_evidence(
    entry: dict[str, Any],
    *,
    provenance: EvidenceProvenance,
    timestamp_ms: float | None,
    body_preview: str | None = None,
) -> Evidence:
    """Summarize one HAR entry, rendering every Playwright sentinel as ``unknown``.

    Playwright pre-fills entries with ``status: -1``, ``time: -1``, and
    ``mimeType: "x-unknown"`` and overwrites them only when the real values
    arrive, so those sentinels must never be reported as if they were measurements.

    ``body_preview`` is supplied by the caller because reaching a body requires
    I/O this module deliberately does not do: a trace stores bodies as ``_sha1``
    pointers it never resolves, while a HAR either inlines the text or points at
    a separate archive member. It is appended last so the leading fields keep a
    stable position for readers and for later budget truncation.
    """
    request = entry.get("request")
    request = request if isinstance(request, dict) else {}
    response = entry.get("response")
    response = response if isinstance(response, dict) else {}

    method = request.get("method")
    method = method if isinstance(method, str) and method else UNKNOWN_FIELD.upper()
    status = http_status(response.get("status"))
    failure_text = response.get("_failureText")
    failure_text = failure_text if isinstance(failure_text, str) and failure_text else None

    parts = [
        f"{method} {sanitize_url(request.get('url'))}",
        f"status={format_status(status, response.get('statusText'))}",
        f"mime={format_mime_type(response)}",
        f"time={format_duration(entry.get('time'))}",
    ]
    if failure_text:
        parts.append(f"failure={failure_text}")
    parts.extend(label for key, label in NETWORK_FLAGS if entry.get(key) is True)
    if body_preview:
        parts.append(f"body={body_preview}")

    return Evidence(
        source="network",
        provenance=provenance,
        summary=" | ".join(parts),
        severity=network_severity(status, failure_text, entry),
        timestamp_ms=timestamp_ms,
    )


def http_status(value: Any) -> int | None:
    """Return a real HTTP status code, rejecting the ``-1`` "not yet known" sentinel."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= MIN_HTTP_STATUS else None


def format_status(status: int | None, status_text: Any) -> str:
    if status is None:
        return UNKNOWN_FIELD
    if isinstance(status_text, str) and status_text:
        return f"{status} {status_text}"
    return str(status)


def format_mime_type(response: dict[str, Any]) -> str:
    """Report the response content type, treating ``x-unknown`` as never learned."""
    content = response.get("content")
    mime_type = content.get("mimeType") if isinstance(content, dict) else None
    if not isinstance(mime_type, str) or not mime_type or mime_type == UNKNOWN_MIME_TYPE:
        return UNKNOWN_FIELD
    return mime_type


def format_duration(value: Any) -> str:
    """Render a HAR duration in milliseconds, treating negatives as unknown."""
    duration = finite_float(value)
    if duration is None or duration < 0:
        return UNKNOWN_FIELD
    return f"{duration:.1f}ms"


def network_severity(status: int | None, failure_text: str | None, entry: dict[str, Any]) -> int:
    """Rank a request so later budgeting keeps the most diagnostic traffic.

    Server errors and transport failures outrank client errors, which outrank
    redirects and requests whose outcome never became known; successful traffic
    ranks lowest because it rarely explains a failure.
    """
    if status is None:
        if failure_text:
            return NETWORK_FAILURE_SEVERITY
        if entry.get("_wasAborted") is True:
            return NETWORK_ABORTED_SEVERITY
        return NETWORK_UNKNOWN_SEVERITY
    return NETWORK_SEVERITY_BY_STATUS_CLASS.get(status // 100, DEFAULT_NETWORK_SEVERITY)


def sanitize_url(value: Any) -> str:
    """Strip secrets from a URL while preserving its diagnostic shape.

    Credentials become ``***``, the fragment is dropped because it never reaches
    the server, and query values are replaced while their names survive: the name
    is usually the useful signal and the value is where tokens hide.
    """
    if not isinstance(value, str) or not value:
        return UNKNOWN_FIELD
    try:
        split = urlsplit(value)
        if split.scheme and not split.netloc and not value.startswith(f"{split.scheme}://"):
            # An opaque URL such as data:, blob:, or javascript: keeps its whole
            # payload in the path, so only the scheme is safe to report.
            return f"{split.scheme}:{REDACTED_FIELD}"
        query = "&".join(
            pair if "=" not in pair else f"{pair.split('=', 1)[0]}={REDACTED_FIELD}"
            for pair in split.query.split("&")
            if pair
        )
        path = split.path if split.netloc else redact_authority_credentials(split.path)
        sanitized = urlunsplit(
            (split.scheme, redact_userinfo(split.netloc), path, query, "")
        )
    except ValueError:
        # The URL is unparsable, so drop everything that can carry a secret and
        # redact credentials by hand rather than trusting the raw text.
        sanitized = redact_unparsable_userinfo(value.split("#", 1)[0].split("?", 1)[0])
    if not sanitized:
        # Everything the URL contained was unsafe, so report nothing rather than
        # an empty field that would read as a real request to a bare host.
        return UNKNOWN_FIELD
    if len(sanitized) > MAX_NETWORK_URL_LENGTH:
        return sanitized[:MAX_NETWORK_URL_LENGTH] + "\u2026"
    return sanitized


def redact_userinfo(netloc: str) -> str:
    """Replace any ``user:password`` prefix with ``***``, keeping the host visible."""
    if "@" not in netloc:
        return netloc
    return f"***@{netloc.rsplit('@', 1)[1]}"


def redact_unparsable_userinfo(url: str) -> str:
    """Redact credentials in a URL that ``urlsplit`` refused to parse."""
    scheme, separator, remainder = url.partition("://")
    if not separator:
        return redact_authority_credentials(url)
    host, slash, path = remainder.partition("/")
    return f"{scheme}://{redact_userinfo(host)}{slash}{path}"


def redact_authority_credentials(value: str) -> str:
    """Redact ``user:password@`` in text that has no recognizable URL authority.

    Only a ``user:password`` pair is redacted. A bare ``@`` is left alone because
    it is ordinary path content, as in an ``/@scope/package`` request.
    """
    if "@" not in value:
        return value
    prefix, _, remainder = value.rpartition("@")
    boundary = max(prefix.rfind(character) for character in ("/", "\\"))
    credentials = prefix[boundary + 1 :]
    if ":" not in credentials:
        return value
    return f"{prefix[: boundary + 1]}***@{remainder}"
