"""Hacker News, via the Algolia search API.

Chosen as the first source because it needs no key and carries a usable
popularity signal (points, comments) in the same response as the story.

Endpoint: ``https://hn.algolia.com/api/v1/search_by_date``. Sorting by date
rather than relevance means the window is exact and pagination is stable while
we walk it -- ``search`` would reshuffle under us as scores move.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import httpx

from ..models import Item
from ..relevance import is_relevant, matched_terms
from ..timeutil import from_unix, to_utc

API_URL = "https://hn.algolia.com/api/v1/search_by_date"
ITEM_URL = "https://news.ycombinator.com/item?id={id}"

#: Algolia caps the page size here; asking for more is silently clamped.
MAX_HITS_PER_PAGE = 100

#: Stop walking pages even if the API claims more. A 24h window over the
#: points floor is far inside this; hitting it means the caller asked for
#: something unreasonable and we would rather truncate than hammer the API.
MAX_PAGES = 10


class HackerNews:
    """Ingester for HN stories above a points floor within a time window."""

    name = "hn"

    def __init__(
        self,
        min_points: int = 5,
        client: httpx.Client | None = None,
        timeout: float = 10.0,
        **_ignored: object,
    ) -> None:
        """
        Args:
            min_points: Ignore stories below this score. The HN firehose is
                mostly noise at 0-2 points; this is the stage-1 signal floor.
            client: Reusable HTTP client. One is created per fetch if omitted.
            timeout: Per-request timeout in seconds.
            _ignored: Absorbs pipeline-wide options this source does not use.
        """
        self.min_points = min_points
        self._client = client
        self._timeout = timeout

    def fetch(self, since: datetime) -> list[Item]:
        """Return AI-relevant HN stories published at or after ``since``."""
        cutoff = int(to_utc(since).timestamp())
        params_base = {
            "tags": "story",
            "numericFilters": f"created_at_i>={cutoff},points>={self.min_points}",
            "hitsPerPage": str(MAX_HITS_PER_PAGE),
        }

        items: list[Item] = []
        if self._client is not None:
            items = self._walk(self._client, params_base)
        else:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                items = self._walk(client, params_base)
        return items

    def _walk(self, client: httpx.Client, params_base: dict[str, str]) -> list[Item]:
        """Page through results until exhausted or :data:`MAX_PAGES` is hit."""
        items: list[Item] = []
        page = 0
        while page < MAX_PAGES:
            response = client.get(API_URL, params={**params_base, "page": str(page)})
            response.raise_for_status()
            payload = response.json()

            for hit in payload.get("hits", []):
                item = self._to_item(hit)
                if item is not None:
                    items.append(item)

            if page + 1 >= int(payload.get("nbPages", 0)):
                break
            page += 1
        return items

    def _to_item(self, hit: dict[str, Any]) -> Item | None:
        """Map one Algolia hit to an :class:`Item`, or ``None`` if it is dropped.

        A hit is dropped when it has no usable title or timestamp, or when it
        is not topically relevant.
        """
        title = (hit.get("title") or "").strip()
        created = hit.get("created_at_i")
        object_id = hit.get("objectID")
        if not title or created is None or object_id is None:
            return None

        discussion_url = ITEM_URL.format(id=object_id)
        # Ask HN / Show HN text posts carry no outbound URL; the thread is the story.
        url = (hit.get("url") or "").strip() or discussion_url

        terms = matched_terms(title, hit.get("url"))
        if not terms and not is_relevant(title):
            return None

        points = int(hit.get("points") or 0)
        comments = int(hit.get("num_comments") or 0)

        return Item(
            title=title,
            url=url,
            source=self.name,
            published_at=from_unix(created),
            # Points are the score; comments break ties between equal scores
            # without ever outweighing a point.
            raw_signal=float(points) + min(comments, 99) / 100.0,
            discussion_url=discussion_url,
            source_id=str(object_id),
            extra={
                "points": points,
                "num_comments": comments,
                "author": hit.get("author"),
                "matched_terms": terms,
            },
        )
