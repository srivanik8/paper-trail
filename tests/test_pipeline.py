from datetime import UTC, datetime, timedelta

import pytest

from papertrail.models import Item
from papertrail.pipeline import build_sources, known_clusters, run
from papertrail.store import STATUS_DUPLICATE, STATUS_NEW, Store

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
WINDOW = timedelta(hours=24)


class FakeSource:
    """A source that returns a fixed list, or raises."""

    def __init__(self, name: str, items: list[Item] | None = None, error: Exception | None = None):
        self.name = name
        self._items = items or []
        self._error = error
        self.asked_since: datetime | None = None
        self.calls = 0

    def fetch(self, since: datetime) -> list[Item]:
        self.calls += 1
        self.asked_since = since
        if self._error is not None:
            raise self._error
        return self._items


def make_item(title: str, url: str | None = None, signal: float = 10.0, source: str = "hn") -> Item:
    return Item(
        title=title,
        url=url or f"https://example.com/{abs(hash(title))}",
        source=source,
        published_at=NOW,
        raw_signal=signal,
    )


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


# --- collection -------------------------------------------------------------


def test_window_is_subtracted_from_now():
    source = FakeSource("fake")
    run(WINDOW, [source], now=NOW)
    assert source.asked_since == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_a_failing_source_is_recorded_and_the_run_continues():
    good = FakeSource("good", [make_item("A story about LLM inference", signal=10.0)])
    bad = FakeSource("bad", error=RuntimeError("feed is down"))

    result = run(WINDOW, [good, bad], now=NOW)
    assert [item.title for item in result.items] == ["A story about LLM inference"]
    assert result.errors == {"bad": "RuntimeError: feed is down"}


def test_build_sources_defaults_to_the_whole_registry():
    assert [source.name for source in build_sources()] == ["hn"]


def test_build_sources_rejects_an_unknown_name():
    with pytest.raises(KeyError, match="nope"):
        build_sources(["nope"])


# --- deduplication within a run --------------------------------------------


def test_one_story_from_three_sources_becomes_one_cluster():
    source = FakeSource(
        "multi",
        [
            make_item("Mistral releases Large 3", "https://a.example/1", 300.0, "hn"),
            make_item("Mistral has released Large 3 model", "https://b.example/2", 40.0, "reddit"),
            make_item("Mistral releases Large 3 today", "https://c.example/3", 90.0, "rss"),
        ],
    )
    result = run(WINDOW, [source], now=NOW)

    assert len(result.clusters) == 1
    assert result.fetched == 3
    assert result.collapsed == 2
    assert result.clusters[0].also_seen == ["rss", "reddit"]


def test_counts_report_fetched_against_deduplicated():
    source = FakeSource(
        "multi",
        [
            make_item("Mistral releases Large 3", "https://a.example/1", 300.0),
            make_item("Mistral releases Large 3 today", "https://b.example/2", 90.0),
            make_item("Postgres 18 adds asynchronous IO", "https://c.example/3", 50.0),
        ],
    )
    result = run(WINDOW, [source], now=NOW)
    assert (result.fetched, len(result.clusters), result.collapsed) == (3, 2, 1)


# --- deduplication across runs ---------------------------------------------


def test_running_twice_reports_nothing_new_the_second_time(store):
    """The day 2 acceptance check."""
    source = FakeSource(
        "hn",
        [
            make_item("Mistral releases Large 3", "https://a.example/1", 300.0),
            make_item("Postgres 18 adds asynchronous IO", "https://b.example/2", 50.0),
        ],
    )

    first = run(WINDOW, [source], store, now=NOW)
    assert len(first.fresh) == 2
    assert len(first.continuing) == 0

    second = run(WINDOW, [source], store, now=NOW + timedelta(hours=1))
    assert len(second.fresh) == 0
    assert len(second.continuing) == 2


def test_a_reworded_headline_the_next_day_is_not_new(store):
    monday = FakeSource("hn", [make_item("Mistral releases Large 3", "https://a.example/1", 300.0)])
    tuesday = FakeSource(
        "rss",
        [
            make_item(
                "Mistral has released Large 3, its flagship model", "https://b.example/2", 20.0
            )
        ],
    )

    run(WINDOW, [monday], store, now=NOW)
    result = run(WINDOW, [tuesday], store, now=NOW + timedelta(days=1))

    assert len(result.fresh) == 0
    assert result.clusters[0].is_continuation is True


def test_a_genuinely_new_story_the_next_day_is_new(store):
    monday = FakeSource("hn", [make_item("Mistral releases Large 3", "https://a.example/1", 300.0)])
    tuesday = FakeSource(
        "hn", [make_item("Postgres 18 adds asynchronous IO", "https://b.example/2")]
    )

    run(WINDOW, [monday], store, now=NOW)
    result = run(WINDOW, [tuesday], store, now=NOW + timedelta(days=1))
    assert len(result.fresh) == 1


def test_a_story_older_than_the_dedup_window_counts_as_new_again(store):
    source = FakeSource("hn", [make_item("Mistral releases Large 3", "https://a.example/1", 300.0)])

    run(WINDOW, [source], store, now=NOW)
    later = run(WINDOW, [source], store, now=NOW + timedelta(days=30))
    assert len(later.fresh) == 1


def test_without_a_store_every_run_looks_new():
    source = FakeSource("hn", [make_item("Mistral releases Large 3", "https://a.example/1", 300.0)])
    assert len(run(WINDOW, [source], now=NOW).fresh) == 1
    assert len(run(WINDOW, [source], now=NOW).fresh) == 1


# --- persistence ------------------------------------------------------------


def test_canonical_and_duplicate_members_are_both_stored(store):
    lead = make_item("Mistral releases Large 3", "https://a.example/1", 300.0)
    echo = make_item("Mistral releases Large 3 today", "https://b.example/2", 20.0)

    run(WINDOW, [FakeSource("hn", [lead, echo])], store, now=NOW)

    assert store.get(lead.id)["status"] == STATUS_NEW
    assert store.get(echo.id)["status"] == STATUS_DUPLICATE
    assert store.get(echo.id)["reason"] == f"duplicate of {lead.id}"
    assert store.get(echo.id)["cluster_id"] == lead.id


def test_dry_run_deduplicates_but_records_nothing(store):
    item = make_item("Mistral releases Large 3", "https://a.example/1", 300.0)
    source = FakeSource("hn", [item])

    result = run(WINDOW, [source], store, now=NOW, persist=False)
    assert len(result.fresh) == 1
    assert store.has(item.id) is False


def test_dry_run_still_sees_what_earlier_runs_recorded(store):
    source = FakeSource("hn", [make_item("Mistral releases Large 3", "https://a.example/1", 300.0)])

    run(WINDOW, [source], store, now=NOW)
    result = run(WINDOW, [source], store, now=NOW + timedelta(hours=1), persist=False)
    assert len(result.fresh) == 0


def test_known_clusters_offers_one_title_per_cluster(store):
    lead = make_item("Mistral releases Large 3", "https://a.example/1", 300.0)
    echo = make_item("Mistral releases Large 3 today", "https://b.example/2", 20.0)
    run(WINDOW, [FakeSource("hn", [lead, echo])], store, now=NOW)

    known = known_clusters(store, NOW - timedelta(days=7))
    assert len(known) == 1
    assert known[0].cluster_id == lead.id
