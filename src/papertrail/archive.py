"""Keeping the database alive across ephemeral runners.

A GitHub Actions runner is destroyed when the job ends, so ``papertrail.db``
does not survive to the next morning -- and without it, deduplication forgets
everything and the same story arrives every day. ``actions/cache`` is the
obvious fix and the wrong one: cache entries are evicted without warning, and
the failure is silent.

So the state is committed to the repository as JSONL and the database is
rebuilt from it at the start of each run. That has three properties nothing
else offers: it survives indefinitely, a pull request diff shows exactly what
changed overnight, and the record of what the filter decided is visible in the
repo rather than trapped in a binary.

**Why not literally append-only.** Rows change -- an item is re-seen, a story
gets sent -- so an append-only log would carry several versions of each row and
need last-write-wins on replay. Instead the export writes current state sorted
by first-sighting, which is append-only *in practice*: new rows land at the end
of the file and git shows them as additions, while an updated row changes in
place instead of duplicating.

**Schema drift is expected.** The file outlives any one schema version, so
export writes whatever columns exist and restore keeps only the columns the
current database knows about. An archive written by a newer version loads into
an older one minus the new fields, rather than failing.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from .store import Store

ITEMS_FILE = "items.jsonl"
SCORES_FILE = "scores.jsonl"

#: Sorting by first sighting is what makes new rows append to the end of the
#: file; the id breaks ties so the output is byte-identical run to run.
_ITEM_ORDER = "ORDER BY first_seen_at, id"
_SCORE_ORDER = "ORDER BY scored_at, cluster_id"


@dataclass(frozen=True, slots=True)
class Counts:
    """How many rows moved."""

    items: int = 0
    scores: int = 0

    @property
    def total(self) -> int:
        """Rows in both tables."""
        return self.items + self.scores


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    """Column names the current schema has for ``table``."""
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}


def _dump(connection: sqlite3.Connection, table: str, order: str, path: Path) -> int:
    """Write every row of ``table`` to ``path`` as one JSON object per line."""
    path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with path.open("w", encoding="utf-8") as handle:
        for row in connection.execute(f"SELECT * FROM {table} {order}"):
            # sort_keys so a column reordering in SQLite never shows as a diff.
            handle.write(json.dumps(dict(row), ensure_ascii=False, sort_keys=True) + "\n")
            written += 1
    return written


def _load(connection: sqlite3.Connection, table: str, key: str, path: Path) -> int:
    """Replace rows in ``table`` from ``path``, ignoring unknown columns."""
    if not path.exists():
        return 0

    known = _columns(connection, table)
    loaded = 0

    with connection:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                # One corrupt line must not cost the whole archive.
                continue
            if key not in payload:
                continue

            fields = {name: value for name, value in payload.items() if name in known}
            placeholders = ",".join("?" * len(fields))
            connection.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(fields)}) VALUES ({placeholders})",
                tuple(fields.values()),
            )
            loaded += 1
    return loaded


def export(store: Store, directory: Path | str) -> Counts:
    """Write the store's contents to ``directory`` as JSONL."""
    root = Path(directory)
    return Counts(
        items=_dump(store.connection, "items", _ITEM_ORDER, root / ITEMS_FILE),
        scores=_dump(store.connection, "scores", _SCORE_ORDER, root / SCORES_FILE),
    )


def restore(store: Store, directory: Path | str) -> Counts:
    """Rebuild the store from JSONL in ``directory``.

    Rows already present are replaced, so restoring onto a live database is
    safe and restoring twice changes nothing.
    """
    root = Path(directory)
    return Counts(
        items=_load(store.connection, "items", "id", root / ITEMS_FILE),
        scores=_load(store.connection, "scores", "cluster_id", root / SCORES_FILE),
    )
