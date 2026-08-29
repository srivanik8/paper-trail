"""Deciding what an item can be checked against.

The order is the whole design:

1. **Ask the source.** Hugging Face and arXiv hand over the paper for free.
2. **Ask the URL.** A link straight to arXiv, GitHub or a lab post needs no
   network at all -- and this is most of what survives stage 1.
3. **Ask the page.** Only now is anything fetched, once, and only its outbound
   links are read.

Everything that finishes at :data:`Evidence.NONE` is dropped before the LLM
stage ever sees it. That is most of the volume, and all of the cost saving.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .dedup import Cluster
from .extract import extract_links
from .fetcher import Fetcher
from .models import Item
from .provenance import NONE, Evidence, Provenance, best, classify

#: Stop after this many classified links per page. A resolved candidate is
#: found in the first handful or not at all.
MAX_CANDIDATES = 60


@dataclass(frozen=True, slots=True)
class Resolution:
    """What resolving one item found."""

    item: Item
    provenance: Provenance
    fetched: bool = False

    @property
    def resolved(self) -> bool:
        """True if this item points at something checkable."""
        return self.provenance.resolved


class Resolver:
    """Resolves items to their primary sources.

    Args:
        fetcher: Page retrieval. Without one, resolution is offline-only:
            steps 1 and 2 still run, step 3 is skipped.
    """

    def __init__(self, fetcher: Fetcher | None = None) -> None:
        self.fetcher = fetcher

    def resolve(self, item: Item, now: datetime | None = None) -> Resolution:
        """Resolve a single item."""
        # 1. The ingester already knew (Hugging Face, arXiv).
        if item.primary_source_url:
            supplied = classify(item.primary_source_url, via="source")
            if supplied.resolved:
                return Resolution(item, supplied)

        # 2. The item's own URL is the artifact.
        itself = classify(item.url, via="self")
        if itself.resolved:
            return Resolution(item, itself)

        # 3. Read the page and see what it points at.
        if self.fetcher is None:
            return Resolution(item, NONE)

        page = self.fetcher.get(item.url, now=now)
        if not page.ok:
            return Resolution(item, NONE, fetched=True)

        candidates = []
        for link in extract_links(page.body, page.url):
            found = classify(link, via="page")
            if found.resolved:
                candidates.append(found)
                if len(candidates) >= MAX_CANDIDATES:
                    break

        return Resolution(item, best(candidates), fetched=True)

    def resolve_cluster(self, cluster: Cluster, now: datetime | None = None) -> Resolution:
        """Resolve a cluster, trying its members in rank order.

        A cluster is resolved as soon as any member is, and the canonical item
        is what gets reported -- the HN link keeps its score while the arXiv
        entry supplies the paper.
        """
        fetched = False
        for member in cluster.members:
            resolution = self.resolve(member, now=now)
            fetched = fetched or resolution.fetched
            if resolution.resolved:
                return Resolution(cluster.canonical, resolution.provenance, fetched=fetched)
        return Resolution(cluster.canonical, NONE, fetched=fetched)


def evidence_of(resolution: Resolution) -> Evidence:
    """Convenience accessor for a resolution's evidence type."""
    return resolution.provenance.evidence
