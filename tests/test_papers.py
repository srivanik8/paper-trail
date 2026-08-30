from datetime import UTC, datetime, timedelta

import httpx
import pytest

from papertrail.papers import API, ArxivPapers, arxiv_id
from papertrail.store import Store
from papertrail.substance import Flag, assess_paper

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


def feed(
    entry_id: str = "http://arxiv.org/abs/2401.00001v3",
    authors: int = 6,
    comment: str | None = None,
    categories: tuple[str, ...] = ("cs.LG", "cs.AI"),
) -> str:
    author_xml = "".join(f"<author><name>Author {i}</name></author>" for i in range(authors))
    category_xml = "".join(f'<category term="{term}"/>' for term in categories)
    comment_xml = (
        f'<arxiv:comment xmlns:arxiv="http://arxiv.org/schemas/atom">{comment}</arxiv:comment>'
        if comment
        else ""
    )
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <id>{entry_id}</id>
    <title>Sparse autoencoders scale to frontier models</title>
    <published>2024-01-01T09:00:00Z</published>
    <updated>2024-06-01T09:00:00Z</updated>
    {author_xml}{category_xml}{comment_xml}
  </entry>
</feed>"""


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def papers(store: Store, body: str = None, status: int = 200, log: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(API)
        if log is not None:
            log.append(str(request.url))
        return httpx.Response(status, text=body if body is not None else feed())

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return ArxivPapers(store, client=client, delay=0)


# --- reference parsing ------------------------------------------------------


@pytest.mark.parametrize(
    "reference",
    [
        "2401.00001",
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/abs/2401.00001v3",
        "https://arxiv.org/pdf/2401.00001v2.pdf",
        "http://www.arxiv.org/html/2401.00001v1",
    ],
)
def test_an_arxiv_id_is_recovered_from_any_form(reference):
    assert arxiv_id(reference) == "2401.00001"


def test_old_style_identifiers_are_recognized():
    assert arxiv_id("https://arxiv.org/abs/cs/0501001") == "cs/0501001"


@pytest.mark.parametrize("reference", ["https://github.com/owner/repo", "not a paper", ""])
def test_non_arxiv_references_yield_nothing(reference):
    assert arxiv_id(reference) is None


def test_a_non_arxiv_reference_is_an_error_not_a_crash(store):
    facts = papers(store).facts("https://github.com/owner/repo", now=NOW)
    assert facts.retrieved is False
    assert "not an arXiv reference" in facts.error


# --- parsing ----------------------------------------------------------------


def test_the_version_is_read_from_the_entry_id(store):
    assert papers(store).facts("2401.00001", now=NOW).version == 3


def test_an_entry_without_a_version_suffix_is_version_one(store):
    body = feed(entry_id="http://arxiv.org/abs/2401.00001")
    assert papers(store, body).facts("2401.00001", now=NOW).version == 1


def test_authors_and_categories_are_counted(store):
    facts = papers(store).facts("2401.00001", now=NOW)
    assert facts.authors == 6
    assert facts.categories == ("cs.LG", "cs.AI")


def test_timestamps_are_parsed_to_utc(store):
    facts = papers(store).facts("2401.00001", now=NOW)
    assert facts.published_at == datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
    assert facts.updated_at == datetime(2024, 6, 1, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    "comment",
    [
        "This paper has been withdrawn by the authors",
        "Withdrawn due to an error in Table 3",
        "Retracted after review",
    ],
)
def test_a_withdrawal_is_detected_in_the_comment(store, comment):
    """arXiv has no structured retraction flag; it lives in free text."""
    facts = papers(store, feed(comment=comment)).facts("2401.00001", now=NOW)
    assert facts.withdrawn is True
    assert Flag.WITHDRAWN in assess_paper(facts).flags


def test_an_ordinary_comment_is_not_a_withdrawal(store):
    body = feed(comment="Accepted at NeurIPS 2024. Code at https://github.com/a/b")
    assert papers(store, body).facts("2401.00001", now=NOW).withdrawn is False


def test_a_missing_entry_is_reported(store):
    body = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    facts = papers(store, body).facts("2401.99999", now=NOW)
    assert facts.retrieved is False
    assert "no such paper" in facts.error


def test_unparseable_xml_is_reported_not_raised(store):
    facts = papers(store, "<not xml").facts("2401.00001", now=NOW)
    assert facts.retrieved is False
    assert "unparseable" in facts.error


def test_an_http_error_is_recorded_not_raised(store):
    facts = papers(store, status=503).facts("2401.00001", now=NOW)
    assert facts.retrieved is False
    assert "HTTPStatusError" in facts.error


# --- caching and politeness -------------------------------------------------


def test_the_second_lookup_is_served_from_cache(store):
    log: list[str] = []
    client = papers(store, log=log)

    client.facts("2401.00001", now=NOW)
    client.facts("2401.00001", now=NOW)
    assert len(log) == 1


def test_every_url_form_shares_one_cache_entry(store):
    log: list[str] = []
    client = papers(store, log=log)

    client.facts("https://arxiv.org/abs/2401.00001v3", now=NOW)
    client.facts("https://arxiv.org/pdf/2401.00001v1.pdf", now=NOW)
    assert len(log) == 1


def test_a_stale_entry_is_refetched(store):
    log: list[str] = []
    client = ArxivPapers(
        store,
        client=papers(store, log=log)._client,
        delay=0,
        max_age=timedelta(days=1),
    )
    client.facts("2401.00001", now=NOW)
    client.facts("2401.00001", now=NOW + timedelta(days=30))
    assert len(log) == 2


def test_the_first_request_does_not_wait(store):
    import time

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=feed())))
    source = ArxivPapers(store, client=client, delay=30.0)

    started = time.monotonic()
    source.facts("2401.00001", now=NOW)
    assert time.monotonic() - started < 1.0
    client.close()


def test_a_second_request_waits_out_the_gap(store):
    import time

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=feed())))
    source = ArxivPapers(store, client=client, delay=0.2)

    source.facts("2401.00001", now=NOW)
    started = time.monotonic()
    source.facts("2401.00002", now=NOW)
    assert time.monotonic() - started >= 0.2
    client.close()


def test_a_revised_multi_author_paper_raises_no_flags(store):
    facts = papers(store).facts("2401.00001", now=NOW)
    assert assess_paper(facts).flags == ()


def test_a_lone_unrevised_preprint_raises_both_flags(store):
    body = feed(entry_id="http://arxiv.org/abs/2401.00001v1", authors=1)
    flags = assess_paper(papers(store, body).facts("2401.00001", now=NOW)).flags
    assert Flag.UNREVISED in flags
    assert Flag.SINGLE_AUTHOR in flags
