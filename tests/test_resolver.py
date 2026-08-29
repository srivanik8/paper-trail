from datetime import UTC, datetime

import httpx
import pytest

from papertrail.dedup import deduplicate
from papertrail.fetcher import Fetcher
from papertrail.models import Item
from papertrail.provenance import Evidence
from papertrail.resolver import Resolver
from papertrail.store import Store

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
ROBOTS = "User-agent: *\nAllow: /\n"


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def make_item(
    url: str, primary: str | None = None, signal: float = 10.0, source: str = "hn"
) -> Item:
    return Item(
        title="Sparse autoencoders scale to frontier models",
        url=url,
        source=source,
        published_at=WHEN,
        raw_signal=signal,
        primary_source_url=primary,
    )


def resolver_serving(store: Store, pages: dict[str, str], log: list[str] | None = None) -> Resolver:
    """A resolver whose fetcher serves ``pages`` keyed by URL suffix."""

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(200, text=ROBOTS)
        if log is not None:
            log.append(url)
        for suffix, body in pages.items():
            if url.endswith(suffix):
                return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404, text="")

    client = httpx.Client(transport=httpx.MockTransport(handler))
    return Resolver(Fetcher(store, client=client))


# --- step 1: the source already knew ----------------------------------------


def test_a_source_supplied_paper_is_used_without_touching_the_network(store):
    log: list[str] = []
    resolver = resolver_serving(store, {}, log)
    item = make_item(
        "https://huggingface.co/papers/2401.00001", primary="https://arxiv.org/abs/2401.00001"
    )

    result = resolver.resolve(item)
    assert result.provenance.evidence is Evidence.PAPER
    assert result.provenance.via == "source"
    assert result.fetched is False
    assert log == []


def test_a_nonsense_primary_source_from_a_feed_is_not_trusted(store):
    """A supplied URL still has to classify; a marketing page is not evidence."""
    resolver = resolver_serving(store, {})
    item = make_item("https://arxiv.org/abs/2401.00001", primary="https://example.com/waitlist")

    result = resolver.resolve(item)
    assert result.provenance.evidence is Evidence.PAPER
    assert result.provenance.via == "self"


# --- step 2: the URL is the artifact ----------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://arxiv.org/abs/2401.00001", Evidence.PAPER),
        ("https://github.com/owner/repo", Evidence.REPO),
        ("https://huggingface.co/org/model", Evidence.MODEL_WEIGHTS),
        ("https://www.anthropic.com/news/a-post", Evidence.OFFICIAL_BLOG),
    ],
)
def test_a_primary_url_resolves_without_a_fetch(store, url, expected):
    log: list[str] = []
    result = resolver_serving(store, {}, log).resolve(make_item(url))

    assert result.provenance.evidence is expected
    assert result.provenance.via == "self"
    assert log == []


# --- step 3: read the page --------------------------------------------------


def test_a_paper_link_is_found_in_the_page(store):
    page = """
    <article>
      <p>We are pleased to announce our new model.</p>
      <a href="https://arxiv.org/abs/2401.00001">Read the paper</a>
    </article>
    """
    result = resolver_serving(store, {"/post": page}).resolve(
        make_item("https://blog.example/post")
    )

    assert result.provenance.evidence is Evidence.PAPER
    assert result.provenance.url == "https://arxiv.org/abs/2401.00001"
    assert result.provenance.via == "page"
    assert result.fetched is True


def test_the_strongest_candidate_on_a_page_wins(store):
    page = """
    <article>
      <a href="https://openai.com/index/announcement">announcement</a>
      <a href="https://huggingface.co/org/model">weights</a>
      <a href="https://github.com/owner/repo">code</a>
      <a href="https://arxiv.org/abs/2401.00001">paper</a>
    </article>
    """
    result = resolver_serving(store, {"/post": page}).resolve(
        make_item("https://blog.example/post")
    )
    assert result.provenance.evidence is Evidence.PAPER


def test_a_page_with_nothing_checkable_stays_unresolved(store):
    page = """
    <article>
      <p>AGI is coming. Join the waitlist.</p>
      <a href="https://forms.example/waitlist">Sign up</a>
      <a href="https://twitter.com/someone">Follow us</a>
    </article>
    """
    result = resolver_serving(store, {"/post": page}).resolve(
        make_item("https://blog.example/post")
    )

    assert result.provenance.evidence is Evidence.NONE
    assert result.resolved is False
    assert result.fetched is True


def test_a_footer_repository_does_not_resolve_an_article(store):
    """The classic false positive: every page on the site links the company repo."""
    page = """
    <article><p>Ten predictions for AI in 2026.</p></article>
    <footer><a href="https://github.com/thecompany/website">our site source</a></footer>
    """
    result = resolver_serving(store, {"/post": page}).resolve(
        make_item("https://blog.example/post")
    )
    assert result.provenance.evidence is Evidence.NONE


def test_an_unreachable_page_stays_unresolved(store):
    result = resolver_serving(store, {}).resolve(make_item("https://blog.example/missing"))
    assert result.provenance.evidence is Evidence.NONE
    assert result.fetched is True


def test_without_a_fetcher_resolution_is_offline_only(store):
    offline = Resolver()

    assert offline.resolve(make_item("https://arxiv.org/abs/2401.1")).resolved is True
    unresolved = offline.resolve(make_item("https://blog.example/post"))
    assert unresolved.resolved is False
    assert unresolved.fetched is False


def test_a_page_is_fetched_once_across_several_items(store):
    log: list[str] = []
    resolver = resolver_serving(store, {"/post": '<a href="https://arxiv.org/abs/1">p</a>'}, log)

    resolver.resolve(make_item("https://blog.example/post"))
    resolver.resolve(make_item("https://blog.example/post?utm_source=x"))
    assert len([entry for entry in log if entry.endswith("/post")]) == 1


# --- clusters ---------------------------------------------------------------


def test_a_cluster_resolves_through_a_quieter_member(store):
    """The HN link has the score; the arXiv entry has the paper."""
    loud = make_item("https://blog.example/post", signal=300.0, source="hn")
    quiet = make_item(
        "https://arxiv.org/abs/2401.00001",
        primary="https://arxiv.org/abs/2401.00001",
        signal=0.0,
        source="arxiv",
    )
    (cluster,) = deduplicate([loud, quiet])

    result = resolver_serving(store, {}).resolve_cluster(cluster)
    assert result.item is loud
    assert result.provenance.evidence is Evidence.PAPER
    assert result.provenance.url == "https://arxiv.org/abs/2401.00001"


def test_a_cluster_nobody_can_vouch_for_stays_unresolved(store):
    items = [
        make_item("https://blog.example/post", signal=300.0),
        make_item("https://other.example/post", signal=10.0, source="rss"),
    ]
    (cluster,) = deduplicate(items)

    result = resolver_serving(store, {}).resolve_cluster(cluster)
    assert result.resolved is False
    assert result.item.raw_signal == 300.0


def test_resolution_reports_the_canonical_item_not_the_member_that_resolved(store):
    loud = make_item("https://blog.example/post", signal=300.0, source="hn")
    quiet = make_item("https://arxiv.org/abs/2401.00001", signal=0.0, source="arxiv")
    (cluster,) = deduplicate([loud, quiet])

    result = resolver_serving(store, {}).resolve_cluster(cluster)
    assert result.item.source == "hn"
    assert result.item.raw_signal == 300.0
