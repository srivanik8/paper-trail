"""Reading back what the filter decided.

Every run records what it kept, what it dropped and why. That accumulates into
the most interesting artifact this project produces -- a labelled record of a
month of judgements -- and none of it is any use if there is no way to look at
it. This module is that way.

It answers the questions you actually ask after a few weeks: how much am I
throwing away, what is it being thrown away for, which flags fire often enough
to matter, and is the score distribution sane or is everything a 7.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .store import Store
from .timeutil import isoformat_utc, utcnow


@dataclass(frozen=True, slots=True)
class Stats:
    """A summary of what the store holds."""

    since: datetime | None
    total: int = 0
    by_status: dict[str, int] = field(default_factory=dict)
    by_evidence: dict[str, int] = field(default_factory=dict)
    by_reason: dict[str, int] = field(default_factory=dict)
    by_source: dict[str, int] = field(default_factory=dict)
    substance_flags: dict[str, int] = field(default_factory=dict)
    hype_flags: dict[str, int] = field(default_factory=dict)
    scores: dict[int, int] = field(default_factory=dict)
    sent: int = 0

    @property
    def kept_ratio(self) -> float:
        """Fraction of everything seen that survived to be a candidate."""
        if not self.total:
            return 0.0
        rejected = self.by_status.get("rejected", 0) + self.by_status.get("duplicate", 0)
        return (self.total - rejected) / self.total

    @property
    def median_score(self) -> int | None:
        """Middle of the score distribution, or ``None`` if nothing is scored."""
        values = [score for score, count in self.scores.items() for _ in range(count)]
        if not values:
            return None
        values.sort()
        return values[len(values) // 2]


def _tally(rows: list[sqlite3.Row], column: str) -> dict[str, int]:
    """Count non-null values of ``column``."""
    counts: dict[str, int] = {}
    for row in rows:
        value = row[column]
        if value is not None:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def _tally_json_list(rows: list[sqlite3.Row], column: str) -> dict[str, int]:
    """Count entries across a column holding JSON arrays of strings."""
    counts: dict[str, int] = {}
    for row in rows:
        raw = row[column]
        if not raw:
            continue
        try:
            values = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            continue
        for value in values if isinstance(values, list) else []:
            counts[str(value)] = counts.get(str(value), 0) + 1
    return counts


def collect(store: Store, since: datetime | None = None) -> Stats:
    """Summarize the store, optionally limited to items first seen since a moment."""
    if since is None:
        rows = list(store.connection.execute("SELECT * FROM items"))
    else:
        rows = store.since(since)

    scores: dict[int, int] = {}
    for row in rows:
        value = row["signal_score"]
        if value is not None:
            scores[int(value)] = scores.get(int(value), 0) + 1

    sent = store.connection.execute("SELECT COUNT(*) AS n FROM sends").fetchone()["n"]

    return Stats(
        since=since,
        total=len(rows),
        by_status=_tally(rows, "status"),
        by_evidence=_tally(rows, "evidence"),
        by_reason=_tally(rows, "reason"),
        by_source=_tally(rows, "source"),
        substance_flags=_tally_json_list(rows, "substance_flags"),
        hype_flags=_tally_json_list(rows, "hype_flags"),
        scores=scores,
        sent=sent,
    )


def _section(title: str, counts: dict[str, int], limit: int = 8) -> list[str]:
    """Render one tally, largest first."""
    if not counts:
        return []
    ranked = sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
    width = max(len(name) for name, _ in ranked)
    return [f"{title}:", *[f"  {name:<{width}}  {count:>5}" for name, count in ranked], ""]


def _histogram(scores: dict[int, int]) -> list[str]:
    """Render the score distribution as bars.

    Worth looking at: a rubric that scores everything a 7 is not a rubric.
    """
    if not scores:
        return []
    widest = max(scores.values())
    lines = ["score distribution:"]
    for score in range(10, -1, -1):
        count = scores.get(score, 0)
        if count:
            bar = "#" * max(1, round(count * 30 / widest))
            lines.append(f"  {score:>2}  {bar} {count}")
    return [*lines, ""]


def format_stats(stats: Stats) -> str:
    """Render a summary for the terminal."""
    window = f" since {isoformat_utc(stats.since)}" if stats.since else " (all time)"
    lines = [
        f"{stats.total} items{window}",
        f"{stats.kept_ratio:.0%} survived to be a candidate; {stats.sent} sent",
        "",
    ]

    lines += _section("status", stats.by_status)
    lines += _section("evidence", stats.by_evidence)
    lines += _section("dropped for", stats.by_reason)
    lines += _section("source", stats.by_source)
    lines += _section("substance flags", stats.substance_flags)
    lines += _section("hype flags", stats.hype_flags)
    lines += _histogram(stats.scores)

    median = stats.median_score
    if median is not None:
        lines.append(f"median score {median}")

    return "\n".join(lines).rstrip()


def window_from_days(days: int | None, now: datetime | None = None) -> datetime | None:
    """Convert a day count to a cutoff, or ``None`` for all time."""
    if days is None or days <= 0:
        return None
    return (now or utcnow()) - timedelta(days=days)
