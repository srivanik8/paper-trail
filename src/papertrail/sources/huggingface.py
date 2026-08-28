"""Hugging Face daily papers.

A curated, upvoted feed of the day's papers -- so unlike the HN firehose it
needs no topical filtering, and unlike arXiv it carries a popularity signal.

Every item arrives with its primary source already known: the arXiv id *is*
the paper. Day 3's resolver gets these for free.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..models import Item
from ..timeutil import parse_iso, to_utc

API_URL = "https://huggingface.co/api/daily_papers"
ARXIV_ABS = "https://arxiv.org/abs/{id}"
PAPER_PAGE = "https://huggingface.co/papers/{id}"

MAX_LIMIT = 100


class HuggingFacePapers:
    """Ingester for the Hugging Face daily papers feed."""

    name = "hf"

    def __init__(
        self,
        limit: int = MAX_LIMIT,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        **_ignored: object,
    ) -> None:
        """
        Args:
            limit: How many recent papers to request.
            client: Reusable HTTP client. One is created per fetch if omitted.
            timeout: Per-request timeout in seconds.
            _ignored: Absorbs pipeline-wide options this source does not use,
                such as ``min_points``.
        """
        self.limit = min(limit, MAX_LIMIT)
        self._client = client
        self._timeout = timeout

    def fetch(self, since: datetime) -> list[Item]:
        """Return papers published at or after ``since``."""
        if self._client is not None:
            return self._fetch(self._client, since)
        with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
            return self._fetch(client, since)

    def _fetch(self, client: httpx.Client, since: datetime) -> list[Item]:
        response = client.get(API_URL, params={"limit": str(self.limit)})
        response.raise_for_status()

        cutoff = to_utc(since)
        items = []
        for entry in response.json():
            item = self._to_item(entry)
            if item is not None and item.published_at >= cutoff:
                items.append(item)
        return items

    def _to_item(self, entry: dict[str, Any]) -> Item | None:
        """Map one feed entry to an :class:`Item`, or ``None`` if unusable."""
        paper = entry.get("paper") or {}
        arxiv_id = paper.get("id")
        title = (paper.get("title") or entry.get("title") or "").strip()
        published = paper.get("publishedAt") or entry.get("publishedAt")
        if not arxiv_id or not title or not published:
            return None

        try:
            published_at = parse_iso(published)
        except ValueError:
            return None

        upvotes = int(paper.get("upvotes") or 0)
        comments = int(entry.get("numComments") or 0)

        return Item(
            title=title,
            url=ARXIV_ABS.format(id=arxiv_id),
            source=self.name,
            published_at=published_at,
            # Same shape as HN: score, with comments only breaking ties.
            raw_signal=float(upvotes) + min(comments, 99) / 100.0,
            primary_source_url=ARXIV_ABS.format(id=arxiv_id),
            discussion_url=PAPER_PAGE.format(id=arxiv_id),
            source_id=str(arxiv_id),
            extra={
                "upvotes": upvotes,
                "num_comments": comments,
                "summary": (paper.get("summary") or "").strip(),
            },
        )
