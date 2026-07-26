"""Remove secrets from evidence text before any prompt is composed.

Everything that reaches a prompt is sent verbatim to a third-party model, and
``core.build_prompt`` asks that model to quote the evidence back in its answer.
So a secret that survives this module is a secret that leaves the machine and
then reappears in a report. Redaction has to happen before composition, not
after: once text is concatenated into a prompt there is no reliable way to tell
a credential from an assertion message.

The rules are deliberately boring. Every pattern is a literal key name, a
delimiter, and a bounded value, or a published vendor prefix. Nothing here
guesses at entropy, because a heuristic that sometimes deletes a stack trace is
worse for triage than one that sometimes keeps an unrecognizable string.

The guiding rule, copied from ``sources.network.sanitize_url``: **redact the
value, keep the name.** ``token=<redacted>`` still tells the model that
authentication was involved, which is exactly the signal triage needs.
"""

import re
from collections.abc import Iterable

from testexplain.models import Evidence
from testexplain.sources.network import REDACTED_FIELD

# Redacting one span can uncover a secret that was glued to it, so the rules run
# repeatedly until the text stops changing. Real inputs settle in one or two
# rounds; the bound only exists so a pathological string cannot loop forever.
MAX_REDACTION_ROUNDS = 8

# Authentication schemes worth keeping as labels. "Bearer <redacted>" says more
# than "<redacted>" and gives away nothing.
SCHEME = r"(?:Bearer|Basic|Digest|Token|Negotiate|NTLM|Hawk|OAuth|SSWS|ApiKey)"

# A key starts either after a non-word character or at a camel-case hump, so
# "userPassword" is recognized while "passwordless" is not.
LEFT_BOUNDARY = r"(?:(?<!\w)|(?-i:(?<=[a-z0-9])(?=[A-Z])))"

# One ':' or '=' with optional horizontal space. Newlines are excluded so a key
# at the end of a line cannot claim the next line as its value.
SEPARATOR = r"[ \t]*[:=][ \t]*"

# Values that mean "there was no value". Keeping them preserves the diagnosis.
ABSENT_VALUE = (
    r"(?!(?:null|undefined|nil|none|true|false|nan|omit|include|same-origin)(?!\w))"
)

# Up to three vendor or transport prefixes, as in "x-amz-security-token". The
# possessive quantifier (Python 3.11+) stops the engine from backtracking into
# the prefix, which keeps the match linear.
KEY_PREFIX = r"(?:[A-Za-z0-9]{1,24}+[_.\-]){0,3}"

KEY_CORE = r"""(?: api[_.\-]?keys? | (?:private|secret|encryption|signing)[_.\-]?keys?
              | passw(?:or)?ds? | pwd | passphrases? | credentials? | secrets? | tokens? )"""

# A header value may contain spaces, but it must stop before the next "key:".
NEXT_KEY = r"(?![ \t]+[A-Za-z][A-Za-z0-9_.\-]{0,40}[\"']?[ \t]*[:=])"

# Never consume an existing marker: that is what makes redaction idempotent.
KEPT_MARKER = rf"(?!{re.escape(REDACTED_FIELD)})"

# Never cross the ' | ' separator that Evidence summaries are built from.
FIELD_BREAK = r"(?! \| )"

_STEP = rf"{KEPT_MARKER}{FIELD_BREAK}"

# A header-style value: spaces allowed, but bounded by the summary grammar.
HEADER_VALUE = rf"{_STEP}[^\s;,\"'](?:{_STEP}{NEXT_KEY}[^\n;,\"'])*"

# A quoted value: anything up to the closing quote, minus the guards above.
QUOTED_VALUE = rf"(?:{_STEP}[^\"'\n])+"

# A bare value: ends at the first delimiter.
BARE_VALUE = r"[^\s;,&\"'}\])]+"

AUTHORIZATION_RULE = re.compile(
    rf"""{LEFT_BOUNDARY}(?:proxy-)?authorization["']?{SEPARATOR}["']?
        # An atomic group: once the scheme is taken it is never given back, so a
        # second pass cannot re-match "Bearer" as though it were the credential.
        (?>(?:(?P<scheme>{SCHEME})[ \t]+)?)
        (?P<value>{HEADER_VALUE})""",
    re.IGNORECASE | re.VERBOSE,
)

KEY_RULE = re.compile(
    rf"""{LEFT_BOUNDARY}{KEY_PREFIX}{KEY_CORE}["']?{SEPARATOR}
        (?:(?P<scheme>{SCHEME})[ \t]+)?
        (?: (?P<quote>["'])(?P<quoted>{QUOTED_VALUE})(?P=quote)(?=[\s;,&|}}\])]|\Z)
          | (?P<object>\{{[^{{}}\n]*\}})
          | (?P<array>\[[^\[\]\n]*\])
          | ["']?{ABSENT_VALUE}(?P<bare>{BARE_VALUE}) )""",
    re.IGNORECASE | re.VERBOSE,
)

# A cookie jar is parsed structurally rather than swallowed to end of line, so a
# following field such as "| status=200" is never eaten.
COOKIE_PAIR = r"[A-Za-z0-9_.\-]{1,64}=[^\s;,|\"']*"
COOKIE_ATTRIBUTE_PAIR = (
    r"(?:Path|Domain|Expires|Max-Age|SameSite|Priority|Version|Comment)"
    r"[ \t]*=[ \t]*[^;|\"'\n]*"
)
COOKIE_ATTRIBUTE_FLAG = r"(?:HttpOnly|Secure|Partitioned)"
COOKIE_SEGMENT = rf"(?:{COOKIE_ATTRIBUTE_PAIR}|{COOKIE_ATTRIBUTE_FLAG}|{COOKIE_PAIR})"
COOKIE_JAR = rf"(?:;[ \t]*)*{COOKIE_SEGMENT}(?:[ \t]*;[ \t]*{COOKIE_SEGMENT})*"

COOKIE_RULE = re.compile(
    rf"""{LEFT_BOUNDARY}(?:set-)?cookies?["']?{SEPARATOR}["']?
        (?:(?P<jar>{COOKIE_JAR})|(?P<opaque>{HEADER_VALUE}))""",
    re.IGNORECASE | re.VERBOSE,
)

COOKIE_ATTRIBUTE_NAME = re.compile(
    r"(?:Path|Domain|Expires|Max-Age|SameSite|Priority|Partitioned|Version|Comment"
    r"|HttpOnly|Secure)\Z",
    re.IGNORECASE,
)
COOKIE_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,64}\Z")

# Self-identifying secrets: shapes their vendors publish, so no key is needed.
# Each prefix was checked against the gitleaks rule set rather than recalled.
VENDOR_RULE = re.compile(
    r"""(?<![A-Za-z0-9_])(?:
          gh[pousr]_[0-9A-Za-z]{16,255}                     # GitHub tokens
        | github_pat_[0-9A-Za-z_]{20,255}                   # GitHub fine-grained PAT
        | sk-ant-(?:api|admin)[0-9]{2}-[0-9A-Za-z_-]{20,255}  # Anthropic
        | sk-(?:proj|svcacct|admin)-[0-9A-Za-z_-]{20,255}   # OpenAI
        | (?:sk|pk|rk)[_-](?:test|live|prod)[_-][0-9A-Za-z]{10,99}
        | (?:A3T[A-Z0-9]|AKIA|ASIA|ABIA|ACCA)[A-Z2-7]{16}(?![A-Z2-7])  # AWS key id
        | AIza[0-9A-Za-z_-]{35}                             # Google API key
        | xox[abeprs]-[0-9A-Za-z-]{8,255}                   # Slack
        | xapp-[0-9]-[0-9A-Za-z-]{8,255}                    # Slack app-level
        | ey[0-9A-Za-z_-]{12,}\.ey[0-9A-Za-z._-]{12,}(?:\.[0-9A-Za-z_-]*)?  # JWT
        | -----BEGIN[A-Z\ ]{0,40}PRIVATE\ KEY-----          # PEM private key
        )""",
    re.VERBOSE,
)

# A credential after a bare scheme, with no key name anywhere: "Bearer abc123".
BARE_SCHEME_RULE = re.compile(
    rf"(?<!\w)(?P<scheme>{SCHEME})[ \t]+(?P<credential>[A-Za-z0-9+/=._~-]{{6,}})",
    re.IGNORECASE,
)


def _replace(match: re.Match[str], *names: str, trim: bool = False) -> str:
    """Rebuild the matched text with the first group that matched swapped for the marker.

    Only the value is replaced; every character the pattern matched around it —
    the key, the quotes, the scheme — is written back unchanged. ``trim`` leaves
    trailing whitespace in place, which is what keeps ``'Authorization: '`` from
    collapsing into ``'Authorization:<redacted>'`` and breaking the ' | ' grammar.
    """
    whole, base = match.group(0), match.start(0)
    for name in names:
        if match.group(name) is None:
            continue
        start, end = match.start(name) - base, match.end(name) - base
        if trim:
            value = whole[start:end]
            end -= len(value) - len(value.rstrip())
        return whole[:start] + REDACTED_FIELD + whole[end:]
    return whole


def _looks_like_a_credential(value: str) -> bool:
    """Report whether text after a bare scheme looks like a credential rather than prose.

    Without this, "Bearer authentication required" would lose its last word.
    """
    return (
        any(character.isdigit() for character in value)
        or any(character in "+/=._~-" for character in value)
        or len(value) >= 20
    )


def _redact_cookie_segment(segment: str) -> str:
    """Redact one ';'-delimited cookie segment, keeping its name and any attributes.

    Anything that does not parse as ``name=value`` fails closed: the whole
    segment is replaced, because an unparsable segment could be the secret.
    """
    stripped = segment.strip()
    if not stripped or stripped == REDACTED_FIELD:
        return segment
    lead = segment[: len(segment) - len(segment.lstrip())]
    trail = segment[len(segment.rstrip()) :]
    name, separator, value = stripped.partition("=")
    name = name.rstrip()
    if COOKIE_ATTRIBUTE_NAME.match(name):
        return segment
    if not separator or not COOKIE_NAME.match(name):
        return lead + REDACTED_FIELD + trail
    if value and not value.strip("="):
        # A trailing '=' is base64 padding, not a name/value separator.
        return lead + REDACTED_FIELD + trail
    if not value or value == REDACTED_FIELD:
        return segment
    return f"{lead}{name}={REDACTED_FIELD}{trail}"


def _redact_cookie(match: re.Match[str]) -> str:
    """Redact a cookie header, pair by pair when it parses and wholesale when it does not."""
    jar = match.group("jar")
    if jar is None:
        return _replace(match, "opaque", trim=True)
    whole, base = match.group(0), match.start(0)
    start, end = match.start("jar") - base, match.end("jar") - base
    cleaned = ";".join(_redact_cookie_segment(segment) for segment in jar.split(";"))
    return whole[:start] + cleaned + whole[end:]


def _redact_once(text: str) -> str:
    """Run every rule a single time, in the order they must not be applied out of.

    Authorization runs first because its value may contain characters the other
    rules would claim. Cookies run after the generic key rule so that a stray
    "token=" inside a jar is handled by the rule that understands it. Vendor
    shapes run late, catching whatever had no key beside it, and the bare-scheme
    rule runs last on what is left.
    """
    text = AUTHORIZATION_RULE.sub(lambda m: _replace(m, "value", trim=True), text)
    text = KEY_RULE.sub(lambda m: _replace(m, "quoted", "object", "array", "bare"), text)
    text = COOKIE_RULE.sub(_redact_cookie, text)
    text = VENDOR_RULE.sub(lambda m: REDACTED_FIELD, text)
    return BARE_SCHEME_RULE.sub(
        lambda m: _replace(m, "credential")
        if _looks_like_a_credential(m.group("credential"))
        else m.group(0),
        text,
    )


def redact_text(text: str) -> str:
    """Return the text with recognized secrets replaced by ``<redacted>``.

    Applying the rules repeatedly until nothing changes makes the function
    idempotent by construction: ``redact_text(redact_text(x)) == redact_text(x)``
    for every input. That matters because two secrets can be written with no
    delimiter between them, so removing the first is what reveals the second.
    """
    for _round in range(MAX_REDACTION_ROUNDS):
        redacted = _redact_once(text)
        if redacted == text:
            return text
        text = redacted
    return text


def redact_evidence(item: Evidence) -> Evidence:
    """Return a copy of the evidence with its summary redacted.

    A copy rather than an edit in place: the same Evidence object may be read
    more than once while Task 9 ranks and budgets, and silently rewriting shared
    state would make those decisions depend on evaluation order.
    """
    return item.model_copy(update={"summary": redact_text(item.summary)})


def redact_all(items: Iterable[Evidence]) -> list[Evidence]:
    """Redact every evidence item, preserving order."""
    return [redact_evidence(item) for item in items]
