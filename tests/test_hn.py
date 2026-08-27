from datetime import UTC, datetime

import httpx
import pytest

from papertrail.sources.hn import API_URL, HackerNews


def hit(**overrides) -> dict:
    payload = {
        "objectID": "123",
        "title": "Show HN: An LLM that reads your logs",
        "url": "https://example.com/llm-logs",
        "points": 87,
        "num_comments": 34,
        "created_at_i": 1767268800,  # 2026-01-01T12:00:00Z
        "author": "someone",
    }
    payload.update(overrides)
    return payload


def client_returning(*pages: dict) -> httpx.Client:
    """A client that serves the given payloads in order, one per request."""
    remaining = list(pages)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API_URL)
        return httpx.Response(200, json=remaining.pop(0))

    return httpx.Client(transport=httpx.MockTransport(handler))


def fetch(*pages: dict) -> list:
    with client_returning(*pages) as client:
        return HackerNews(client=client).fetch(datetime(2026, 1, 1, tzinfo=UTC))


def test_maps_a_hit_onto_the_common_schema():
    (item,) = fetch({"hits": [hit()], "nbPages": 1})

    assert item.title == "Show HN: An LLM that reads your logs"
    assert item.url == "https://example.com/llm-logs"
    assert item.source == "hn"
    assert item.published_at == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert item.source_id == "123"
    assert item.discussion_url == "https://news.ycombinator.com/item?id=123"
    assert item.primary_source_url is None
    assert item.extra["points"] == 87


def test_raw_signal_is_points_with_comments_breaking_ties():
    (item,) = fetch({"hits": [hit(points=87, num_comments=34)], "nbPages": 1})
    assert item.raw_signal == pytest.approx(87.34)


def test_comment_tiebreak_never_outweighs_a_point():
    (busy,) = fetch({"hits": [hit(points=10, num_comments=5000)], "nbPages": 1})
    (quiet,) = fetch({"hits": [hit(points=11, num_comments=0)], "nbPages": 1})
    assert quiet.raw_signal > busy.raw_signal


def test_text_posts_fall_back_to_the_hn_thread_url():
    (item,) = fetch({"hits": [hit(url=None, title="Ask HN: best local LLM setup?")], "nbPages": 1})
    assert item.url == "https://news.ycombinator.com/item?id=123"
    assert item.url == item.discussion_url


def test_off_topic_stories_are_dropped():
    payload = {"hits": [hit(title="Postgres 18 released", url="https://pg.org")], "nbPages": 1}
    assert fetch(payload) == []


def test_malformed_hits_are_skipped_not_fatal():
    payload = {
        "hits": [
            hit(),
            hit(objectID="456", title=""),
            hit(objectID="789", created_at_i=None),
            {"objectID": "999"},
        ],
        "nbPages": 1,
    }
    assert [item.source_id for item in fetch(payload)] == ["123"]


def test_pagination_walks_every_page():
    items = fetch(
        {"hits": [hit(objectID="1")], "nbPages": 2},
        {"hits": [hit(objectID="2")], "nbPages": 2},
    )
    assert [item.source_id for item in items] == ["1", "2"]


def test_http_errors_propagate_to_the_pipeline():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with (
        httpx.Client(transport=httpx.MockTransport(handler)) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        HackerNews(client=client).fetch(datetime(2026, 1, 1, tzinfo=UTC))
