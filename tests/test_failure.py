import httpx
import pytest

from papertrail.failure import MAX_LENGTH, describe


def test_a_failure_reads_as_type_then_message():
    assert describe(ValueError("something went wrong")) == "ValueError: something went wrong"


def test_a_multi_line_message_is_collapsed_to_one_line():
    """httpx appends a documentation URL on its own line."""
    exc = httpx.HTTPStatusError(
        "Client error '403 Forbidden' for url 'https://example.com'\n"
        "For more information check: https://developer.mozilla.org/en-US/docs/Web/HTTP/Status/403",
        request=httpx.Request("GET", "https://example.com"),
        response=httpx.Response(403),
    )
    described = describe(exc)

    assert "\n" not in described
    assert described.startswith("HTTPStatusError: Client error")


def test_runs_of_whitespace_are_squeezed():
    assert describe(ValueError("too    many\t\tspaces")) == "ValueError: too many spaces"


def test_a_long_message_is_truncated_with_an_ellipsis():
    described = describe(ValueError("x" * 500))
    assert len(described) == MAX_LENGTH
    assert described.endswith("…")


def test_a_short_message_is_untouched():
    assert describe(ValueError("brief")) == "ValueError: brief"


def test_an_exception_with_no_message_reports_its_type():
    assert describe(RuntimeError()) == "RuntimeError"


def test_the_limit_is_adjustable():
    assert len(describe(ValueError("x" * 100), limit=20)) == 20


@pytest.mark.parametrize(
    "exc",
    [httpx.ConnectTimeout("timed out"), httpx.ProxyError("403 Forbidden"), KeyError("missing")],
)
def test_the_type_name_leads_because_that_is_what_you_scan_for(exc):
    assert describe(exc).startswith(type(exc).__name__)
