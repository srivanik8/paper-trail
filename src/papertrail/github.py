"""Gathering the facts a repository can be judged on.

Four questions, four endpoints, and each one is a request against somebody
else's rate limit, so the results are cached in the store for a day. With a
``GITHUB_TOKEN`` the budget is 5,000 requests an hour; without one it is 60,
which a single run can exhaust. The token is read from the environment and never
logged.

Only facts are collected here. What they mean is :mod:`papertrail.substance`.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta

import httpx

from .store import Store
from .substance import RepoFacts, code_and_doc_files
from .timeutil import parse_iso, utcnow

API = "https://api.github.com"
API_VERSION = "2022-11-28"

#: Repository facts change slowly; a day-old answer is a good answer.
DEFAULT_MAX_AGE = timedelta(hours=24)

DEFAULT_TIMEOUT = 10.0

#: Contributors are paginated. One page of this size answers "is this one
#: person or a project?" without walking a 3,000-contributor list.
CONTRIBUTOR_PAGE = 100

#: A recursive tree can be enormous; this is plenty to tell code from a README.
MAX_TREE_ENTRIES = 3000


class GitHub:
    """Reads repository facts from the GitHub REST API, with a cache.

    Args:
        store: Where fetched facts are cached.
        token: API token. Defaults to ``GITHUB_TOKEN`` in the environment.
        client: Reusable HTTP client. One is created per call if omitted.
        timeout: Per-request timeout in seconds.
        max_age: Refetch facts older than this.
    """

    def __init__(
        self,
        store: Store,
        token: str | None = None,
        client: httpx.Client | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        max_age: timedelta = DEFAULT_MAX_AGE,
    ) -> None:
        self.store = store
        self._token = token if token is not None else os.environ.get("GITHUB_TOKEN")
        self._client = client
        self._timeout = timeout
        self._max_age = max_age
        self.requests = 0

    @property
    def authenticated(self) -> bool:
        """True if a token was supplied. Without one the budget is 60/hour."""
        return bool(self._token)

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": "paper-trail/0.1",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    def facts(self, slug: str, now: datetime | None = None) -> RepoFacts:
        """Return facts for ``owner/repo``, from cache when fresh."""
        moment = now or utcnow()
        cache_key = f"github-facts:{slug}"

        cached = self.store.cached_page(cache_key, fresh_after=moment - self._max_age)
        if cached is not None and cached["body"]:
            return _decode(slug, cached["body"])

        if self._client is not None:
            facts = self._gather(self._client, slug)
        else:
            with httpx.Client(timeout=self._timeout, follow_redirects=True) as client:
                facts = self._gather(client, slug)

        self.store.cache_page(
            cache_key,
            status=200 if facts.retrieved else 0,
            body=_encode(facts),
            content_type="application/json",
            error=facts.error,
            now=moment,
        )
        return facts

    def _gather(self, client: httpx.Client, slug: str) -> RepoFacts:
        """Perform the four calls, tolerating failure in the optional three."""
        try:
            response = self._get(client, f"{API}/repos/{slug}")
            response.raise_for_status()
            repo = response.json()
        except httpx.HTTPError as exc:
            return RepoFacts(slug=slug, error=f"{type(exc).__name__}: {exc}")

        default_branch = repo.get("default_branch") or "main"

        return RepoFacts(
            slug=repo.get("full_name") or slug,
            stars=int(repo.get("stargazers_count") or 0),
            created_at=_at(repo.get("created_at")),
            pushed_at=_at(repo.get("pushed_at")),
            contributors=self._contributors(client, slug),
            has_license=repo.get("license") is not None,
            archived=bool(repo.get("archived")),
            is_fork=bool(repo.get("fork")),
            description=repo.get("description") or "",
            readme=self._readme(client, slug),
            **self._commit_span(client, slug),
            **self._tree(client, slug, default_branch),
        )

    def _get(self, client: httpx.Client, url: str, **params: str) -> httpx.Response:
        """One counted request."""
        self.requests += 1
        return client.get(url, headers=self._headers(), params=params or None)

    def _contributors(self, client: httpx.Client, slug: str) -> int | None:
        """Count contributors, reading the ``Link`` header rather than paging.

        GitHub reports the last page number for a page size of one, which turns
        an unbounded walk into a single request.
        """
        try:
            response = self._get(client, f"{API}/repos/{slug}/contributors", per_page="1", anon="1")
            if response.status_code != 200:
                return None
        except httpx.HTTPError:
            return None

        link = response.headers.get("link", "")
        for part in link.split(","):
            if 'rel="last"' in part and "page=" in part:
                try:
                    return int(part.split("page=")[-1].split(">")[0].split("&")[0])
                except ValueError:
                    break

        # No Link header means a single page; count what came back.
        payload = response.json()
        return len(payload) if isinstance(payload, list) else None

    def _commit_span(self, client: httpx.Client, slug: str) -> dict[str, datetime | None]:
        """Find the first and last commit dates.

        The last commit is the newest entry on page one. The first is the
        newest entry on the *last* page, which the ``Link`` header names.
        """
        blank: dict[str, datetime | None] = {"first_commit_at": None, "last_commit_at": None}
        try:
            response = self._get(client, f"{API}/repos/{slug}/commits", per_page="1")
            if response.status_code != 200:
                return blank
            newest = response.json()
        except httpx.HTTPError:
            return blank

        if not isinstance(newest, list) or not newest:
            return blank

        last_commit_at = _commit_date(newest[0])
        first_commit_at = last_commit_at

        last_page = _last_page(response.headers.get("link", ""))
        if last_page and last_page > 1:
            try:
                oldest = self._get(
                    client, f"{API}/repos/{slug}/commits", per_page="1", page=str(last_page)
                )
                if oldest.status_code == 200 and oldest.json():
                    first_commit_at = _commit_date(oldest.json()[0])
            except httpx.HTTPError:
                pass

        return {"first_commit_at": first_commit_at, "last_commit_at": last_commit_at}

    def _tree(self, client: httpx.Client, slug: str, branch: str) -> dict[str, int | None]:
        """Count implementation files against documentation files."""
        try:
            response = self._get(
                client,
                f"{API}/git/trees/{branch}".replace("/git/", f"/repos/{slug}/git/"),
                recursive="1",
            )
            if response.status_code != 200:
                return {"code_files": None, "doc_files": 0}
            payload = response.json()
        except httpx.HTTPError:
            return {"code_files": None, "doc_files": 0}

        paths = [
            entry["path"]
            for entry in payload.get("tree", [])[:MAX_TREE_ENTRIES]
            if entry.get("type") == "blob" and entry.get("path")
        ]
        code, docs = code_and_doc_files(paths)
        return {"code_files": code, "doc_files": docs}

    def _readme(self, client: httpx.Client, slug: str) -> str:
        """Return the decoded README, or an empty string."""
        try:
            response = self._get(client, f"{API}/repos/{slug}/readme")
            if response.status_code != 200:
                return ""
            payload = response.json()
        except httpx.HTTPError:
            return ""

        if payload.get("encoding") != "base64" or not payload.get("content"):
            return ""
        try:
            return base64.b64decode(payload["content"]).decode("utf-8", errors="replace")
        except (ValueError, TypeError):
            return ""


def _at(value: str | None) -> datetime | None:
    """Parse an API timestamp, tolerating absence."""
    if not value:
        return None
    try:
        return parse_iso(value)
    except ValueError:
        return None


def _commit_date(entry: dict) -> datetime | None:
    """Pull the committer date out of a commit entry."""
    return _at((entry.get("commit") or {}).get("committer", {}).get("date"))


def _last_page(link_header: str) -> int | None:
    """Extract the last page number from a ``Link`` header."""
    for part in link_header.split(","):
        if 'rel="last"' in part and "page=" in part:
            try:
                return int(part.split("page=")[-1].split(">")[0].split("&")[0])
            except ValueError:
                return None
    return None


def _encode(facts: RepoFacts) -> str:
    """Serialize facts for the cache."""
    payload = {
        "slug": facts.slug,
        "stars": facts.stars,
        "created_at": facts.created_at.isoformat() if facts.created_at else None,
        "pushed_at": facts.pushed_at.isoformat() if facts.pushed_at else None,
        "first_commit_at": facts.first_commit_at.isoformat() if facts.first_commit_at else None,
        "last_commit_at": facts.last_commit_at.isoformat() if facts.last_commit_at else None,
        "contributors": facts.contributors,
        "code_files": facts.code_files,
        "doc_files": facts.doc_files,
        "has_license": facts.has_license,
        "archived": facts.archived,
        "is_fork": facts.is_fork,
        "description": facts.description,
        "readme": facts.readme[:20_000],
        "error": facts.error,
    }
    return json.dumps(payload, ensure_ascii=False)


def _decode(slug: str, body: str) -> RepoFacts:
    """Rebuild facts from the cache, falling back to a bare record."""
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return RepoFacts(slug=slug, error="corrupt cache entry")

    return RepoFacts(
        slug=payload.get("slug", slug),
        stars=payload.get("stars", 0),
        created_at=_at(payload.get("created_at")),
        pushed_at=_at(payload.get("pushed_at")),
        first_commit_at=_at(payload.get("first_commit_at")),
        last_commit_at=_at(payload.get("last_commit_at")),
        contributors=payload.get("contributors"),
        code_files=payload.get("code_files"),
        doc_files=payload.get("doc_files", 0),
        has_license=payload.get("has_license", True),
        archived=payload.get("archived", False),
        is_fork=payload.get("is_fork", False),
        description=payload.get("description", ""),
        readme=payload.get("readme", ""),
        error=payload.get("error"),
    )
