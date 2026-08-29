from datetime import UTC, datetime, timedelta

import httpx
import pytest

from papertrail.fetcher import (
    MAX_BODY_CHARS,
    STATUS_BLOCKED_BY_ROBOTS,
    STATUS_ERROR,
    STATUS_UNREADABLE,
    USER_AGENT,
    Fetcher,
)
from papertrail.store import Store

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ROBOTS_ALLOW_ALL = "User-agent: *\nAllow: /\n"


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def serving(routes: dict[str, httpx.Response], log: list[str] | None = None) -> httpx.Client:
    """A client serving ``routes`` by path, 404 for anything else."""

    def handler(request: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append(str(request.url))
        for path, response in routes.items():
            if str(request.url).endswith(path):
                return response
        return httpx.Response(404, text="not found")

    return httpx.Client(transport=httpx.MockTransport(handler))


def html(body: str = "<html><body>hello</body></html>") -> httpx.Response:
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def test_a_page_is_fetched_and_returned(store):
    client = serving({"/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL), "/post": html()})
    page = Fetcher(store, client=client).get("https://example.com/post")

    assert page.ok is True
    assert page.body == "<html><body>hello</body></html>"
    assert page.content_type == "text/html"
    assert page.from_cache is False


def test_the_second_request_for_a_url_is_served_from_cache(store):
    log: list[str] = []
    client = serving(
        {"/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL), "/post": html()}, log
    )
    fetcher = Fetcher(store, client=client)

    fetcher.get("https://example.com/post")
    page = fetcher.get("https://example.com/post")

    assert page.from_cache is True
    assert fetcher.fetches == 1
    assert log.count("https://example.com/post") == 1


def test_urls_differing_only_by_tracking_share_one_fetch(store):
    client = serving({"/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL), "/post": html()})
    fetcher = Fetcher(store, client=client)

    fetcher.get("https://example.com/post")
    fetcher.get("https://www.example.com/post/?utm_source=newsletter")
    assert fetcher.fetches == 1


def test_a_stale_cache_entry_is_refetched(store):
    client = serving({"/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL), "/post": html()})
    fetcher = Fetcher(store, client=client, max_age=timedelta(days=1))

    fetcher.get("https://example.com/post", now=WHEN)
    fetcher.get("https://example.com/post", now=WHEN + timedelta(days=30))
    assert fetcher.fetches == 2


def test_failures_are_cached_and_not_retried(store):
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS_ALLOW_ALL)
        raise httpx.ConnectTimeout("timed out")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        fetcher = Fetcher(store, client=client)
        first = fetcher.get("https://example.com/post")
        second = fetcher.get("https://example.com/post")

    assert first.status == STATUS_ERROR
    assert "ConnectTimeout" in first.error
    assert second.from_cache is True
    assert fetcher.fetches == 1


def test_a_404_is_cached_too(store):
    client = serving({"/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL)})
    fetcher = Fetcher(store, client=client)

    assert fetcher.get("https://example.com/gone").status == 404
    assert fetcher.get("https://example.com/gone").from_cache is True
    assert fetcher.fetches == 1


def test_robots_disallow_is_obeyed(store):
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"),
            "/private/post": html(),
        }
    )
    page = Fetcher(store, client=client).get("https://example.com/private/post")

    assert page.status == STATUS_BLOCKED_BY_ROBOTS
    assert page.body == ""


def test_robots_allows_what_it_does_not_forbid(store):
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /private/\n"),
            "/public/post": html(),
        }
    )
    assert Fetcher(store, client=client).get("https://example.com/public/post").ok is True


def test_a_missing_robots_file_is_treated_as_permissive(store):
    """The standard says absent means allowed, and every crawler agrees."""
    client = serving({"/post": html()})
    assert Fetcher(store, client=client).get("https://example.com/post").ok is True


def test_robots_is_fetched_once_per_host(store):
    log: list[str] = []
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL),
            "/a": html(),
            "/b": html(),
        },
        log,
    )
    fetcher = Fetcher(store, client=client)
    fetcher.get("https://example.com/a")
    fetcher.get("https://example.com/b")

    assert len([entry for entry in log if entry.endswith("/robots.txt")]) == 1


def test_non_markup_responses_are_rejected_without_reading_them(store):
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL),
            "/paper.pdf": httpx.Response(
                200, text="%PDF-1.7 ...", headers={"content-type": "application/pdf"}
            ),
        }
    )
    page = Fetcher(store, client=client).get("https://example.com/paper.pdf")

    assert page.status == STATUS_UNREADABLE
    assert page.body == ""
    assert "application/pdf" in page.error


def test_an_oversized_body_is_truncated(store):
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text=ROBOTS_ALLOW_ALL),
            "/huge": html("x" * (MAX_BODY_CHARS + 5000)),
        }
    )
    page = Fetcher(store, client=client).get("https://example.com/huge")
    assert len(page.body) == MAX_BODY_CHARS


def test_the_user_agent_identifies_the_project(store):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        return (
            httpx.Response(200, text=ROBOTS_ALLOW_ALL)
            if str(request.url).endswith("/robots.txt")
            else html()
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        Fetcher(store, client=client).get("https://example.com/post")

    assert all(agent == USER_AGENT for agent in seen)
    assert "paper-trail" in USER_AGENT


def test_non_http_urls_are_refused_without_a_request(store):
    fetcher = Fetcher(store, client=serving({}))
    page = fetcher.get("mailto:someone@example.com")

    assert page.status == STATUS_ERROR
    assert fetcher.fetches == 0


def test_obey_robots_can_be_disabled_for_tests(store):
    client = serving(
        {
            "/robots.txt": httpx.Response(200, text="User-agent: *\nDisallow: /\n"),
            "/post": html(),
        }
    )
    assert Fetcher(store, client=client, obey_robots=False).get("https://example.com/post").ok
