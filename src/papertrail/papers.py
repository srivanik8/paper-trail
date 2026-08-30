"""Gathering the facts a paper can be judged on.

Much thinner than the repository side, because arXiv exposes much less. Three
things are worth knowing and all three come from one Atom entry: whether the
submission was **withdrawn**, how many **versions** it has been through, and how
many **authors** signed it.

The version count is the useful one. A v1 that never moved is a preprint nobody
has revised; a v3 has survived at least two rounds of the author's own second
thoughts. It is weak evidence, which is why it produces a flag rather than a
verdict.
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from datetime import timedelta

import httpx

from .store import Store
from .substance import PaperFacts
from .timeutil import parse_iso, utcnow

API = "http://export.arxiv.org/api/query"

#: Papers do not change often; a week-old answer is a good answer.
DEFAULT_MAX_AGE = timedelta(days=7)

DEFAULT_TIMEOUT = 20.0

#: arXiv's courtesy delay between requests, in seconds.
REQUEST_DELAY = 3.0

_ATOM = {"atom": "http://www.w3.org/2005/Atom"}

#: Entry ids carry the version: http://arxiv.org/abs/2401.00001v3
_VERSION = re.compile(r"v(\d+)\s*$")

#: arXiv marks retractions in the comment field; there is no structured flag.
_WITHDRAWN = re.compile(r"\b(withdraw\w*|retract\w*)\b", re.IGNORECASE)

#: Accepts a bare id, an abs/pdf URL, and an optional version suffix.
_ARXIV_ID = re.compile(r"(?:arxiv\.org/(?:abs|pdf|html)/)?(?P<id>\d{4}\.\d{4,5}|[a-z\-]+/\d{7})")


def arxiv_id(value: str) -> str | None:
    """Extract a bare arXiv id from a URL or an id, or return ``None``."""
    match = _ARXIV_ID.search(value.strip())
    return match.group("id") if match else None


class ArxivPapers:
    """Reads paper facts from the arXiv export API, with a cache.

    Args:
        store: Where fetched facts are cached.
        client: Reusable HTTP client. One is created per call if omitted.
        timeout: Per-request timeout in seconds.
        max_age: Refetch facts older than this.
        delay: Courtesy gap between requests, per arXiv's guidance. As with the
            ingester, it is a gap and not a preamble: the first call of a run
            goes straight out.
    """

    def __init__(
        self,
        store: Store,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_age: timedelta = DEFAULT_MAX_AGE,
        delay: float = REQUEST_DELAY,
    ) -> None:
        self.store = store
        self._client = client
        self._timeout = timeout
        self._max_age = max_age
        self._delay = delay
        self._last_request_at: float | None = None
        self.requests = 0

    def facts(self, reference: str, now=None) -> PaperFacts:
        """Return facts for an arXiv id or URL."""
        identifier = arxiv_id(reference)
        if identifier is None:
            return PaperFacts(arxiv_id=reference, error="not an arXiv reference")

        moment = now or utcnow()
        cache_key = f"arxiv-facts:{identifier}"

        cached = self.store.cached_page(cache_key, fresh_after=moment - self._max_age)
        if cached is not None and cached["body"]:
            return _parse(identifier, cached["body"])

        body, error = self._retrieve(identifier)
        self.store.cache_page(
            cache_key,
            status=200 if error is None else 0,
            body=body,
            content_type="application/atom+xml",
            error=error,
            now=moment,
        )
        if error is not None:
            return PaperFacts(arxiv_id=identifier, error=error)
        return _parse(identifier, body)

    def _retrieve(self, identifier: str) -> tuple[str, str | None]:
        """Fetch one entry, returning ``(body, error)``."""
        if self._last_request_at is not None and self._delay:
            remaining = self._delay - (time.monotonic() - self._last_request_at)
            if remaining > 0:
                time.sleep(remaining)
        self._last_request_at = time.monotonic()

        params = {"id_list": identifier, "max_results": "1"}
        self.requests += 1
        try:
            if self._client is not None:
                response = self._client.get(API, params=params)
            else:
                with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                    response = client.get(API, params=params)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            return "", f"{type(exc).__name__}: {exc}"

        return response.text, None


def _parse(identifier: str, body: str) -> PaperFacts:
    """Turn one Atom entry into :class:`PaperFacts`."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        return PaperFacts(arxiv_id=identifier, error=f"unparseable response: {exc}")

    entry = root.find("atom:entry", _ATOM)
    if entry is None:
        return PaperFacts(arxiv_id=identifier, error="no such paper")

    raw_id = _text(entry, "atom:id") or ""
    version_match = _VERSION.search(raw_id)

    comment = ""
    for child in entry:
        if child.tag.endswith("}comment") and child.text:
            comment = child.text

    return PaperFacts(
        arxiv_id=identifier,
        version=int(version_match.group(1)) if version_match else 1,
        withdrawn=bool(_WITHDRAWN.search(comment)),
        authors=len(entry.findall("atom:author", _ATOM)),
        categories=tuple(
            category.get("term", "")
            for category in entry.findall("atom:category", _ATOM)
            if category.get("term")
        ),
        published_at=_at(_text(entry, "atom:published")),
        updated_at=_at(_text(entry, "atom:updated")),
    )


def _text(element: ET.Element, path: str) -> str | None:
    """Return the stripped text at ``path``, or ``None``."""
    found = element.find(path, _ATOM)
    return found.text.strip() if found is not None and found.text else None


def _at(value: str | None):
    """Parse a timestamp, tolerating absence."""
    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None
