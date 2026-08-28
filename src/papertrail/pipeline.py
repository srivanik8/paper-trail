"""Runs the configured sources, deduplicates, and records what it saw.

The order matters. Sources fan out, their items are clustered against both each
other and the rolling window of what previous runs stored, and only then does
anything get written. A story that arrived yesterday is recognised as a
continuation rather than reported as new -- which is the whole point of having
a store.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .dedup import DEFAULT_THRESHOLD, Cluster, Known, deduplicate
from .models import Item
from .sources import REGISTRY
from .sources.base import Source
from .store import STATUS_DUPLICATE, STATUS_NEW, Store
from .timeutil import utcnow

#: How far back to look for stories a previous run already handled. Longer than
#: the usual fetch window on purpose: a launch resurfaces for days.
DEFAULT_DEDUP_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one pipeline run produced."""

    clusters: list[Cluster]
    since: datetime
    errors: dict[str, str] = field(default_factory=dict)
    fetched: int = 0

    @property
    def items(self) -> list[Item]:
        """Canonical item of every cluster, best-supported first."""
        return [cluster.canonical for cluster in self.clusters]

    @property
    def fresh(self) -> list[Cluster]:
        """Clusters this run saw for the first time."""
        return [cluster for cluster in self.clusters if not cluster.is_continuation]

    @property
    def continuing(self) -> list[Cluster]:
        """Clusters a previous run already recorded."""
        return [cluster for cluster in self.clusters if cluster.is_continuation]

    @property
    def collapsed(self) -> int:
        """How many items were folded into another item's cluster."""
        return sum(len(cluster.duplicates) for cluster in self.clusters)

    @property
    def per_source(self) -> dict[str, int]:
        """Canonical-item count keyed by source name."""
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


def collect(
    window: timedelta, sources: list[Source], now: datetime | None = None
) -> tuple[list[Item], datetime, dict[str, str]]:
    """Fetch from every source over ``window``.

    A source that raises is recorded and skipped; one broken feed must not take
    the digest down with it.
    """
    since = (now or utcnow()) - window
    items: list[Item] = []
    errors: dict[str, str] = {}

    for source in sources:
        try:
            items.extend(source.fetch(since))
        except Exception as exc:  # noqa: BLE001 - one bad feed must not stop the run
            errors[source.name] = f"{type(exc).__name__}: {exc}"

    return items, since, errors


def known_clusters(store: Store, moment: datetime) -> list[Known]:
    """Cluster titles recorded since ``moment``, one entry per cluster.

    Only the canonical title of each cluster is offered for matching, for the
    same reason clustering compares against a canonical member: a cluster must
    not drift as members accumulate.
    """
    seen: dict[str, Known] = {}
    for row in store.since(moment):
        seen.setdefault(row["cluster_id"], Known(cluster_id=row["cluster_id"], title=row["title"]))
    return list(seen.values())


def run(
    window: timedelta,
    sources: list[Source],
    store: Store | None = None,
    *,
    now: datetime | None = None,
    dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
    persist: bool = True,
) -> RunResult:
    """Fetch, deduplicate and record.

    Args:
        window: How far back to ask sources for items.
        sources: Ingesters to run.
        store: Where to record what was seen. Without one the run is stateless,
            so every item looks new -- fine for a one-off, useless for a digest.
        now: Clock override, for tests.
        dedup_window: How far back to look for stories already handled.
        threshold: Title similarity required to merge two items.
        persist: Set ``False`` to compute everything and write nothing.

    Returns:
        A :class:`RunResult` whose clusters are ranked by signal, descending.
    """
    items, since, errors = collect(window, sources, now)

    known = []
    if store is not None:
        known = known_clusters(store, (now or utcnow()) - dedup_window)

    clusters = deduplicate(items, known=known, threshold=threshold)

    if store is not None and persist:
        _record(store, clusters, now)

    return RunResult(clusters=clusters, since=since, errors=errors, fetched=len(items))


def _record(store: Store, clusters: list[Cluster], now: datetime | None) -> None:
    """Write every member of every cluster, canonical members marked ``new``.

    Duplicates are stored too, not dropped: knowing that a story was carried by
    four outlets is signal, and the rejected rows are the dataset the rubric
    gets tuned against later.
    """
    with store.transaction():
        for cluster in clusters:
            store.upsert(
                cluster.canonical, cluster_id=cluster.cluster_id, status=STATUS_NEW, now=now
            )
            for duplicate in cluster.duplicates:
                store.upsert(
                    duplicate,
                    cluster_id=cluster.cluster_id,
                    status=STATUS_DUPLICATE,
                    reason=f"duplicate of {cluster.canonical.id}",
                    now=now,
                )
