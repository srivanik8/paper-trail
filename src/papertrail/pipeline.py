"""Runs the configured sources and collects their items.

Day 1 is a straight fan-out with no persistence: every source is asked for the
same window and the results are concatenated and sorted. Deduplication and the
SQLite store land on day 2 and slot in right here, between collection and
return.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .models import Item
from .sources import REGISTRY
from .sources.base import Source
from .timeutil import utcnow


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one pipeline run produced."""

    items: list[Item]
    since: datetime
    errors: dict[str, str]

    @property
    def per_source(self) -> dict[str, int]:
        """Item count keyed by source name."""
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.source] = counts.get(item.source, 0) + 1
        return counts


def build_sources(names: list[str] | None = None, **kwargs: object) -> list[Source]:
    """Instantiate sources by name, defaulting to everything registered.

    Raises:
        KeyError: if a requested name is not in the registry.
    """
    chosen = names or list(REGISTRY)
    sources: list[Source] = []
    for name in chosen:
        if name not in REGISTRY:
            raise KeyError(f"unknown source {name!r}; known: {', '.join(sorted(REGISTRY))}")
        sources.append(REGISTRY[name](**kwargs))  # type: ignore[arg-type]
    return sources


def run(window: timedelta, sources: list[Source], now: datetime | None = None) -> RunResult:
    """Fetch from every source over ``window`` and return the merged items.

    A source that raises is recorded in ``errors`` and skipped; one broken feed
    must not take the digest down with it.
    """
    since = (now or utcnow()) - window
    items: list[Item] = []
    errors: dict[str, str] = {}

    for source in sources:
        try:
            items.extend(source.fetch(since))
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            errors[source.name] = f"{type(exc).__name__}: {exc}"

    items.sort(key=lambda item: (-item.raw_signal, item.title.lower()))
    return RunResult(items=items, since=since, errors=errors)
