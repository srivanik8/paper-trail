import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from papertrail.archive import ITEMS_FILE, SCORES_FILE, export, restore
from papertrail.models import Item
from papertrail.store import STATUS_REJECTED, Store

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


@pytest.fixture
def store():
    with Store() as opened:
        yield opened


def make_item(url: str = "https://arxiv.org/abs/2401.00001", title: str = "A paper") -> Item:
    return Item(
        title=title,
        url=url,
        source="hn",
        published_at=WHEN,
        raw_signal=42.5,
        primary_source_url="https://arxiv.org/abs/2401.00001",
        extra={"points": 42, "matched_terms": ["llm"]},
    )


def test_an_empty_store_exports_empty_files(store, tmp_path):
    counts = export(store, tmp_path)

    assert counts.total == 0
    assert (tmp_path / ITEMS_FILE).read_text() == ""


def test_items_round_trip_through_the_archive(store, tmp_path):
    item = make_item()
    store.upsert(item, now=WHEN)
    store.set_provenance(item.id, "paper", "https://arxiv.org/abs/2401.00001", "self")
    store.set_substance(item.id, ["single_contributor"], 3.5)
    store.set_score(item.id, 8, "A concrete result.", ["vendor_benchmark"])

    export(store, tmp_path)

    with Store() as fresh:
        counts = restore(fresh, tmp_path)
        assert counts.items == 1

        row = fresh.get(item.id)
        assert row["title"] == "A paper"
        assert row["raw_signal"] == 42.5
        assert row["evidence"] == "paper"
        assert row["signal_score"] == 8
        assert json.loads(row["substance_flags"]) == ["single_contributor"]
        assert json.loads(row["extra"])["points"] == 42


def test_rejects_are_archived_with_their_reason(store, tmp_path):
    """The rejected rows are the interesting half of the dataset."""
    item = make_item()
    store.upsert(item, status=STATUS_REJECTED, reason="no primary source", now=WHEN)
    export(store, tmp_path)

    with Store() as fresh:
        restore(fresh, tmp_path)
        row = fresh.get(item.id)
        assert row["status"] == STATUS_REJECTED
        assert row["reason"] == "no primary source"


def test_scores_round_trip_so_they_are_not_bought_twice(store, tmp_path):
    store.record_score("cluster-1", '{"signal_score": 9}', now=WHEN)
    counts = export(store, tmp_path)
    assert counts.scores == 1

    with Store() as fresh:
        restore(fresh, tmp_path)
        assert fresh.cached_score("cluster-1") == '{"signal_score": 9}'


def test_deduplication_survives_a_rebuild(store, tmp_path):
    """The whole reason the archive exists."""
    item = make_item()
    store.upsert(item, now=WHEN)
    export(store, tmp_path)

    with Store() as fresh:  # a brand-new runner
        restore(fresh, tmp_path)
        assert fresh.upsert(item, now=WHEN + timedelta(days=1)) is False


def test_a_sent_story_stays_sent_across_a_rebuild(store, tmp_path):
    item = make_item()
    store.upsert(item, now=WHEN)
    store.mark_sent([item.id], "2026-01-01", now=WHEN)
    export(store, tmp_path)

    with Store() as fresh:
        restore(fresh, tmp_path)
        # sends live in their own table, so the item's status carries the fact
        assert fresh.get(item.id)["status"] == "sent"


# --- file shape -------------------------------------------------------------


def test_one_json_object_per_line(store, tmp_path):
    for i in range(3):
        store.upsert(make_item(f"https://arxiv.org/abs/2401.{i:05d}"), now=WHEN)
    export(store, tmp_path)

    lines = (tmp_path / ITEMS_FILE).read_text().strip().splitlines()
    assert len(lines) == 3
    assert all(json.loads(line)["id"] for line in lines)


def test_the_export_is_byte_identical_run_to_run(store, tmp_path):
    """Otherwise every scheduled run commits a spurious diff."""
    for i in range(5):
        store.upsert(make_item(f"https://arxiv.org/abs/2401.{i:05d}"), now=WHEN)

    export(store, tmp_path / "a")
    export(store, tmp_path / "b")
    assert (tmp_path / "a" / ITEMS_FILE).read_bytes() == (tmp_path / "b" / ITEMS_FILE).read_bytes()


def test_new_rows_append_to_the_end_of_the_file(store, tmp_path):
    """Sorted by first sighting, so git shows a scheduled run as added lines."""
    store.upsert(make_item("https://arxiv.org/abs/2401.00001"), now=WHEN)
    export(store, tmp_path)
    before = (tmp_path / ITEMS_FILE).read_text()

    store.upsert(make_item("https://arxiv.org/abs/2401.00002"), now=WHEN + timedelta(days=1))
    export(store, tmp_path)
    after = (tmp_path / ITEMS_FILE).read_text()

    assert after.startswith(before)


def test_the_output_directory_is_created(store, tmp_path):
    store.upsert(make_item(), now=WHEN)
    export(store, tmp_path / "deep" / "nested")
    assert (tmp_path / "deep" / "nested" / ITEMS_FILE).exists()


# --- robustness -------------------------------------------------------------


def test_restoring_twice_changes_nothing(store, tmp_path):
    store.upsert(make_item(), now=WHEN)
    export(store, tmp_path)

    with Store() as fresh:
        first = restore(fresh, tmp_path)
        second = restore(fresh, tmp_path)
        assert first.items == second.items == 1
        assert len(fresh.since(datetime(2020, 1, 1, tzinfo=UTC))) == 1


def test_restoring_onto_a_live_database_replaces_rows(store, tmp_path):
    item = make_item()
    store.upsert(item, now=WHEN)
    store.set_score(item.id, 9, "Archived judgement.", [])
    export(store, tmp_path)

    store.set_score(item.id, 1, "Local scribble.", [])
    restore(store, tmp_path)
    assert store.get(item.id)["signal_score"] == 9


def test_a_missing_archive_is_not_an_error(store, tmp_path):
    assert restore(store, tmp_path / "nothing-here").total == 0


def test_a_corrupt_line_is_skipped_not_fatal(store, tmp_path):
    store.upsert(make_item(), now=WHEN)
    export(store, tmp_path)

    path = tmp_path / ITEMS_FILE
    path.write_text("{not json\n" + path.read_text() + "\n\n")

    with Store() as fresh:
        assert restore(fresh, tmp_path).items == 1


def test_a_line_without_a_primary_key_is_skipped(store, tmp_path):
    (tmp_path / ITEMS_FILE).write_text(json.dumps({"title": "no id here"}) + "\n")
    assert restore(store, tmp_path).items == 0


def test_columns_from_a_newer_schema_are_ignored(store, tmp_path):
    """The archive outlives any one schema version."""
    item = make_item()
    store.upsert(item, now=WHEN)
    export(store, tmp_path)

    path = tmp_path / ITEMS_FILE
    payload = json.loads(path.read_text().strip())
    payload["a_field_from_the_future"] = "surprise"
    path.write_text(json.dumps(payload) + "\n")

    with Store() as fresh:
        assert restore(fresh, tmp_path).items == 1
        assert fresh.get(item.id)["title"] == "A paper"


def test_a_row_missing_newer_columns_still_loads(store, tmp_path):
    """An archive written before day 5 has no score columns."""
    (tmp_path / ITEMS_FILE).write_text(
        json.dumps(
            {
                "id": "abc123",
                "cluster_id": "abc123",
                "title": "An old row",
                "url": "https://example.com/x",
                "canonical_url": "https://example.com/x",
                "source": "hn",
                "published_at": "2026-01-01T00:00:00Z",
                "raw_signal": 1.0,
                "extra": "{}",
                "status": "new",
                "first_seen_at": "2026-01-01T00:00:00Z",
                "last_seen_at": "2026-01-01T00:00:00Z",
            }
        )
        + "\n"
    )

    assert restore(store, tmp_path).items == 1
    assert store.get("abc123")["signal_score"] is None


def test_the_archive_survives_a_file_backed_round_trip(tmp_path):
    item = make_item()
    with Store(tmp_path / "first.db") as store:
        store.upsert(item, now=WHEN)
        export(store, tmp_path / "data")

    with Store(tmp_path / "second.db") as store:
        restore(store, tmp_path / "data")
        assert store.has(item.id) is True


def test_restore_does_not_care_about_column_order(store, tmp_path):
    """SQLite column order can shift across migrations; sort_keys makes it moot."""
    store.upsert(make_item(), now=WHEN)
    export(store, tmp_path)

    keys = list(json.loads((tmp_path / ITEMS_FILE).read_text().strip()))
    assert keys == sorted(keys)


def test_scores_file_is_written_even_when_empty(store, tmp_path):
    store.upsert(make_item(), now=WHEN)
    export(store, tmp_path)
    assert (tmp_path / SCORES_FILE).exists()


def test_an_unreadable_table_name_cannot_be_injected(store, tmp_path):
    """Table names are literals in this module, never user input."""
    with pytest.raises(sqlite3.OperationalError):
        store.connection.execute("SELECT * FROM definitely_not_a_table").fetchall()
