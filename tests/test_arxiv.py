import time
from datetime import UTC, datetime

import httpx
import pytest

from papertrail.sources.arxiv import API_URL, ArxivListings

SINCE = datetime(2026, 1, 1, tzinfo=UTC)

FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>http://arxiv.org/abs/2401.00001v2</id>
    <title>Sparse autoencoders scale
      to frontier models</title>
    <summary>  We show   that sparse
      autoencoders work.  </summary>
    <published>2026-01-02T09:00:00Z</published>
    <author><name>Ada Lovelace</name></author>
    <author><name>Alan Turing</name></author>
    <category term="cs.LG"/>
    <category term="cs.AI"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2312.99999v1</id>
    <title>An older paper</title>
    <published>2025-06-01T09:00:00Z</published>
    <author><name>Someone Else</name></author>
    <category term="cs.CL"/>
  </entry>
</feed>
"""


def fetch(body: str = FEED, since: datetime = SINCE) -> list:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API_URL)
        return httpx.Response(200, text=body)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        return ArxivListings(client=client, delay=0).fetch(since)


def test_maps_an_entry_onto_the_common_schema():
    (item,) = fetch()

    assert item.source == "arxiv"
    assert item.url == "https://arxiv.org/abs/2401.00001"
    assert item.published_at == datetime(2026, 1, 2, 9, 0, tzinfo=UTC)
    assert item.source_id == "2401.00001"


def test_the_version_suffix_is_stripped_from_the_id():
    """v2 today and v3 tomorrow are the same paper, so they must share an id."""
    (item,) = fetch()
    assert item.source_id == "2401.00001"
    assert item.url.endswith("/2401.00001")


def test_wrapped_titles_and_summaries_are_collapsed_to_one_line():
    (item,) = fetch()
    assert item.title == "Sparse autoencoders scale to frontier models"
    assert item.extra["summary"] == "We show that sparse autoencoders work."


def test_authors_and_categories_are_carried_through():
    (item,) = fetch()
    assert item.extra["authors"] == ["Ada Lovelace", "Alan Turing"]
    assert item.extra["categories"] == ["cs.LG", "cs.AI"]


def test_arxiv_carries_no_popularity_signal():
    (item,) = fetch()
    assert item.raw_signal == 0.0


def test_the_primary_source_is_the_paper_itself():
    (item,) = fetch()
    assert item.primary_source_url == "https://arxiv.org/abs/2401.00001"


def test_entries_older_than_the_window_are_dropped():
    assert [item.source_id for item in fetch()] == ["2401.00001"]


def test_entries_missing_required_fields_are_skipped():
    body = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry><id>http://arxiv.org/abs/2401.1</id></entry>
      <entry><title>No id</title><published>2026-01-02T09:00:00Z</published></entry>
    </feed>"""
    assert fetch(body) == []


def test_an_empty_feed_is_not_an_error():
    body = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    assert fetch(body) == []


def test_the_first_request_does_not_wait():
    """The courtesy gap is between requests, not a preamble to the first."""
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FEED)))
    source = ArxivListings(client=client, delay=30.0)

    started = time.monotonic()
    source.fetch(SINCE)
    assert time.monotonic() - started < 1.0
    client.close()


def test_a_second_request_waits_out_the_gap():
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=FEED)))
    source = ArxivListings(client=client, delay=0.2)

    source.fetch(SINCE)
    started = time.monotonic()
    source.fetch(SINCE)
    assert time.monotonic() - started >= 0.2
    client.close()


def test_http_errors_propagate_to_the_pipeline():
    with (
        httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(503))) as client,
        pytest.raises(httpx.HTTPStatusError),
    ):
        ArxivListings(client=client, delay=0).fetch(SINCE)


def test_a_paper_from_arxiv_and_the_same_paper_from_hugging_face_share_an_id():
    from papertrail.ids import item_id

    (item,) = fetch()
    assert item.id == item_id("https://arxiv.org/abs/2401.00001")
