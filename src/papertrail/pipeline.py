"""Runs the configured sources, deduplicates, and records what it saw.

The order matters. Sources fan out, their items are clustered against both each
other and the rolling window of what previous runs stored, each surviving
cluster is resolved to a primary source, and only then does anything get
written. A story that arrived yesterday is recognised as a continuation rather
than reported as new -- which is the whole point of having a store.

Resolution is where the volume goes. Clusters that cannot be traced to a paper,
a repository, published weights or an official post are recorded with the
reason and then dropped, so nothing downstream -- and nothing billable -- ever
sees them.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .dedup import DEFAULT_THRESHOLD, Cluster, Known, deduplicate
from .models import Item
from .provenance import NONE, Evidence, Provenance, classify
from .resolver import Resolver
from .sources import REGISTRY
from .sources.base import Source
from .store import STATUS_DUPLICATE, STATUS_NEW, STATUS_REJECTED, Store
from .timeutil import utcnow

#: How far back to look for stories a previous run already handled. Longer than
#: the usual fetch window on purpose: a launch resurfaces for days.
DEFAULT_DEDUP_WINDOW = timedelta(days=7)


@dataclass(frozen=True, slots=True)
class Story:
    """A cluster together with what it can be checked against."""

    cluster: Cluster
    provenance: Provenance = NONE

    @property
    def canonical(self) -> Item:
        """The best-supported member of the cluster."""
        return self.cluster.canonical

    @property
    def evidence(self) -> Evidence:
        """What this story points at."""
        return self.provenance.evidence

    @property
    def resolved(self) -> bool:
        """True if this story points at something checkable."""
        return self.provenance.resolved


@dataclass(frozen=True, slots=True)
class RunResult:
    """What one pipeline run produced."""

    stories: list[Story]
    since: datetime
    errors: dict[str, str] = field(default_factory=dict)
    fetched: int = 0
    dropped: list[Story] = field(default_factory=list)
    pages_fetched: int = 0

    @property
    def clusters(self) -> list[Cluster]:
        """Surviving clusters, best-supported first."""
        return [story.cluster for story in self.stories]

    @property
    def items(self) -> list[Item]:
        """Canonical item of every surviving story, best-supported first."""
        return [story.canonical for story in self.stories]

    @property
    def fresh(self) -> list[Story]:
        """Stories this run saw for the first time."""
        return [story for story in self.stories if not story.cluster.is_continuation]

    @property
    def continuing(self) -> list[Story]:
        """Stories a previous run already recorded."""
        return [story for story in self.stories if story.cluster.is_continuation]

    @property
    def collapsed(self) -> int:
        """How many items were folded into another item's cluster."""
        return sum(len(story.cluster.duplicates) for story in self.stories)

    @property
    def per_source(self) -> dict[str, int]:
        """Canonical-item count keyed by source name."""
        counts: dict[str, int] = {}
        for item in self.items:
            counts[item.source] = counts.get(item.source, 0) + 1
        return counts

    @property
    def per_evidence(self) -> dict[str, int]:
        """Surviving-story count keyed by evidence type."""
        counts: dict[str, int] = {}
        for story in self.stories:
            counts[story.evidence.value] = counts.get(story.evidence.value, 0) + 1
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
    resolver: Resolver | None = None,
    now: datetime | None = None,
    dedup_window: timedelta = DEFAULT_DEDUP_WINDOW,
    threshold: int = DEFAULT_THRESHOLD,
    persist: bool = True,
    require_provenance: bool = True,
) -> RunResult:
    """Fetch, deduplicate, resolve and record.

    Args:
        window: How far back to ask sources for items.
        sources: Ingesters to run.
        store: Where to record what was seen. Without one the run is stateless,
            so every item looks new -- fine for a one-off, useless for a digest.
        resolver: Finds each cluster's primary source. Without one, resolution
            is skipped and nothing is dropped for lack of provenance.
        now: Clock override, for tests.
        dedup_window: How far back to look for stories already handled.
        threshold: Title similarity required to merge two items.
        persist: Set ``False`` to compute everything and write nothing.
        require_provenance: Drop stories that resolve to nothing. Turning this
            off is for inspecting what the resolver misses, not for producing
            a digest.

    Returns:
        A :class:`RunResult` whose stories are ranked by signal, descending.
    """
    items, since, errors = collect(window, sources, now)

    known = []
    if store is not None:
        known = known_clusters(store, (now or utcnow()) - dedup_window)

    clusters = deduplicate(items, known=known, threshold=threshold)

    kept: list[Story] = []
    dropped: list[Story] = []
    for cluster in clusters:
        story = Story(cluster=cluster, provenance=_provenance_of(cluster, resolver, now))
        if require_provenance and resolver is not None and not story.resolved:
            dropped.append(story)
        else:
            kept.append(story)

    if store is not None and persist:
        _record(store, kept, dropped, now)

    return RunResult(
        stories=kept,
        since=since,
        errors=errors,
        fetched=len(items),
        dropped=dropped,
        pages_fetched=resolver.fetcher.fetches if resolver and resolver.fetcher else 0,
    )


def _provenance_of(cluster: Cluster, resolver: Resolver | None, now: datetime | None) -> Provenance:
    """Resolve a cluster, or fall back to what its sources already supplied."""
    if resolver is not None:
        return resolver.resolve_cluster(cluster, now=now).provenance
    if cluster.primary_source_url:
        return classify(cluster.primary_source_url, via="source")
    return NONE


def _record(store: Store, kept: list[Story], dropped: list[Story], now: datetime | None) -> None:
    """Write every member of every cluster, kept and dropped alike.

    Duplicates and rejects are stored, not discarded: knowing a story was
    carried by four outlets is signal, and the rejected rows are the dataset the
    rubric gets tuned against later. A dropped story keeps the reason, so "why
    didn't I see this?" always has an answer.
    """
    with store.transaction():
        for story in kept:
            _record_cluster(store, story, STATUS_NEW, None, now)
            store.set_provenance(
                story.canonical.id,
                story.evidence.value,
                story.provenance.url,
                story.provenance.via,
            )
        for story in dropped:
            _record_cluster(store, story, STATUS_REJECTED, "no primary source", now)


def _record_cluster(
    store: Store, story: Story, status: str, reason: str | None, now: datetime | None
) -> None:
    """Write a cluster's canonical member at ``status``, and its duplicates."""
    cluster = story.cluster
    store.upsert(
        cluster.canonical,
        cluster_id=cluster.cluster_id,
        status=status,
        reason=reason,
        now=now,
    )
    for duplicate in cluster.duplicates:
        store.upsert(
            duplicate,
            cluster_id=cluster.cluster_id,
            status=STATUS_DUPLICATE,
            reason=f"duplicate of {cluster.canonical.id}",
            now=now,
        )
