"""The v1 -> v2 migration, exercised on a database that already holds data."""

import sqlite3
from datetime import UTC, datetime, timedelta

from papertrail.models import Item
from papertrail.store import SCHEMA_VERSION, Store

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE items (
    id TEXT PRIMARY KEY, cluster_id TEXT NOT NULL, title TEXT NOT NULL,
    url TEXT NOT NULL, canonical_url TEXT NOT NULL, source TEXT NOT NULL,
    published_at TEXT NOT NULL, raw_signal REAL NOT NULL, primary_source_url TEXT,
    discussion_url TEXT, source_id TEXT, extra TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL, reason TEXT, first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE sends (
    item_id TEXT NOT NULL, digest_date TEXT NOT NULL, sent_at TEXT NOT NULL,
    PRIMARY KEY (item_id, digest_date)
);
INSERT INTO meta (key, value) VALUES ('schema_version', '1');
INSERT INTO items VALUES (
    'abc123', 'abc123', 'An older story', 'https://example.com/old',
    'https://example.com/old', 'hn', '2026-01-01T00:00:00Z', 42.0, NULL, NULL,
    NULL, '{}', 'new', NULL, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z'
);
"""


def make_v1_database(path) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(V1_SCHEMA)
    connection.commit()
    connection.close()


def test_a_v1_database_is_upgraded_in_place(tmp_path):
    path = tmp_path / "old.db"
    make_v1_database(path)

    with Store(path) as store:
        assert store.schema_version == SCHEMA_VERSION


def test_existing_rows_survive_the_upgrade(tmp_path):
    path = tmp_path / "old.db"
    make_v1_database(path)

    with Store(path) as store:
        row = store.get("abc123")
        assert row["title"] == "An older story"
        assert row["raw_signal"] == 42.0
        # Columns added by the migration exist and are empty for old rows.
        assert row["evidence"] is None
        assert row["provenance_via"] is None


def test_the_page_cache_appears_after_the_upgrade(tmp_path):
    path = tmp_path / "old.db"
    make_v1_database(path)

    with Store(path) as store:
        store.cache_page("https://example.com/x", status=200, body="<html></html>")
        assert store.cached_page("https://example.com/x")["status"] == 200


def test_reopening_an_already_migrated_database_is_a_no_op(tmp_path):
    path = tmp_path / "old.db"
    make_v1_database(path)

    with Store(path) as store:
        store.cache_page("https://example.com/x", status=200, body="hello")
    with Store(path) as store:
        assert store.schema_version == SCHEMA_VERSION
        assert store.cached_page("https://example.com/x")["body"] == "hello"


def test_a_fresh_database_lands_on_the_current_version(tmp_path):
    with Store(tmp_path / "new.db") as store:
        assert store.schema_version == SCHEMA_VERSION


# --- the cache itself -------------------------------------------------------


def test_a_cached_page_is_returned():
    with Store() as store:
        store.cache_page(
            "https://example.com/a", status=200, body="<html>hi</html>", content_type="text/html"
        )
        row = store.cached_page("https://example.com/a")
        assert row["body"] == "<html>hi</html>"
        assert row["content_type"] == "text/html"


def test_an_uncached_url_returns_none():
    with Store() as store:
        assert store.cached_page("https://example.com/missing") is None


def test_failures_are_cached_so_they_are_not_retried_every_run():
    with Store() as store:
        store.cache_page("https://example.com/dead", status=0, error="ConnectTimeout")
        row = store.cached_page("https://example.com/dead")
        assert row["error"] == "ConnectTimeout"
        assert row["body"] == ""


def test_a_stale_entry_is_ignored_when_freshness_is_required():
    with Store() as store:
        store.cache_page("https://example.com/a", status=200, body="old", now=WHEN)
        assert (
            store.cached_page("https://example.com/a", fresh_after=WHEN + timedelta(days=1)) is None
        )
        assert store.cached_page("https://example.com/a", fresh_after=WHEN - timedelta(days=1))


def test_refetching_replaces_the_cached_entry():
    with Store() as store:
        store.cache_page("https://example.com/a", status=500, error="server error", now=WHEN)
        store.cache_page(
            "https://example.com/a", status=200, body="recovered", now=WHEN + timedelta(hours=1)
        )
        row = store.cached_page("https://example.com/a")
        assert (row["status"], row["body"], row["error"]) == (200, "recovered", None)


def test_provenance_is_recorded_against_an_item():
    with Store() as store:
        item = Item(
            title="A paper writeup",
            url="https://example.com/post",
            source="hn",
            published_at=WHEN,
            raw_signal=10.0,
        )
        store.upsert(item)
        store.set_provenance(item.id, "paper", "https://arxiv.org/abs/2401.00001", "page")

        row = store.get(item.id)
        assert row["evidence"] == "paper"
        assert row["primary_source_url"] == "https://arxiv.org/abs/2401.00001"
        assert row["provenance_via"] == "page"


# --- v2 -> v3 ---------------------------------------------------------------


def test_a_v2_database_gains_the_substance_columns(tmp_path):
    """A database created before day 4 must upgrade without losing rows."""
    path = tmp_path / "v2.db"
    make_v1_database(path)

    with Store(path) as store:  # v1 -> v3 in one pass
        assert store.schema_version == SCHEMA_VERSION
        row = store.get("abc123")
        assert row["title"] == "An older story"
        assert row["substance_flags"] is None
        assert row["star_velocity"] is None


def test_substance_is_recorded_against_an_item(tmp_path):
    import json

    with Store(tmp_path / "p.db") as store:
        item = Item(
            title="A repository launch",
            url="https://github.com/owner/repo",
            source="hn",
            published_at=WHEN,
            raw_signal=10.0,
        )
        store.upsert(item)
        store.set_substance(item.id, ["readme_only", "single_contributor"], 12.5)

        row = store.get(item.id)
        assert json.loads(row["substance_flags"]) == ["readme_only", "single_contributor"]
        assert row["star_velocity"] == 12.5


def test_an_item_with_no_flags_records_an_empty_list(tmp_path):
    import json

    with Store(tmp_path / "p.db") as store:
        item = Item(
            title="A solid repository",
            url="https://github.com/owner/solid",
            source="hn",
            published_at=WHEN,
            raw_signal=10.0,
        )
        store.upsert(item)
        store.set_substance(item.id, [], 0.4)
        assert json.loads(store.get(item.id)["substance_flags"]) == []


# --- v3 -> v4 ---------------------------------------------------------------


def test_a_pre_scoring_database_gains_the_score_columns(tmp_path):
    path = tmp_path / "old.db"
    make_v1_database(path)

    with Store(path) as store:  # v1 -> v4 in one pass
        assert store.schema_version == SCHEMA_VERSION
        row = store.get("abc123")
        assert row["title"] == "An older story"
        assert row["signal_score"] is None
        assert row["one_line"] is None


def test_a_score_is_recorded_against_an_item(tmp_path):
    import json

    with Store(tmp_path / "p.db") as store:
        item = Item(
            title="A paper release",
            url="https://arxiv.org/abs/2401.00001",
            source="hn",
            published_at=WHEN,
            raw_signal=10.0,
        )
        store.upsert(item)
        store.set_score(item.id, 8, "A concrete scaling result.", ["vendor_benchmark"])

        row = store.get(item.id)
        assert row["signal_score"] == 8
        assert row["one_line"] == "A concrete scaling result."
        assert json.loads(row["hype_flags"]) == ["vendor_benchmark"]


def test_a_cached_score_survives_reopening(tmp_path):
    with Store(tmp_path / "p.db") as store:
        store.record_score("cluster-1", '{"signal_score": 7}', now=WHEN)
    with Store(tmp_path / "p.db") as store:
        assert store.cached_score("cluster-1") == '{"signal_score": 7}'


def test_rescoring_replaces_the_cached_payload(tmp_path):
    with Store(tmp_path / "p.db") as store:
        store.record_score("cluster-1", '{"signal_score": 3}', now=WHEN)
        store.record_score("cluster-1", '{"signal_score": 9}', now=WHEN)
        assert store.cached_score("cluster-1") == '{"signal_score": 9}'


def test_an_unscored_cluster_has_no_cached_payload(tmp_path):
    with Store(tmp_path / "p.db") as store:
        assert store.cached_score("never-seen") is None
