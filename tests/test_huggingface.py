from datetime import UTC, datetime

import httpx
import pytest

from papertrail.sources.huggingface import API_URL, HuggingFacePapers

SINCE = datetime(2026, 1, 1, tzinfo=UTC)


def entry(**overrides) -> dict:
    paper = {
        "id": "2401.00001",
        "title": "Sparse autoencoders scale to frontier models",
        "publishedAt": "2026-01-02T09:00:00.000Z",
        "upvotes": 148,
        "summary": "  We show that sparse autoencoders  ",
    }
    paper.update(overrides.pop("paper", {}))
    payload = {"paper": paper, "numComments": 12}
    payload.update(overrides)
    return payload


def fetch(payload: list[dict], since: datetime = SINCE) -> list:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API_URL)
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return HuggingFacePapers(client=client).fetch(since)


def test_maps_a_paper_onto_the_common_schema():
    (item,) = fetch([entry()])

    assert item.title == "Sparse autoencoders scale to frontier models"
    assert item.source == "hf"
    assert item.url == "https://arxiv.org/abs/2401.00001"
    assert item.published_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert item.source_id == "2401.00001"


def test_the_primary_source_is_known_without_resolution():
    """A paper feed already knows its paper; day 3 gets these for free."""
    (item,) = fetch([entry()])
    assert item.primary_source_url == "https://arxiv.org/abs/2401.00001"
    assert item.discussion_url == "https://huggingface.co/papers/2401.00001"


def test_raw_signal_is_upvotes_with_comments_breaking_ties():
    (item,) = fetch([entry()])
    assert item.raw_signal == pytest.approx(148.12)


def test_papers_older_than_the_window_are_dropped():
    old = entry(paper={"publishedAt": "2025-06-01T09:00:00.000Z"})
    assert fetch([old]) == []


def test_entries_missing_an_id_or_title_are_skipped():
    assert fetch([{"paper": {"title": "No id here", "publishedAt": "2026-01-02T09:00:00Z"}}]) == []
    assert fetch([{"paper": {"id": "2401.1", "publishedAt": "2026-01-02T09:00:00Z"}}]) == []
    assert fetch([{}]) == []


def test_an_unparseable_timestamp_is_skipped_not_fatal():
    good = entry()
    bad = entry(paper={"id": "2401.00002", "publishedAt": "sometime last week"})
    assert [item.source_id for item in fetch([bad, good])] == ["2401.00001"]


def test_the_url_shares_an_id_with_the_same_paper_from_arxiv():
    from papertrail.ids import item_id

    (item,) = fetch([entry()])
    assert item.id == item_id("https://arxiv.org/pdf/2401.00001v2")


def test_http_errors_propagate_to_the_pipeline():
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        HuggingFacePapers(client=client).fetch(SINCE)
