import pytest

from testexplain.assembly.redact import redact_all, redact_evidence, redact_text
from testexplain.models import Evidence

REDACTED = "<redacted>"


def _assert_redacts(source: str, expected: str) -> None:
    """Assert the exact redacted output, and that redacting it again changes nothing.

    Every case checks idempotence because Task 9 may redact at more than one
    point in the pipeline, and a rule that redacts its own marker would corrupt
    text a little more on every pass.
    """
    actual = redact_text(source)
    assert actual == expected
    assert redact_text(actual) == actual


def _evidence(summary: str) -> Evidence:
    """Build one network Evidence item with every non-summary field set to a distinct value."""
    return Evidence(
        source="network",
        provenance="har",
        summary=summary,
        severity=3,
        timestamp_ms=1234.5,
    )


# --- authorization -------------------------------------------------------


def test_redact_authorization_bearer_keeps_the_scheme() -> None:
    _assert_redacts(
        "Authorization: Bearer abc123",
        f"Authorization: Bearer {REDACTED}",
    )


def test_redact_authorization_basic_keeps_the_scheme() -> None:
    _assert_redacts(
        "Authorization: Basic dXNlcjpwYXNz",
        f"Authorization: Basic {REDACTED}",
    )


def test_redact_authorization_unknown_scheme_redacts_the_whole_value() -> None:
    """An unrecognized scheme is treated as part of the secret, not as a label."""
    _assert_redacts("Authorization: Acme xyz789", f"Authorization: {REDACTED}")


def test_redact_authorization_is_case_insensitive() -> None:
    _assert_redacts("AUTHORIZATION=Bearer tok", f"AUTHORIZATION=Bearer {REDACTED}")


def test_redact_proxy_authorization() -> None:
    _assert_redacts(
        "Proxy-Authorization: Basic abc",
        f"Proxy-Authorization: Basic {REDACTED}",
    )


def test_redact_authorization_with_empty_value_is_unchanged() -> None:
    """A missing credential is diagnostic signal, not a secret, so nothing is invented."""
    _assert_redacts("Authorization: ", "Authorization: ")


def test_redact_authorization_with_no_delimiter_is_unchanged() -> None:
    _assert_redacts("Authorization:", "Authorization:")


def test_redact_authorization_inside_a_network_summary_keeps_the_field_grammar() -> None:
    _assert_redacts(
        "failure=Authorization: Bearer abc | aborted",
        f"failure=Authorization: Bearer {REDACTED} | aborted",
    )


def test_redact_authorization_as_a_json_field() -> None:
    _assert_redacts(
        '{"authorization":"Bearer abc"}',
        '{"authorization":"Bearer ' + REDACTED + '"}',
    )


# --- cookies -------------------------------------------------------------


def test_redact_cookie_redacts_every_pair_and_keeps_every_name() -> None:
    _assert_redacts(
        "Cookie: session=abc; theme=dark",
        f"Cookie: session={REDACTED}; theme={REDACTED}",
    )


def test_redact_set_cookie_keeps_attributes() -> None:
    """Path, HttpOnly and SameSite describe the cookie; only the value is secret."""
    _assert_redacts(
        "Set-Cookie: sid=abc; Path=/; HttpOnly; SameSite=Lax",
        f"Set-Cookie: sid={REDACTED}; Path=/; HttpOnly; SameSite=Lax",
    )


def test_redact_set_cookie_keeps_an_expires_attribute_containing_commas() -> None:
    _assert_redacts(
        "Set-Cookie: sid=abc; Expires=Wed, 21 Oct 2015 07:28:00 GMT",
        f"Set-Cookie: sid={REDACTED}; Expires=Wed, 21 Oct 2015 07:28:00 GMT",
    )


def test_redact_set_cookie_fails_closed_when_the_value_is_not_a_pair() -> None:
    """An opaque cookie value has no name to keep, so the whole value goes."""
    _assert_redacts("Set-Cookie:abc123", f"Set-Cookie:{REDACTED}")


def test_redact_cookie_keeps_the_following_summary_fields() -> None:
    _assert_redacts(
        "Cookie: a=1; b=2 | status=200",
        f"Cookie: a={REDACTED}; b={REDACTED} | status=200",
    )


def test_redact_cookie_with_an_empty_value_is_unchanged() -> None:
    _assert_redacts("cookie: a=; b=2", f"cookie: a=; b={REDACTED}")


def test_redact_cookie_inside_a_json_field() -> None:
    _assert_redacts(
        '{"Cookie": "a=1; b=2"}',
        '{"Cookie": "a=' + REDACTED + "; b=" + REDACTED + '"}',
    )


def test_redact_cookie_base64_padding_is_not_treated_as_a_pair() -> None:
    """A trailing '=' is base64 padding, not a name/value separator, so fail closed."""
    _assert_redacts(
        "Cookie: a=1;dXNlcjpwYXNzd29yZA==",
        f"Cookie: a={REDACTED};{REDACTED}",
    )


# --- api keys ------------------------------------------------------------


def test_redact_api_key_json_field() -> None:
    _assert_redacts('{"api_key":"secret-value"}', '{"api_key":"' + REDACTED + '"}')


def test_redact_x_api_key_header() -> None:
    _assert_redacts("X-Api-Key: sk-FAKE-1234", f"X-Api-Key: {REDACTED}")


@pytest.mark.parametrize(
    "spelling",
    ["Api_Key=abc", "api-key: abc", "apikey=abc", "x-api-key=abc", "API_KEY: abc"],
)
def test_redact_api_key_spellings(spelling: str) -> None:
    key, delimiter, _value = spelling.partition("abc")
    _assert_redacts(spelling, f"{key}{REDACTED}")


def test_redact_camel_case_key() -> None:
    """'userPassword' has no delimiter before 'Password', so a camel hump is a boundary."""
    _assert_redacts("userPassword=hunter2", f"userPassword={REDACTED}")


def test_redact_vendor_prefixed_header_name() -> None:
    _assert_redacts("x-amz-security-token: abc", f"x-amz-security-token: {REDACTED}")


# --- query secrets in free text -----------------------------------------


def test_redact_query_secrets_in_free_text() -> None:
    """sanitize_url only cleans the URL field; the same text in a body is Task 8's job."""
    _assert_redacts(
        "?token=abc&password=x",
        f"?token={REDACTED}&password={REDACTED}",
    )


# --- json fields ---------------------------------------------------------


def test_redact_json_password_keeps_sibling_fields() -> None:
    _assert_redacts(
        '{"password": "hunter2", "keep": 1}',
        '{"password": "' + REDACTED + '", "keep": 1}',
    )


def test_redact_json_single_quoted_with_spacing() -> None:
    _assert_redacts("{'token' : 'abc'}", "{'token' : '" + REDACTED + "'}")


def test_redact_json_with_mismatched_quotes() -> None:
    _assert_redacts("token':'abc123", "token':'" + REDACTED)


def test_redact_json_access_and_refresh_tokens() -> None:
    _assert_redacts(
        '{"access_token":"a.b.c","refresh_token":"d"}',
        '{"access_token":"' + REDACTED + '","refresh_token":"' + REDACTED + '"}',
    )


def test_redact_json_nested_object_value() -> None:
    """A whole object is redacted, because any of its fields could be the secret."""
    _assert_redacts(
        '{"client_secret":{"nested":"deep"}}',
        '{"client_secret":' + REDACTED + "}",
    )


def test_redact_json_array_value() -> None:
    _assert_redacts('{"token": [1,2,3]}', '{"token": ' + REDACTED + "}")


def test_redact_json_numeric_value() -> None:
    _assert_redacts('{"secret": 12345}', '{"secret": ' + REDACTED + "}")


@pytest.mark.parametrize("literal", ["null", "undefined", "nil", "none", "false"])
def test_redact_keeps_absent_value_literals(literal: str) -> None:
    """'the token was missing' is a diagnosis; replacing it would destroy the clue."""
    _assert_redacts('{"password": %s}' % literal, '{"password": %s}' % literal)


def test_redact_keeps_a_credentials_mode() -> None:
    _assert_redacts("credentials: include", "credentials: include")


def test_redact_keeps_an_empty_quoted_value() -> None:
    _assert_redacts('password=""', 'password=""')


# --- ordinary text -------------------------------------------------------


@pytest.mark.parametrize(
    "ordinary",
    [
        "passwordless: true",
        "tokenizer: fast",
        "secretary: alice",
        "authorization_required",
        "expect(page.getByLabel('Password')).toBeVisible()",
        "key_length = 32",
        "secret_length = 44",
        "ordinary failure: expected 200 but got 404",
        "Bearer authentication required",
        "",
        " ",
    ],
)
def test_redact_leaves_ordinary_text_untouched(ordinary: str) -> None:
    _assert_redacts(ordinary, ordinary)


def test_redact_value_containing_the_delimiter() -> None:
    _assert_redacts('password="a=b;c"', 'password="' + REDACTED + '"')


# --- multi-line ----------------------------------------------------------


def test_redact_multiline_keeps_the_following_line() -> None:
    """Trace console and stderr summaries already contain newlines."""
    _assert_redacts(
        "error: line1 token=abc\nline2 stays",
        f"error: line1 token={REDACTED}\nline2 stays",
    )


def test_redact_multiline_authorization() -> None:
    _assert_redacts(
        "a\nAuthorization: Bearer x\nb",
        f"a\nAuthorization: Bearer {REDACTED}\nb",
    )


def test_redact_multiline_cookie() -> None:
    _assert_redacts("a\nCookie: s=1\nb", f"a\nCookie: s={REDACTED}\nb")


def test_redact_redacts_every_line_not_just_the_first() -> None:
    _assert_redacts("token=a\ntoken=b", f"token={REDACTED}\ntoken={REDACTED}")


# --- bare scheme ---------------------------------------------------------


def test_redact_bare_bearer_credential_in_a_failure_string() -> None:
    _assert_redacts(
        "failure=auth failed for Bearer abc123",
        f"failure=auth failed for Bearer {REDACTED}",
    )


# --- vendor-prefixed values ---------------------------------------------


@pytest.mark.parametrize(
    "secret",
    [
        "ghp_FAKE1234567890abcdefghijklmnopqrstuv",
        "github_pat_" + "x" * 82,
        "AKIAIOSFODNN7EXAMPLQ",
        "AIza" + "b" * 35,
        "xoxb-1234567890-abcdefghijk",
        "sk-ant-api03-" + "z" * 93 + "AA",
        "sk-live-abcdef1234567890",
        "eyJhbGciOiJub25lIn0.eyJzdWIiOiJ0ZXN0LXVzZXIifQ.",
    ],
)
def test_redact_vendor_prefixed_secret_without_a_key(secret: str) -> None:
    """These shapes are self-identifying, so they are caught with no key nearby."""
    _assert_redacts(secret, REDACTED)


def test_redact_vendor_prefixed_secret_inside_a_url_path() -> None:
    _assert_redacts(
        "/users/ghp_FAKE1234567890abcdefghijklmnopqrstuv/profile",
        f"/users/{REDACTED}/profile",
    )


def test_redact_adjacent_vendor_secrets_are_both_removed() -> None:
    """Redacting the first secret exposes the second one's left boundary."""
    _assert_redacts(
        "AKIAIOSFODNN7EXAMPLQ ghp_FAKE1234567890abcdefghijklmnopqrstuv",
        f"{REDACTED} {REDACTED}",
    )


def test_redact_the_same_secret_twice() -> None:
    _assert_redacts("token=abc | token=abc", f"token={REDACTED} | token={REDACTED}")


def test_redact_several_different_secrets_in_one_string() -> None:
    _assert_redacts(
        "Authorization: Bearer abc | Cookie: s=1 | api_key=xyz",
        f"Authorization: Bearer {REDACTED} | Cookie: s={REDACTED} | api_key={REDACTED}",
    )


# --- documented limitations ---------------------------------------------


def test_redact_does_not_touch_an_unrecognizable_path_secret() -> None:
    """A path segment is indistinguishable from an ID, so generic paths are out of scope.

    Blanking every path segment would delete the most diagnostic part of a
    request. Vendor-prefixed secrets in a path are still caught, because those
    shapes identify themselves.
    """
    _assert_redacts(
        "/users/my-secret-token/profile",
        "/users/my-secret-token/profile",
    )


def test_redact_does_not_touch_a_locator_shaped_argument() -> None:
    """'fill(#password, "hunter2")' has no key/value delimiter to anchor on."""
    _assert_redacts(
        'fill(#password, "hunter2")',
        'fill(#password, "hunter2")',
    )


# --- the marker itself ---------------------------------------------------


@pytest.mark.parametrize(
    "already",
    [
        "token=<redacted>",
        "Cookie: a=<redacted>",
        "Bearer <redacted>",
        "Authorization: <redacted>",
        "GET https://x.test/a?token=<redacted> | status=401 | mime=unknown | time=1.0ms",
    ],
)
def test_redact_never_nests_the_marker(already: str) -> None:
    _assert_redacts(already, already)


# --- hostile input -------------------------------------------------------


@pytest.mark.parametrize(
    "hostile",
    ["\x00", "\ud800", "पासवर्ड ключ", "\x00\ud800 ordinary", "a" * 10000],
)
def test_redact_survives_hostile_input(hostile: str) -> None:
    _assert_redacts(hostile, hostile)


def test_redact_handles_a_very_long_secret() -> None:
    _assert_redacts("token=" + "a" * 10000, f"token={REDACTED}")


@pytest.mark.parametrize("wrong", [None, 123, b"token=abc", ["token=abc"]])
def test_redact_rejects_anything_that_is_not_text(wrong: object) -> None:
    """The signature takes str, so a wrong type is the caller's bug and must not pass silently.

    Coercing would be worse than raising: str(None) is 'None', which redacts
    cleanly and hides the fact that a summary went missing.
    """
    with pytest.raises(TypeError):
        redact_text(wrong)  # type: ignore[arg-type]


# --- realistic summaries -------------------------------------------------


def test_redact_keeps_a_whole_network_summary_intact() -> None:
    """Task 9 slices summaries on ' | ', so redaction must not disturb the grammar."""
    _assert_redacts(
        "GET https://x.test/a | status=401 Bad token | mime=application/json"
        ' | time=1.0ms | body={"password":"p"}',
        "GET https://x.test/a | status=401 Bad token | mime=application/json"
        ' | time=1.0ms | body={"password":"' + REDACTED + '"}',
    )


def test_redact_keeps_a_whole_action_summary_intact() -> None:
    _assert_redacts(
        'click | params={"selector":"#login","password":"hunter2"} | error=Timeout',
        'click | params={"selector":"#login","password":"' + REDACTED + '"} | error=Timeout',
    )


# --- the Evidence API ----------------------------------------------------


def test_redact_evidence_only_changes_the_summary() -> None:
    item = _evidence("Authorization: Bearer abc123")
    cleaned = redact_evidence(item)
    assert cleaned.summary == f"Authorization: Bearer {REDACTED}"
    assert cleaned.source == item.source
    assert cleaned.provenance == item.provenance
    assert cleaned.severity == item.severity
    assert cleaned.timestamp_ms == item.timestamp_ms


def test_redact_evidence_returns_a_new_object_and_leaves_the_original_alone() -> None:
    """Ranking in Task 9 must be reproducible, so shared evidence is never mutated."""
    item = _evidence("token=abc")
    cleaned = redact_evidence(item)
    assert cleaned is not item
    assert item.summary == "token=abc"


def test_redact_evidence_with_a_clean_summary_is_unchanged() -> None:
    item = _evidence("expected 200 but got 404")
    assert redact_evidence(item).summary == "expected 200 but got 404"


def test_redact_all_preserves_order_and_length() -> None:
    items = [_evidence("token=a"), _evidence("clean"), _evidence("Cookie: s=b")]
    cleaned = redact_all(items)
    assert [item.summary for item in cleaned] == [
        f"token={REDACTED}",
        "clean",
        f"Cookie: s={REDACTED}",
    ]


def test_redact_all_accepts_any_iterable_and_returns_a_list() -> None:
    cleaned = redact_all(item for item in [_evidence("token=a")])
    assert isinstance(cleaned, list)
    assert cleaned[0].summary == f"token={REDACTED}"


def test_redact_all_of_nothing_is_an_empty_list() -> None:
    assert redact_all([]) == []
