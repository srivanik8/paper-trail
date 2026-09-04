import json
from datetime import UTC, datetime, timedelta

import pytest

from papertrail.cli import main
from papertrail.models import Item
from papertrail.stats import collect, format_stats, window_from_days
from papertrail.store import (
    STATUS_DUPLICATE,
    STATUS_NEW,
    STATUS_REJECTED,
    Store,
)

NOW = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def add(store, url: str, *, status=STATUS_NEW, reason=None, when=NOW, source="hn") -> Item:
    item = Item(
        title=f"A story about {url}",
        url=url,
        source=source,
        published_at=NOW,
        raw_signal=10.0,
    )
    store.upsert(item, status=status, reason=reason, now=when)
    return item


def test_an_empty_store_reports_nothing_gracefully(store):
    stats = collect(store)
    assert stats.total == 0
    assert stats.kept_ratio == 0.0
    assert stats.median_score is None
    assert "0 items" in format_stats(stats)


def test_items_are_counted_by_status(store):
    add(store, "https://a.example/1")
    add(store, "https://a.example/2", status=STATUS_REJECTED, reason="no primary source")
    add(store, "https://a.example/3", status=STATUS_DUPLICATE, reason="duplicate of x")

    stats = collect(store)
    assert stats.total == 3
    assert stats.by_status == {"new": 1, "rejected": 1, "duplicate": 1}


def test_the_kept_ratio_excludes_rejects_and_duplicates(store):
    add(store, "https://a.example/1")
    for i in range(3):
        add(store, f"https://a.example/r{i}", status=STATUS_REJECTED, reason="no primary source")

    assert collect(store).kept_ratio == pytest.approx(0.25)


def test_drop_reasons_are_tallied(store):
    """The question you actually ask after a month: what am I throwing away?"""
    for i in range(4):
        add(store, f"https://a.example/{i}", status=STATUS_REJECTED, reason="no primary source")
    add(store, "https://a.example/9", status=STATUS_DUPLICATE, reason="duplicate of abc")

    assert collect(store).by_reason["no primary source"] == 4


def test_evidence_types_are_tallied(store):
    first = add(store, "https://a.example/1")
    second = add(store, "https://a.example/2")
    store.set_provenance(first.id, "paper", "https://arxiv.org/abs/1", "self")
    store.set_provenance(second.id, "repo", "https://github.com/a/b", "page")

    assert collect(store).by_evidence == {"paper": 1, "repo": 1}


def test_sources_are_tallied(store):
    add(store, "https://a.example/1", source="hn")
    add(store, "https://a.example/2", source="arxiv")
    add(store, "https://a.example/3", source="hn")

    assert collect(store).by_source == {"hn": 2, "arxiv": 1}


def test_substance_flags_are_counted_across_items(store):
    first = add(store, "https://a.example/1")
    second = add(store, "https://a.example/2")
    store.set_substance(first.id, ["readme_only", "waitlist"], 3.0)
    store.set_substance(second.id, ["readme_only"], 1.0)

    assert collect(store).substance_flags == {"readme_only": 2, "waitlist": 1}


def test_hype_flags_are_counted_across_items(store):
    item = add(store, "https://a.example/1")
    store.set_score(item.id, 4, "A vendor claim.", ["vendor_benchmark", "no_baseline"])

    assert collect(store).hype_flags == {"vendor_benchmark": 1, "no_baseline": 1}


def test_malformed_flag_json_is_skipped(store):
    item = add(store, "https://a.example/1")
    store.connection.execute(
        "UPDATE items SET substance_flags = ? WHERE id = ?", ("{not json", item.id)
    )
    assert collect(store).substance_flags == {}


def test_scores_are_distributed_not_averaged(store):
    for i, score in enumerate([9, 7, 7, 3]):
        item = add(store, f"https://a.example/{i}")
        store.set_score(item.id, score, "A line.", [])

    stats = collect(store)
    assert stats.scores == {9: 1, 7: 2, 3: 1}
    assert stats.median_score == 7


def test_sends_are_counted(store):
    item = add(store, "https://a.example/1")
    store.mark_sent([item.id], "2026-06-01", now=NOW)
    assert collect(store).sent == 1


def test_a_window_limits_what_is_counted(store):
    add(store, "https://a.example/old", when=NOW - timedelta(days=60))
    add(store, "https://a.example/new", when=NOW)

    assert collect(store, since=NOW - timedelta(days=7)).total == 1
    assert collect(store).total == 2


def test_window_from_days_converts_to_a_cutoff():
    assert window_from_days(7, now=NOW) == NOW - timedelta(days=7)
    assert window_from_days(0) is None
    assert window_from_days(None) is None


# --- rendering --------------------------------------------------------------


def test_the_summary_names_every_section(store):
    item = add(store, "https://a.example/1")
    add(store, "https://a.example/2", status=STATUS_REJECTED, reason="no primary source")
    store.set_provenance(item.id, "paper", "https://arxiv.org/abs/1", "self")
    store.set_substance(item.id, ["single_contributor"], 2.0)
    store.set_score(item.id, 8, "A result.", ["vendor_benchmark"])

    text = format_stats(collect(store))
    for heading in ("status", "evidence", "dropped for", "source", "substance flags", "hype flags"):
        assert f"{heading}:" in text


def test_the_score_histogram_shows_the_shape(store):
    """A rubric that scores everything a 7 is not a rubric."""
    for i in range(5):
        item = add(store, f"https://a.example/{i}")
        store.set_score(item.id, 7, "A line.", [])

    text = format_stats(collect(store))
    assert "score distribution:" in text
    assert "#" in text
    assert "median score 7" in text


def test_the_window_is_named_in_the_summary(store):
    add(store, "https://a.example/1")
    assert "since 2026-05-25" in format_stats(collect(store, since=NOW - timedelta(days=7)))
    assert "(all time)" in format_stats(collect(store))


# --- cli --------------------------------------------------------------------


def test_the_stats_command_prints_a_summary(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    with Store(db) as store:
        item = add(store, "https://a.example/1")
        store.set_score(item.id, 8, "A result.", [])
        add(store, "https://a.example/2", status=STATUS_REJECTED, reason="no primary source")

    assert main(["stats", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "2 items" in out
    assert "no primary source" in out


def test_the_stats_command_accepts_a_window(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    with Store(db) as store:
        add(store, "https://a.example/1", when=datetime.now(UTC))

    assert main(["stats", "--db", db, "--days", "7"]) == 0
    assert "since" in capsys.readouterr().out


def test_stats_on_a_fresh_database_is_not_an_error(tmp_path, capsys):
    assert main(["stats", "--db", str(tmp_path / "new.db")]) == 0
    assert "0 items" in capsys.readouterr().out


def test_extra_json_survives_the_tally(store):
    """extra is JSON but not a list of flags; it must not be mistaken for one."""
    item = add(store, "https://a.example/1")
    store.connection.execute(
        "UPDATE items SET extra = ? WHERE id = ?", (json.dumps({"points": 5}), item.id)
    )
    assert collect(store).total == 1


def test_a_single_item_gets_a_single_character_bar(store):
    """Scaling up makes one item look like a trend on the first week of data."""
    for i, score in enumerate([9, 8, 7]):
        item = add(store, f"https://a.example/{i}")
        store.set_score(item.id, score, "A line.", [])

    text = format_stats(collect(store))
    bars = [
        line.split()[1] for line in text.splitlines() if line.strip().startswith(("9", "8", "7"))
    ]
    assert all(bar == "#" for bar in bars)


def test_bars_are_scaled_down_when_counts_are_large(store):
    from papertrail.stats import BAR_WIDTH

    for i in range(120):
        item = add(store, f"https://a.example/{i}")
        store.set_score(item.id, 7, "A line.", [])
    item = add(store, "https://a.example/lonely")
    store.set_score(item.id, 2, "A line.", [])

    text = format_stats(collect(store))
    widest = max(len(line.split()[1]) for line in text.splitlines() if "#" in line)
    assert widest == BAR_WIDTH


def test_relative_heights_are_preserved(store):
    for i in range(40):
        item = add(store, f"https://a.example/hi{i}")
        store.set_score(item.id, 8, "A line.", [])
    for i in range(10):
        item = add(store, f"https://a.example/lo{i}")
        store.set_score(item.id, 3, "A line.", [])

    bars = {
        line.split()[0]: len(line.split()[1])
        for line in format_stats(collect(store)).splitlines()
        if "#" in line
    }
    assert bars["8"] > bars["3"] * 3
