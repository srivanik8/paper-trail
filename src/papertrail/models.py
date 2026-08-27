"""The one schema every source normalizes into.

Ingesters differ wildly in what they return; everything downstream -- dedup,
provenance resolution, scoring, rendering -- sees only :class:`Item`. Adding a
source must never widen this shape.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .ids import item_id
from .timeutil import isoformat_utc, to_utc


@dataclass(frozen=True, slots=True)
class Item:
    """A single normalized story.

    Attributes:
        title: Headline as published.
        url: What the story points at. Identity is derived from this.
        source: Ingester that produced the item, e.g. ``hn``.
        published_at: Aware UTC timestamp of publication.
        raw_signal: Source-native popularity, comparable only within a source
            (HN points, stars, upvotes). Cross-source ranking happens later.
        primary_source_url: Paper, repo or official post backing the claim.
            Populated on day 3; ``None`` means "not yet resolved", not "none
            exists".
        discussion_url: Where people are talking about it, when that differs
            from ``url``.
        source_id: The item's native id at its source, for debugging.
        extra: Source-specific fields kept for later stages.
    """

    title: str
    url: str
    source: str
    published_at: datetime
    raw_signal: float
    primary_source_url: str | None = None
    discussion_url: str | None = None
    source_id: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.title.strip():
            raise ValueError("item title must not be empty")
        if not self.url.strip():
            raise ValueError("item url must not be empty")
        # Normalize at construction so no downstream stage has to wonder.
        object.__setattr__(self, "published_at", to_utc(self.published_at))

    @property
    def id(self) -> str:
        """Stable id derived from the canonical URL."""
        return item_id(self.url)

    def to_dict(self) -> dict[str, Any]:
        """Render as JSON-safe primitives, with timestamps as ISO-8601 UTC."""
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "source": self.source,
            "published_at": isoformat_utc(self.published_at),
            "raw_signal": self.raw_signal,
            "primary_source_url": self.primary_source_url,
            "discussion_url": self.discussion_url,
            "source_id": self.source_id,
            "extra": self.extra,
        }
