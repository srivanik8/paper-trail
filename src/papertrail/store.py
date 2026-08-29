"""SQLite persistence.

Two things live here, and the distinction matters:

``items``
    Every item the pipeline has ever seen, **including the ones it threw away**,
    with the reason it threw them away. Rejects are not noise to be discarded;
    a month of them is a labelled record of what the rubric decided, and the
    only way to tune the rubric later.

``sends``
    What actually went out, and in which digest. This is what stops the same
    story arriving four mornings running.

``pages``
    Bodies already fetched while resolving provenance, so a URL is retrieved
    once and only once. Politeness is a correctness property here, not a
    courtesy: the resolver reads other people's sites.

Nothing here reaches the network and nothing here ranks. The store records
decisions; it does not make them.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from .ids import canonical_url
from .models import Item
from .timeutil import isoformat_utc, parse_iso, to_utc, utcnow

SCHEMA_VERSION = 2

#: Lifecycle of an item. ``new`` means "survived to be a candidate"; the rest
#: record why it stopped, so a query can always answer "why didn't I see this?"
STATUS_NEW = "new"
STATUS_DUPLICATE = "duplicate"
STATUS_REJECTED = "rejected"
STATUS_SENT = "sent"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS items (
    id                 TEXT PRIMARY KEY,
    cluster_id         TEXT NOT NULL,
    title              TEXT NOT NULL,
    url                TEXT NOT NULL,
    canonical_url      TEXT NOT NULL,
    source             TEXT NOT NULL,
    published_at       TEXT NOT NULL,
    raw_signal         REAL NOT NULL,
    primary_source_url TEXT,
    discussion_url     TEXT,
    source_id          TEXT,
    extra              TEXT NOT NULL DEFAULT '{}',
    status             TEXT NOT NULL,
    reason             TEXT,
    first_seen_at      TEXT NOT NULL,
    last_seen_at       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS items_published_at ON items (published_at);
CREATE INDEX IF NOT EXISTS items_cluster      ON items (cluster_id);
CREATE INDEX IF NOT EXISTS items_status       ON items (status);

CREATE TABLE IF NOT EXISTS sends (
    item_id     TEXT NOT NULL,
    digest_date TEXT NOT NULL,
    sent_at     TEXT NOT NULL,
    PRIMARY KEY (item_id, digest_date)
);
"""

#: Applied in order to a database created at an earlier version. Each entry
#: must be safe to run exactly once, on a database that already holds data.
_MIGRATIONS: dict[int, str] = {
    2: """
    CREATE TABLE IF NOT EXISTS pages (
        url          TEXT PRIMARY KEY,
        status       INTEGER NOT NULL,
        content_type TEXT,
        body         TEXT NOT NULL DEFAULT '',
        error        TEXT,
        fetched_at   TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS pages_fetched_at ON pages (fetched_at);

    ALTER TABLE items ADD COLUMN evidence TEXT;
    ALTER TABLE items ADD COLUMN provenance_via TEXT;
    """,
}


class Store:
    """A SQLite-backed record of everything the pipeline has seen.

    Use as a context manager; the connection is closed on exit::

        with Store("papertrail.db") as store:
            store.upsert(item, cluster_id=cluster_id)
    """

    def __init__(self, path: str | Path = ":memory:") -> None:
        """Open (and if necessary create) the database at ``path``.

        ``:memory:`` gives an ephemeral store, which is what the tests use.
        """
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)

        self.connection = sqlite3.connect(self.path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        if self.path != ":memory:":
            # Concurrent readers during a run; irrelevant (and unsupported) in memory.
            self.connection.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Close the underlying connection."""
        self.connection.close()

    def _migrate(self) -> None:
        """Create the base schema, then apply any migrations this file is behind.

        A brand-new database gets the base schema and then every migration in
        order, so there is exactly one code path and it is the one that gets
        exercised on every run.
        """
        with self.connection:
            existing = self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
            ).fetchone()

            self.connection.executescript(_SCHEMA)
            if existing is None:
                self.connection.execute(
                    "INSERT INTO meta (key, value) VALUES ('schema_version', '1')"
                )

            current = int(
                self.connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'"
                ).fetchone()["value"]
            )
            for version in sorted(_MIGRATIONS):
                if version > current:
                    self.connection.executescript(_MIGRATIONS[version])
                    current = version

            self.connection.execute(
                "UPDATE meta SET value = ? WHERE key = 'schema_version'", (str(current),)
            )

    @property
    def schema_version(self) -> int:
        """Version of the schema this database was created with."""
        row = self.connection.execute(
            "SELECT value FROM meta WHERE key = 'schema_version'"
        ).fetchone()
        return int(row["value"])

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Group several writes so a failure rolls all of them back."""
        with self.connection:
            yield self.connection

    def upsert(
        self,
        item: Item,
        *,
        cluster_id: str | None = None,
        status: str = STATUS_NEW,
        reason: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        """Record ``item``, returning ``True`` if this is the first sighting.

        A repeat sighting refreshes ``last_seen_at`` and keeps the **highest**
        ``raw_signal`` seen so far -- a story climbing the HN front page should
        not lose its score because a later poll caught a cached number. The
        first-seen timestamp and the existing status are never overwritten:
        once something has been sent or rejected, re-ingesting it must not
        quietly resurrect it.
        """
        stamp = isoformat_utc(now or utcnow())
        row = (
            item.id,
            cluster_id or item.id,
            item.title,
            item.url,
            canonical_url(item.url),
            item.source,
            isoformat_utc(item.published_at),
            item.raw_signal,
            item.primary_source_url,
            item.discussion_url,
            item.source_id,
            json.dumps(item.extra, ensure_ascii=False),
            status,
            reason,
            stamp,
            stamp,
        )
        # Ask before writing: after the upsert, a row inserted now and a row
        # re-seen within the same second are indistinguishable by timestamp.
        is_new = not self.has(item.id)

        self.connection.execute(
            """
            INSERT INTO items (
                id, cluster_id, title, url, canonical_url, source, published_at,
                raw_signal, primary_source_url, discussion_url, source_id, extra,
                status, reason, first_seen_at, last_seen_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET
                last_seen_at = excluded.last_seen_at,
                raw_signal   = MAX(items.raw_signal, excluded.raw_signal),
                primary_source_url =
                    COALESCE(items.primary_source_url, excluded.primary_source_url)
            """,
            row,
        )
        self.connection.commit()
        return is_new

    def has(self, item_id: str) -> bool:
        """True if this exact item id is already recorded."""
        return (
            self.connection.execute(
                "SELECT 1 FROM items WHERE id = ? LIMIT 1", (item_id,)
            ).fetchone()
            is not None
        )

    def get(self, item_id: str) -> sqlite3.Row | None:
        """Return the stored row for ``item_id``, or ``None``."""
        return self.connection.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()

    def since(self, moment: datetime) -> list[sqlite3.Row]:
        """Every item first seen at or after ``moment``, newest first.

        Keyed on first-seen rather than published-at: the dedup window asks
        "have I dealt with this recently", which is about when we saw it.
        """
        return list(
            self.connection.execute(
                "SELECT * FROM items WHERE first_seen_at >= ? ORDER BY first_seen_at DESC",
                (isoformat_utc(moment),),
            )
        )

    def set_status(self, item_id: str, status: str, reason: str | None = None) -> None:
        """Move an item to ``status``, recording why."""
        with self.connection:
            self.connection.execute(
                "UPDATE items SET status = ?, reason = ? WHERE id = ?",
                (status, reason, item_id),
            )

    def mark_sent(
        self, item_ids: Iterable[str], digest_date: str, now: datetime | None = None
    ) -> int:
        """Record that these items went out in ``digest_date``'s digest.

        Returns the number newly recorded. Re-sending the same item in the same
        digest is a no-op, so a retried delivery does not double-count.
        """
        stamp = isoformat_utc(now or utcnow())
        ids = list(item_ids)
        with self.connection:
            before = self.connection.total_changes
            self.connection.executemany(
                "INSERT OR IGNORE INTO sends (item_id, digest_date, sent_at) VALUES (?,?,?)",
                [(item_id, digest_date, stamp) for item_id in ids],
            )
            recorded = self.connection.total_changes - before
            self.connection.executemany(
                "UPDATE items SET status = ? WHERE id = ?",
                [(STATUS_SENT, item_id) for item_id in ids],
            )
        return recorded

    def was_sent(self, item_id: str) -> bool:
        """True if this item has gone out in any digest."""
        return (
            self.connection.execute(
                "SELECT 1 FROM sends WHERE item_id = ? LIMIT 1", (item_id,)
            ).fetchone()
            is not None
        )

    def counts_by_status(self) -> dict[str, int]:
        """Item tally keyed by status."""
        return {
            row["status"]: row["n"]
            for row in self.connection.execute(
                "SELECT status, COUNT(*) AS n FROM items GROUP BY status"
            )
        }

    # --- page cache ---------------------------------------------------------

    def cached_page(self, url: str, fresh_after: datetime | None = None) -> sqlite3.Row | None:
        """Return a cached fetch for ``url``, or ``None``.

        Args:
            url: Canonical URL of the page.
            fresh_after: Ignore entries fetched before this moment. Without it
                any cached entry counts, however old.
        """
        row = self.connection.execute("SELECT * FROM pages WHERE url = ?", (url,)).fetchone()
        if row is None:
            return None
        if fresh_after is not None and parse_iso(row["fetched_at"]) < to_utc(fresh_after):
            return None
        return row

    def cache_page(
        self,
        url: str,
        *,
        status: int,
        body: str = "",
        content_type: str | None = None,
        error: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Record the outcome of fetching ``url``.

        Failures are cached too, deliberately: a URL that timed out or returned
        404 must not be retried on every run.
        """
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO pages (url, status, content_type, body, error, fetched_at)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT(url) DO UPDATE SET
                    status       = excluded.status,
                    content_type = excluded.content_type,
                    body         = excluded.body,
                    error        = excluded.error,
                    fetched_at   = excluded.fetched_at
                """,
                (url, status, content_type, body, error, isoformat_utc(now or utcnow())),
            )

    def set_provenance(self, item_id: str, evidence: str, url: str | None, via: str | None) -> None:
        """Record what an item's URL was found to point at."""
        with self.connection:
            self.connection.execute(
                """
                UPDATE items
                   SET evidence = ?, primary_source_url = ?, provenance_via = ?
                 WHERE id = ?
                """,
                (evidence, url, via, item_id),
            )

    @staticmethod
    def row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        """Convert a row to plain values, decoding ``extra`` and timestamps."""
        payload = dict(row)
        payload["extra"] = json.loads(payload["extra"])
        payload["published_at"] = parse_iso(payload["published_at"])
        return payload
