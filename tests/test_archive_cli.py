"""The export and restore commands."""

import json
from datetime import UTC, datetime

import pytest

from papertrail.archive import ITEMS_FILE
from papertrail.cli import main
from papertrail.models import Item
from papertrail.store import Store

WHEN = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def seed(db, url: str = "https://arxiv.org/abs/2401.00001") -> str:
    item = Item(title="A paper", url=url, source="hn", published_at=WHEN, raw_signal=10.0)
    with Store(db) as store:
        store.upsert(item, now=WHEN)
        store.record_score(item.id, '{"signal_score": 8}', now=WHEN)
    return item.id


def test_export_writes_the_archive(tmp_path, capsys):
    db = str(tmp_path / "p.db")
    seed(db)

    assert main(["export", "--db", db, "--data", str(tmp_path / "data")]) == 0

    assert (tmp_path / "data" / ITEMS_FILE).exists()
    assert "exported 1 items, 1 scores" in capsys.readouterr().out


def test_restore_rebuilds_a_fresh_database(tmp_path, capsys):
    source = str(tmp_path / "first.db")
    item_id = seed(source)
    main(["export", "--db", source, "--data", str(tmp_path / "data")])
    capsys.readouterr()

    target = str(tmp_path / "second.db")
    assert main(["restore", "--db", target, "--data", str(tmp_path / "data")]) == 0
    assert "restored 1 items" in capsys.readouterr().out

    with Store(target) as store:
        assert store.has(item_id) is True
        assert store.cached_score(item_id) == '{"signal_score": 8}'


def test_a_round_trip_preserves_deduplication(tmp_path):
    """A rebuilt runner must not re-report yesterday's stories."""
    source = str(tmp_path / "first.db")
    seed(source)
    main(["export", "--db", source, "--data", str(tmp_path / "data")])

    target = str(tmp_path / "second.db")
    main(["restore", "--db", target, "--data", str(tmp_path / "data")])

    item = Item(
        title="A paper",
        url="https://arxiv.org/abs/2401.00001",
        source="hn",
        published_at=WHEN,
        raw_signal=10.0,
    )
    with Store(target) as store:
        assert store.upsert(item, now=WHEN) is False


def test_restoring_from_nothing_is_not_an_error(tmp_path, capsys):
    assert main(["restore", "--db", str(tmp_path / "p.db"), "--data", str(tmp_path / "gone")]) == 0
    assert "restored 0 items" in capsys.readouterr().out


def test_exporting_an_empty_store_is_not_an_error(tmp_path, capsys):
    assert main(["export", "--db", str(tmp_path / "p.db"), "--data", str(tmp_path / "data")]) == 0
    assert "exported 0 items" in capsys.readouterr().out


def test_the_archive_is_diffable_json(tmp_path):
    db = str(tmp_path / "p.db")
    seed(db)
    main(["export", "--db", db, "--data", str(tmp_path / "data")])

    line = (tmp_path / "data" / ITEMS_FILE).read_text().strip()
    payload = json.loads(line)
    assert payload["title"] == "A paper"
    assert list(payload) == sorted(payload)
