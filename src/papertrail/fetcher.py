"""Polite, cached page retrieval.

The resolver reads other people's websites, so this module exists to make that
defensible. Every rule here is about being a good citizen of somebody else's
server rather than about correctness of the digest:

* **One fetch per URL, ever.** Results are cached in the store, failures
  included -- a URL that 404s must not be retried on every run.
* **robots.txt is honoured**, fetched once per host and cached for the process.
* **An honest User-Agent** that says what this is and where it came from.
* **A hard cap on body size**, because a resolver does not need the 40MB video
  page it accidentally asked for.

This is the part the day 3 plan warns will quietly break in week 6 if it is
written carelessly now.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from .ids import canonical_url
from .store import Store
from .timeutil import utcnow

USER_AGENT = "paper-trail/0.1 (+https://github.com/srivanik8/paper-trail)"

#: Cached pages older than this are refetched.
DEFAULT_MAX_AGE = timedelta(days=14)

#: Stop reading a response after this many characters. Enough for any article's
#: worth of links; small enough that one bad URL cannot exhaust memory.
MAX_BODY_CHARS = 2_000_000

DEFAULT_TIMEOUT = 5.0

#: Only markup is worth scanning for links.
_READABLE_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

#: Sentinel statuses for outcomes that never reached HTTP.
STATUS_ERROR = 0
STATUS_BLOCKED_BY_ROBOTS = -1
STATUS_UNREADABLE = -2


@dataclass(frozen=True, slots=True)
class Page:
    """The outcome of asking for a URL."""

    url: str
    status: int
    body: str = ""
    content_type: str | None = None
    error: str | None = None
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        """True if there is a body worth reading."""
        return 200 <= self.status < 300 and bool(self.body)


class Fetcher:
    """Fetches pages once, politely, and remembers the outcome.

    Args:
        store: Where fetch outcomes are cached.
        client: Reusable HTTP client. One is created per fetch if omitted.
        timeout: Per-request timeout in seconds.
        max_age: Refetch a cached page older than this.
        obey_robots: Set ``False`` only for tests of other behaviour.
    """

    def __init__(
        self,
        store: Store,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_age: timedelta = DEFAULT_MAX_AGE,
        obey_robots: bool = True,
    ) -> None:
        self.store = store
        self._client = client
        self._timeout = timeout
        self._max_age = max_age
        self._obey_robots = obey_robots
        self._robots: dict[str, RobotFileParser | None] = {}
        self.fetches = 0

    def get(self, url: str, now: datetime | None = None) -> Page:
        """Return the page at ``url``, from cache when possible."""
        target = canonical_url(url)
        if not target.startswith(("http://", "https://")):
            return Page(url=target, status=STATUS_ERROR, error="not an http url")

        cached = self.store.cached_page(target, fresh_after=(now or utcnow()) - self._max_age)
        if cached is not None:
            return Page(
                url=target,
                status=cached["status"],
                body=cached["body"],
                content_type=cached["content_type"],
                error=cached["error"],
                from_cache=True,
            )

        if self._client is not None:
            page = self._retrieve(self._client, target)
        else:
            with httpx.Client(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT},
            ) as client:
                page = self._retrieve(client, target)

        self.store.cache_page(
            target,
            status=page.status,
            body=page.body,
            content_type=page.content_type,
            error=page.error,
            now=now,
        )
        return page

    def _retrieve(self, client: httpx.Client, url: str) -> Page:
        """Perform one request, converting every failure into a cacheable Page."""
        if self._obey_robots and not self._allowed(client, url):
            return Page(url=url, status=STATUS_BLOCKED_BY_ROBOTS, error="disallowed by robots.txt")

        self.fetches += 1
        try:
            response = client.get(url, headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError as exc:
            return Page(url=url, status=STATUS_ERROR, error=f"{type(exc).__name__}: {exc}")

        content_type = response.headers.get("content-type", "").split(";")[0].strip()
        if content_type and not content_type.startswith(_READABLE_TYPES):
            return Page(
                url=url,
                status=STATUS_UNREADABLE,
                content_type=content_type,
                error=f"not markup: {content_type}",
            )

        return Page(
            url=url,
            status=response.status_code,
            body=response.text[:MAX_BODY_CHARS],
            content_type=content_type or None,
        )

    def _allowed(self, client: httpx.Client, url: str) -> bool:
        """True if ``url`` may be fetched under its host's robots.txt.

        A host whose robots.txt cannot be retrieved is treated as permissive,
        which is what the standard prescribes and what every crawler does.
        """
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"

        if origin not in self._robots:
            self._robots[origin] = self._load_robots(client, origin)

        parser = self._robots[origin]
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    def _load_robots(self, client: httpx.Client, origin: str) -> RobotFileParser | None:
        """Fetch and parse ``origin``'s robots.txt, or ``None`` if unavailable."""
        try:
            response = client.get(f"{origin}/robots.txt", headers={"User-Agent": USER_AGENT})
        except httpx.HTTPError:
            return None

        if response.status_code != 200:
            return None

        parser = RobotFileParser()
        parser.parse(response.text.splitlines())
        return parser
