"""arXiv, via the export API.

Returns Atom XML rather than JSON, and asks callers to leave roughly three
seconds *between* requests -- the delay is built in rather than bolted on
later, because getting throttled mid-demo is the classic way this source
breaks. It is a gap, not a preamble: the first request of a run goes straight
out, and only a follow-up waits.

arXiv carries **no popularity signal**, so every item here has a ``raw_signal``
of zero and sorts below anything from HN or Hugging Face. That is correct at
this stage: a paper's standing comes from its provenance and its scoring, which
arrive on days 3 and 5. It is in the pipeline now because a paper that lands
here first should already be in the store when HN discovers it tomorrow.
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from datetime import datetime

import httpx

from ..models import Item
from ..timeutil import parse_iso, to_utc

API_URL = "http://export.arxiv.org/api/query"
ABS_URL = "https://arxiv.org/abs/{id}"

#: The categories this project cares about.
CATEGORIES = ("cs.AI", "cs.LG", "cs.CL")

#: arXiv's stated courtesy delay between requests, in seconds.
REQUEST_DELAY = 3.0

MAX_RESULTS = 100

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}

# Entry ids look like http://arxiv.org/abs/2401.00001v1; the version is not
# part of the paper's identity.
_ID_PREFIX = "http://arxiv.org/abs/"


class ArxivListings:
    """Ingester for recent submissions in :data:`CATEGORIES`."""

    name = "arxiv"

    def __init__(
        self,
        categories: tuple[str, ...] = CATEGORIES,
        max_results: int = MAX_RESULTS,
        client: httpx.Client | None = None,
        timeout: float = 20.0,
        delay: float = REQUEST_DELAY,
        **_ignored: object,
    ) -> None:
        """
        Args:
            categories: arXiv categories to search.
            max_results: Cap on entries requested.
            client: Reusable HTTP client. One is created per fetch if omitted.
            timeout: Per-request timeout in seconds. Higher than the other
                sources: the export API is reliably slow.
            delay: Seconds to wait before the request, per arXiv's guidance.
                Set to ``0`` in tests.
            _ignored: Absorbs pipeline-wide options this source does not use.
        """
        self.categories = categories
        self.max_results = max_results
        self._client = client
        self._timeout = timeout
        self._delay = delay
        self._last_request_at: float | None = None

    def _wait_turn(self) -> None:
        """Sleep only for whatever remains of the courtesy gap."""
        if self._last_request_at is not None and self._delay:
            remaining = self._delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

    def fetch(self, since: datetime) -> list[Item]:
        """Return submissions published at or after ``since``."""
        self._wait_turn()

        params = {
            "search_query": " OR ".join(f"cat:{category}" for category in self.categories),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "max_results": str(self.max_results),
        }

        if self._client is not None:
            response = self._client.get(API_URL, params=params)
        else:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                response = client.get(API_URL, params=params)
        response.raise_for_status()

        cutoff = to_utc(since)
        items = []
        for entry in ET.fromstring(response.text).findall("atom:entry", _ATOM):
            item = self._to_item(entry)
            if item is not None and item.published_at >= cutoff:
                items.append(item)
        return items

    def _to_item(self, entry: ET.Element) -> Item | None:
        """Map one Atom entry to an :class:`Item`, or ``None`` if unusable."""
        raw_id = _text(entry, "atom:id")
        title = _text(entry, "atom:title")
        published = _text(entry, "atom:published")
        if not raw_id or not title or not published:
            return None

        arxiv_id = raw_id.removeprefix(_ID_PREFIX)
        arxiv_id = arxiv_id.split("v")[0] if "v" in arxiv_id.rsplit("/", 1)[-1] else arxiv_id

        try:
            published_at = parse_iso(published)
        except ValueError:
            return None

        abs_url = ABS_URL.format(id=arxiv_id)
        authors = [
            name.text.strip()
            for author in entry.findall("atom:author", _ATOM)
            if (name := author.find("atom:name", _ATOM)) is not None and name.text
        ]

        return Item(
            title=" ".join(title.split()),  # arXiv wraps titles across lines
            url=abs_url,
            source=self.name,
            published_at=published_at,
            # No popularity signal exists here; see the module docstring.
            raw_signal=0.0,
            primary_source_url=abs_url,
            source_id=arxiv_id,
            extra={
                "authors": authors,
                "categories": [
                    category.get("term")
                    for category in entry.findall("atom:category", _ATOM)
                    if category.get("term")
                ],
                "summary": " ".join((_text(entry, "atom:summary") or "").split()),
            },
        )


def _text(element: ET.Element, path: str) -> str | None:
    """Return the stripped text at ``path``, or ``None``."""
    found = element.find(path, _ATOM)
    return found.text.strip() if found is not None and found.text else None
