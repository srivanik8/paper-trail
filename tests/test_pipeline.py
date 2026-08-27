from datetime import UTC, datetime, timedelta

import pytest

from papertrail.models import Item
from papertrail.pipeline import build_sources, run

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


class FakeSource:
    def __init__(self, name: str, items: list[Item] | None = None, error: Exception | None = None):
        self.name = name
        self._items = items or []
        self._error = error
        self.asked_since: datetime | None = None

    def fetch(self, since: datetime) -> list[Item]:
        self.asked_since = since
        if self._error is not None:
            raise self._error
        return self._items


def make_item(title: str, signal: float, source: str = "fake") -> Item:
    return Item(
        title=title,
        url=f"https://example.com/{title.replace(' ', '-')}",
        source=source,
        published_at=NOW,
        raw_signal=signal,
    )


def test_window_is_subtracted_from_now():
    source = FakeSource("fake")
    run(timedelta(hours=24), [source], now=NOW)
    assert source.asked_since == datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_items_are_sorted_by_signal_descending():
    source = FakeSource("fake", [make_item("low", 1.0), make_item("high", 99.0)])
    result = run(timedelta(hours=24), [source], now=NOW)
    assert [item.title for item in result.items] == ["high", "low"]


def test_equal_signals_break_ties_on_title():
    source = FakeSource("fake", [make_item("beta", 5.0), make_item("alpha", 5.0)])
    result = run(timedelta(hours=24), [source], now=NOW)
    assert [item.title for item in result.items] == ["alpha", "beta"]


def test_a_failing_source_is_recorded_and_the_run_continues():
    good = FakeSource("good", [make_item("kept", 10.0, source="good")])
    bad = FakeSource("bad", error=RuntimeError("feed is down"))
    result = run(timedelta(hours=24), [good, bad], now=NOW)

    assert [item.title for item in result.items] == ["kept"]
    assert result.errors == {"bad": "RuntimeError: feed is down"}


def test_per_source_counts():
    source = FakeSource(
        "fake",
        [make_item("a", 1.0, source="hn"), make_item("b", 2.0, source="hn")],
    )
    assert run(timedelta(hours=24), [source], now=NOW).per_source == {"hn": 2}


def test_build_sources_defaults_to_the_whole_registry():
    assert [source.name for source in build_sources()] == ["hn"]


def test_build_sources_rejects_an_unknown_name():
    with pytest.raises(KeyError, match="nope"):
        build_sources(["nope"])
